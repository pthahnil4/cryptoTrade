#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：实盘分析记录（批次10）— 建表 + CRUD + 复盘回填 + 统计

运行：python crypto/_smoke_analysis_record.py
测试数据自清理：脚本结束删除本次插入的全部记录。
"""

import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto.database import get_engine, session_scope, Base
from crypto import models  # noqa: F401 注册模型
from crypto import analysis_record_repo as repo


def main():
    # 1. 建表（checkfirst，已存在则跳过）
    engine = get_engine()
    models.TaskAnalysisRecord.__table__.create(engine, checkfirst=True)
    print('[1] 建表完成（task_analysis_records）')

    now = datetime.datetime.now()
    ts_2h_ago = (now - datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
    ts_now = now.strftime('%Y-%m-%d %H:%M:%S')
    inserted_ids = []

    with session_scope() as s:
        # 2. 新增：一条 2 小时前的记录（用于回填）+ 一条当前记录
        rid1 = repo.add_record(s, {
            'ts': ts_2h_ago, 'inst_id': 'BTC-USDT-SWAP', 'price': 100000.0,
            'short_period': '5m', 'long_period': '4H',
            'short_dir': 'long', 'long_dir': 'long', 'long_dir_prev': 'long',
            'atr_pct': 0.35, 'user_judgment': 'rise', 'user_reason': '冒烟测试-看涨',
        })
        rid2 = repo.add_record(s, {
            'ts': ts_now, 'inst_id': 'BTC-USDT-SWAP', 'price': 5.123,
            'short_period': '5m', 'long_period': '4H',
            'short_dir': 'short', 'long_dir': 'short', 'long_dir_prev': None,
            'atr_pct': 1.2, 'user_judgment': 'watch', 'user_reason': '',
        })
        inserted_ids = [rid1, rid2]
    print(f'[2] 新增记录成功: id={inserted_ids}')

    with session_scope() as s:
        rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        assert any(r['id'] == rid1 for r in rows), '查询未返回新记录'
        assert rows[0]['ts'] >= rows[-1]['ts'], 'ts 降序不成立'
    print('[3] 查询正常（ts 降序，共 %d 条匹配）' % len(rows))

    # 4. 修改个人判断/原因
    with session_scope() as s:
        ok = repo.update_user_fields(s, rid1, 'fall', '冒烟测试-改为看跌')
        assert ok
    with session_scope() as s:
        rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        rec1 = next(r for r in rows if r['id'] == rid1)
        assert rec1['user_judgment'] == 'fall' and rec1['user_reason'] == '冒烟测试-改为看跌'
    print('[4] 修改判断/原因正常')

    # 5. 复盘回填（2小时前的记录应回填 1H，4H 未到窗口保持空）
    #    依赖 OKX K线网络；失败时仅提示不视为致命错误
    try:
        with session_scope() as s:
            filled = repo.backfill_due_reviews(s)
        with session_scope() as s:
            rows = repo.query_records(s, inst_id='BTC-USDT-SWAP')
        rec1 = next(r for r in rows if r['id'] == rid1)
        print(f'[5] 回填字段数={filled} | price_1h={rec1["price_1h"]} ts_1h={rec1["ts_1h"]} '
              f'price_4h={rec1["price_4h"]}（应为 None）')
        assert rec1['price_4h'] is None, '4H 未到窗口不应回填'
        if rec1['price_1h'] is None:
            print('    [5!] 1H 未回填（多为网络/K线不可达），回填逻辑未实网验证')
    except Exception as e:
        print(f'[5!] 回填异常（非致命，可能为网络问题）: {e}')

    # 6. 统计口径验证（三分类中性带 + 混淆矩阵；atr_pct=1.0 → θ近=0.3%、θ远≈0.424%）
    demo = [
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'rise', 'long_dir_prev': 'long',
         'long_dir': 'long', 'price_1h': 110.0, 'price_4h': 90.0},   # 近涨(判对)/远跌(判错)
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'fall', 'long_dir_prev': None,
         'long_dir': 'short', 'price_1h': 95.0, 'price_4h': None},    # 近跌判对；策略空命中
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'watch', 'long_dir_prev': 'long',
         'long_dir': 'long', 'price_1h': 100.1, 'price_4h': None},    # 近横盘观望判对；策略多遇横盘→统一口径计为判错
        {'price': 100.0, 'atr_pct': 1.0, 'user_judgment': 'watch', 'long_dir_prev': 'short',
         'long_dir': 'short', 'price_1h': 105.0, 'price_4h': None},   # 近涨观望判错；策略空未命中
    ]
    st = repo.compute_stats(demo)
    # 个人：rise/fall/watch 三分类全计分（观望命中横盘）
    assert st['user']['near']['total'] == 4, 'watch 应计入个人三分类统计'
    assert st['user']['near']['hit'] == 3 and st['user']['near']['rate'] == 75.0
    assert st['user']['far']['total'] == 1 and st['user']['far']['hit'] == 0
    # 策略：与个人统一口径，横盘也计入分母（第3条横盘 → total=4、该条判错）
    assert st['strategy']['near']['total'] == 4, '横盘对策略方向应统一计入分母'
    assert st['strategy']['near']['hit'] == 2 and st['strategy']['near']['rate'] == 50.0  # 多涨命中、空跌命中、多横未中、空涨未中
    # 混淆矩阵：预测 × 实际
    assert st['confusion']['near']['rise']['up'] == 1
    assert st['confusion']['near']['watch']['flat'] == 1 and st['confusion']['near']['watch']['up'] == 1
    assert st['confusion']['near']['fall']['down'] == 1
    assert st['confusion']['far']['rise']['down'] == 1
    # 中性带参数回传（前端本地重算据此对齐口径）
    assert st['hit_k'] == repo.HIT_ATR_K and st['hit_floor_pct'] == repo.HIT_FLOOR_PCT
    print(f'[6] 三分类统计口径正确: user.near={st["user"]["near"]} strategy.near={st["strategy"]["near"]}')

    # 7. 删除测试数据
    with session_scope() as s:
        for rid in inserted_ids:
            assert repo.delete_record(s, rid)
        assert not repo.delete_record(s, 999999999), '不存在的记录应返回 False'
    print('[7] 删除正常，测试数据已清理')

    # 8. 批量删除（delete_records）：新增 3 条一次性删除，混合不存在 id 验证计数
    batch_ids = []
    with session_scope() as s:
        for i in range(3):
            batch_ids.append(repo.add_record(s, {
                'ts': ts_now, 'inst_id': 'SMOKE-BATCH-USDT-SWAP', 'price': 1.0 + i,
                'short_period': '5m', 'long_period': '4H',
                'short_dir': 'long', 'long_dir': 'long', 'long_dir_prev': None,
                'atr_pct': 0.5, 'user_judgment': 'watch', 'user_reason': '冒烟-批量删除',
            }))
    with session_scope() as s:
        deleted = repo.delete_records(s, batch_ids + [999999998])  # 含 1 个不存在 id
        assert deleted == 3, f'批量删除应删除 3 条，实际 {deleted}'
    with session_scope() as s:
        left = repo.query_records(s, inst_id='SMOKE-BATCH-USDT-SWAP')
        assert not left, '批量删除后仍残留记录'
    with session_scope() as s:
        assert repo.delete_records(s, []) == 0, '空 id 列表应返回 0 且不报错'
        assert repo.delete_records(s, [999999997]) == 0, '全部不存在 id 应返回 0'
    print('[8] 批量删除正常（含不存在 id 自动忽略、空列表安全）')

    print('\n===== 冒烟测试通过 =====')


if __name__ == '__main__':
    main()
