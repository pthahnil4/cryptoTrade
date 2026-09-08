#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热量模块冒烟测试（MySQL 版）：用 Flask test_client 验证 calorie_bp 全部 API

测试隔离策略（保护真实数据）：
  1. 运行前快照 calorie_foods / calorie_records / calorie_meal_items / calorie_config 四表
  2. 清空四表后执行全部 API 用例
  3. finally 中无条件恢复快照（无论用例成败）

前置条件：环境变量 CRYPTO_DB_URL 已设置
  （mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4）
"""

import json
import os
import sys

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if not os.environ.get('CRYPTO_DB_URL', '').strip():
    print('❌ 请先设置环境变量 CRYPTO_DB_URL（mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4）')
    sys.exit(2)

from sqlalchemy import select, delete  # noqa: E402
from crypto import calorie_routes as cr  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import CalorieFood, CalorieRecord, CalorieMealItem, CalorieConfig  # noqa: E402
from flask import Flask  # noqa: E402

print(f"[Smoke] 目标数据库: {os.environ['CRYPTO_DB_URL'].split('@')[-1]}")

# =============================================================================
# 真实数据快照 + 清场（测试不污染生产数据）
# =============================================================================

def _snapshot():
    with session_scope() as s:
        foods = [(f.id, f.name, f.unit, f.calories, f.category, f.created_at) for f in
                 s.execute(select(CalorieFood).order_by(CalorieFood.id)).scalars()]
        records = [(r.id, r.date, r.morning_weight, r.evening_weight, r.bmr,
                    r.breakfast_food, r.breakfast_calories,
                    r.lunch_food, r.lunch_calories,
                    r.dinner_food, r.dinner_calories,
                    r.intake_deficit, r.daily_steps, r.exercise_calories,
                    r.calorie_deficit, r.cumulative_deficit,
                    r.created_at, r.updated_at) for r in
                   s.execute(select(CalorieRecord)).scalars()]
        items = [(i.record_id, i.meal, i.position, i.name, i.calories) for i in
                 s.execute(select(CalorieMealItem)).scalars()]
        cfg = s.get(CalorieConfig, 1)
        config = None if cfg is None else (cfg.height, cfg.age, cfg.step_frequency,
                                           cfg.weight_factor, cfg.target_deficit)
    return foods, records, items, config


def _clean():
    with session_scope() as s:
        s.execute(delete(CalorieMealItem))
        s.execute(delete(CalorieRecord))
        s.execute(delete(CalorieFood))
        s.execute(delete(CalorieConfig))


def _restore(snap):
    foods, records, items, config = snap
    with session_scope() as s:
        s.execute(delete(CalorieMealItem))
        s.execute(delete(CalorieRecord))
        s.execute(delete(CalorieFood))
        s.execute(delete(CalorieConfig))
        for fid, name, unit, calories, category, created_at in foods:
            s.add(CalorieFood(id=fid, name=name, unit=unit, calories=calories,
                              category=category, created_at=created_at))
        s.flush()
        for row in records:
            s.add(CalorieRecord(
                id=row[0], date=row[1], morning_weight=row[2], evening_weight=row[3],
                bmr=row[4], breakfast_food=row[5], breakfast_calories=row[6],
                lunch_food=row[7], lunch_calories=row[8],
                dinner_food=row[9], dinner_calories=row[10],
                intake_deficit=row[11], daily_steps=row[12], exercise_calories=row[13],
                calorie_deficit=row[14], cumulative_deficit=row[15],
                created_at=row[16], updated_at=row[17]))
        s.flush()
        for record_id, meal, position, name, calories in items:
            s.add(CalorieMealItem(record_id=record_id, meal=meal, position=position,
                                  name=name, calories=calories))
        if config is not None:
            s.add(CalorieConfig(id=1, height=config[0], age=config[1],
                                step_frequency=config[2], weight_factor=config[3],
                                target_deficit=config[4]))


_SNAP = _snapshot()
print(f"[Smoke] 已快照真实数据: 食物 {len(_SNAP[0])} / 记录 {len(_SNAP[1])}，测试后自动恢复")
_clean()

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.register_blueprint(cr.calorie_bp)
client = app.test_client()

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def post(url, body):
    return client.post(url, data=json.dumps(body), content_type='application/json')


def put(url, body):
    return client.put(url, data=json.dumps(body), content_type='application/json')


def num_eq(a, b, tol=0.01):
    return abs(float(a or 0) - float(b or 0)) < tol


try:
    print('\n=== 1. 页面路由 ===')
    r = client.get('/calorie')
    check('页面返回 200', r.status_code == 200)

    print('\n=== 2. 空库初始状态 ===')
    r = client.get('/calorie/api/records')
    data = r.get_json()
    check('记录接口 success', data['success'] is True)
    check('初始无记录', len(data['records']) == 0)
    check('看板 record_count=0', data['dashboard']['record_count'] == 0)
    check('无配置行时返回默认配置', num_eq(data['config']['height'], 169)
          and num_eq(data['config']['target_deficit'], 100000))

    print('\n=== 3. 食物库种子兜底 ===')
    r = client.get('/calorie/api/foods')
    data = r.get_json()
    check('空表自动补种子 115 个', data['total'] == 115, str(data['total']))
    r = client.get('/calorie/api/foods?search=燕麦')
    data = r.get_json()
    check('search 过滤生效', data['total'] >= 1 and all('燕麦' in f['name'] for f in data['foods']))
    r = client.get('/calorie/api/foods?category=水果')
    data = r.get_json()
    check('category 过滤生效', data['total'] >= 1 and all(f['category'] == '水果' for f in data['foods']))
    names = [f['name'] for f in data['foods']]
    check('列表按名称排序', names == sorted(names))
    r = client.get('/calorie/api/categories')
    cats = r.get_json()['categories']
    check('分类接口返回主要分类', {'主食', '肉蛋', '蔬菜', '水果'} <= set(cats), str(cats))

    print('\n=== 4. 保存记录（新格式 foods 数组）===')
    body = {
        'date': '2026-08-01',
        'morning_weight': 60,
        'evening_weight': 59.5,
        'breakfast_foods': [{'name': '白米饭', 'calories': 116}],
        'lunch_foods': [],
        'dinner_foods': [],
        'daily_steps': 0
    }
    r = post('/calorie/api/record', body)
    data = r.get_json()
    rec = data['record']
    check('保存成功', data['success'] is True)
    # bmr = 10*60/2 + 6.25*169 - 5*29 + 5 = 1216.25
    check('bmr 计算正确', num_eq(rec['bmr'], 1216.25), str(rec['bmr']))
    check('早餐热量=明细求和', num_eq(rec['breakfast_calories'], 116))
    check('早餐食物名拼接', rec['breakfast_food'] == '白米饭', rec['breakfast_food'])
    check('三餐明细数组回显', rec['breakfast_foods'] == [{'name': '白米饭', 'calories': 116.0}])
    check('摄入缺口=bmr-摄入', num_eq(rec['intake_deficit'], 1216.25 - 116))
    check('已有食物不产生 new_foods', data['new_foods'] == [], str(data['new_foods']))

    print('\n=== 5. 保存第二条记录（累计重算 + 新食物自动入库）===')
    body = {
        'date': '2026-08-02',
        'morning_weight': 60,
        'breakfast_foods': [{'name': '冒烟测试食物甲', 'calories': 100}],
        'lunch_foods': [{'name': '冒烟测试食物乙', 'calories': 200}],
        'dinner_foods': [],
        'daily_steps': 10000
    }
    r = post('/calorie/api/record', body)
    data = r.get_json()
    rec_b = data['record']
    # exercise = 10000*0.7*55/1000 = 385
    check('运动消耗计算正确', num_eq(rec_b['exercise_calories'], 385.0), str(rec_b['exercise_calories']))
    # intake_deficit = 1216.25 - 300 = 916.25; deficit = 1301.25
    check('缺口计算正确', num_eq(rec_b['calorie_deficit'], 916.25 + 385.0), str(rec_b['calorie_deficit']))
    new_names = sorted(f['name'] for f in data['new_foods'])
    check('新食物自动入库 2 个', new_names == ['冒烟测试食物乙', '冒烟测试食物甲'], str(new_names))
    check('新食物 id 递增 food_116', any(f['id'] == 'food_116' for f in data['new_foods']),
          str([f['id'] for f in data['new_foods']]))
    # 累计：A=1100.25?  A deficit = 1216.25-116 = 1100.25；B 累计 = 1100.25+1301.25
    r = client.get('/calorie/api/records')
    recs = {x['id']: x for x in r.get_json()['records']}
    check('累计缺口-第1天', num_eq(recs['2026-08-01']['cumulative_deficit'], 1100.25),
          str(recs['2026-08-01']['cumulative_deficit']))
    check('累计缺口-第2天', num_eq(recs['2026-08-02']['cumulative_deficit'], 2401.5),
          str(recs['2026-08-02']['cumulative_deficit']))
    check('列表按日期降序', [x['id'] for x in r.get_json()['records']] == ['2026-08-02', '2026-08-01'])
    check('看板统计正确', num_eq(r.get_json()['dashboard']['total_calorie_deficit'], 2401.5))

    print('\n=== 6. 保存记录（旧格式字符串 + 缺日期校验）===')
    body = {
        'date': '2026-08-03',
        'morning_weight': 60,
        'breakfast_food': '冒烟测试食物丙',
        'breakfast_calories': 50,
        'daily_steps': 0
    }
    r = post('/calorie/api/record', body)
    data = r.get_json()
    check('旧格式保存成功', data['success'] is True)
    check('旧格式食物自动入库', any(f['name'] == '冒烟测试食物丙' for f in data['new_foods']))
    r = post('/calorie/api/record', {'morning_weight': 60})
    check('缺日期返回 400', r.status_code == 400 and r.get_json()['success'] is False)

    print('\n=== 7. 更新记录（保留 created_at）===')
    created_before = recs['2026-08-01']['created_at']
    body = {
        'date': '2026-08-01',
        'morning_weight': 61,
        'breakfast_foods': [{'name': '白米饭', 'calories': 116}],
        'daily_steps': 0
    }
    r = post('/calorie/api/record', body)
    rec_upd = r.get_json()['record']
    # bmr = 10*61/2 + 1056.25 - 145 + 5 = 1221.25
    check('更新后 bmr 重算', num_eq(rec_upd['bmr'], 1221.25), str(rec_upd['bmr']))
    check('更新保留原 created_at', rec_upd['created_at'] == created_before,
          f"{rec_upd['created_at']} vs {created_before}")

    print('\n=== 8. 删除记录 ===')
    r = client.delete('/calorie/api/record/2026-08-03')
    check('删除成功', r.status_code == 200 and r.get_json()['success'] is True)
    r = client.get('/calorie/api/records')
    recs = r.get_json()['records']
    check('删除后剩 2 条', len(recs) == 2)
    r = client.delete('/calorie/api/record/1999-01-01')
    check('删除不存在返回 404', r.status_code == 404)

    print('\n=== 9. 食物库增删改 ===')
    r = post('/calorie/api/food', {'name': 'SmokeTestFood', 'unit': '1份',
                                   'calories': 99, 'category': '测试'})
    data = r.get_json()
    check('新增食物成功', data['success'] is True)
    fid = data['food']['id']
    check('新食物 id 顺延不复用', fid == 'food_119', fid)  # 115种子+甲乙丙=118，下一个119
    r = post('/calorie/api/food', {'name': 'smoketestfood', 'calories': 1})
    check('重名(大小写不敏感)返回 409', r.status_code == 409)
    r = post('/calorie/api/food', {'name': '', 'calories': 1})
    check('空名称返回 400', r.status_code == 400)
    r = put(f'/calorie/api/food/{fid}', {'name': '冒烟测试食物甲'})
    check('改名撞已有食物返回 409', r.status_code == 409)
    r = put(f'/calorie/api/food/{fid}', {'calories': 123, 'category': '零食'})
    data = r.get_json()
    check('更新食物字段成功', data['success'] is True and num_eq(data['food']['calories'], 123)
          and data['food']['category'] == '零食')
    r = put('/calorie/api/food/food_999', {'calories': 1})
    check('更新不存在食物返回 404', r.status_code == 404)
    r = client.delete(f'/calorie/api/food/{fid}')
    check('删除食物成功', r.status_code == 200)
    r = client.delete(f'/calorie/api/food/{fid}')
    check('重复删除返回 404', r.status_code == 404)

    print('\n=== 10. 配置更新触发全量重算 ===')
    r = client.get('/calorie/api/config')
    cfg = r.get_json()['config']
    check('GET 配置', num_eq(cfg['height'], 169))
    r = put('/calorie/api/config', {'height': 170, 'target_deficit': 0})
    data = r.get_json()
    check('PUT 配置成功', data['success'] is True)
    check('非法 target_deficit(<=0) 被忽略', num_eq(data['config']['target_deficit'], 100000))
    check('配置已更新 height=170', num_eq(data['config']['height'], 170))
    r = client.get('/calorie/api/records')
    recs = {x['id']: x for x in r.get_json()['records']}
    # height=170 → bmr = 10*61/2 + 6.25*170 - 5*29 + 5 = 1227.5
    check('记录 bmr 随配置重算', num_eq(recs['2026-08-01']['bmr'], 1227.5),
          str(recs['2026-08-01']['bmr']))
    # 恢复原配置，避免恢复快照前残留（快照恢复也会覆盖，这里仅为语义完整）
    put('/calorie/api/config', {'height': 169})

    print('\n=== 11. 数据恢复 ===')
finally:
    _restore(_SNAP)
    with session_scope() as s:
        n_foods = len(s.execute(select(CalorieFood.id)).scalars().all())
        n_recs = len(s.execute(select(CalorieRecord.id)).scalars().all())
    ok_restore = (n_foods == len(_SNAP[0]) and n_recs == len(_SNAP[1]))
    print(f"\n[Smoke] 快照恢复{'成功' if ok_restore else '❌ 失败'}"
          f"（食物 {n_foods}/{len(_SNAP[0])}，记录 {n_recs}/{len(_SNAP[1])}）")

print(f"\n{'=' * 50}\n冒烟测试结果: {PASS} 通过 / {FAIL} 失败\n{'=' * 50}")
sys.exit(1 if FAIL or not ok_restore else 0)
