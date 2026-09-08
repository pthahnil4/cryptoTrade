#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热量模块数据访问层（MySQL 版）
================================
替代原 calorie_routes.py 中的 _load_records/_save_records/_load_food_db/_save_food_db。
业务语义与 JSON 版逐条对齐：
- 食物名不区分大小写查重（依赖 utf8mb4_general_ci 的 CI 排序规则）
- 新食物 id 生成规则 food_{现有最大序号+1:03d}
- 记录按日期排序重算 cumulative_deficit
- 配置单行表，无行时返回代码内默认值
"""

import datetime
import logging

from sqlalchemy import select, delete, func, update, case

from .models import CalorieFood, CalorieRecord, CalorieMealItem, CalorieConfig

logger = logging.getLogger(__name__)

# 与 JSON 版 _DEFAULT_CONFIG 一致的默认配置
DEFAULT_CONFIG = {
    'height': 169,
    'age': 29,
    'step_frequency': 0.7,
    'weight_factor': 55,
    'target_deficit': 100000
}

_MEALS = ('breakfast', 'lunch', 'dinner')


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


# =============================================================================
# 配置（单行表）
# =============================================================================

def load_config(session):
    """读取配置 dict；无行时返回默认配置（对齐 JSON 版缺 config 时的兜底）"""
    row = session.get(CalorieConfig, 1)
    if row is None:
        return dict(DEFAULT_CONFIG)
    return row.to_dict()


def save_config(session, config):
    """整体写入配置（UPSERT 单行）"""
    row = session.get(CalorieConfig, 1)
    if row is None:
        row = CalorieConfig(id=1)
        session.add(row)
    for key in ('height', 'age', 'step_frequency', 'weight_factor', 'target_deficit'):
        if key in config:
            setattr(row, key, float(config[key]))
    session.flush()


# =============================================================================
# 每日记录
# =============================================================================

def _record_to_dict(session, row):
    """记录行 -> 与 JSON 版结构一致的 dict（含三餐食物明细数组）"""
    items = session.execute(
        select(CalorieMealItem)
        .where(CalorieMealItem.record_id == row.id)
        .order_by(CalorieMealItem.meal, CalorieMealItem.position)
    ).scalars().all()
    meals = {m: [] for m in _MEALS}
    for it in items:
        if it.meal in meals:
            meals[it.meal].append({'name': it.name, 'calories': it.calories})
    return {
        'id': row.id,
        'date': row.date,
        'morning_weight': row.morning_weight,
        'evening_weight': row.evening_weight,
        'bmr': row.bmr,
        'breakfast_foods': meals['breakfast'],
        'breakfast_food': row.breakfast_food,
        'breakfast_calories': row.breakfast_calories,
        'lunch_foods': meals['lunch'],
        'lunch_food': row.lunch_food,
        'lunch_calories': row.lunch_calories,
        'dinner_foods': meals['dinner'],
        'dinner_food': row.dinner_food,
        'dinner_calories': row.dinner_calories,
        'intake_deficit': row.intake_deficit,
        'daily_steps': row.daily_steps,
        'exercise_calories': row.exercise_calories,
        'calorie_deficit': row.calorie_deficit,
        'cumulative_deficit': row.cumulative_deficit,
        'created_at': row.created_at.strftime('%Y-%m-%d %H:%M:%S') if row.created_at else '',
        'updated_at': row.updated_at.strftime('%Y-%m-%d %H:%M:%S') if row.updated_at else ''
    }


def load_records(session, order_by_date_asc=True):
    """全部记录（默认按日期升序，对齐 JSON 版 _recalc_cumulative 的排序语义）"""
    order = CalorieRecord.date.asc() if order_by_date_asc else CalorieRecord.date.desc()
    rows = session.execute(select(CalorieRecord).order_by(order)).scalars().all()
    return [_record_to_dict(session, r) for r in rows]


def get_record(session, record_id):
    row = session.get(CalorieRecord, record_id)
    return _record_to_dict(session, row) if row else None


def upsert_record(session, record):
    """新增或整行更新一条记录（含三餐明细重建）；保留原 created_at"""
    row = session.get(CalorieRecord, record['id'])
    created_at = row.created_at if row else _parse_ts(record.get('created_at'))
    if row is None:
        row = CalorieRecord(id=record['id'])
        session.add(row)

    row.date = record.get('date', record['id'])
    row.morning_weight = record.get('morning_weight')
    row.evening_weight = record.get('evening_weight')
    row.bmr = record.get('bmr', 0) or 0
    row.breakfast_food = record.get('breakfast_food', '') or ''
    row.breakfast_calories = record.get('breakfast_calories', 0) or 0
    row.lunch_food = record.get('lunch_food', '') or ''
    row.lunch_calories = record.get('lunch_calories', 0) or 0
    row.dinner_food = record.get('dinner_food', '') or ''
    row.dinner_calories = record.get('dinner_calories', 0) or 0
    row.intake_deficit = record.get('intake_deficit', 0) or 0
    row.daily_steps = int(record.get('daily_steps', 0) or 0)
    row.exercise_calories = record.get('exercise_calories', 0) or 0
    row.calorie_deficit = record.get('calorie_deficit', 0) or 0
    row.cumulative_deficit = record.get('cumulative_deficit', 0) or 0
    row.created_at = created_at or datetime.datetime.now()
    row.updated_at = _parse_ts(record.get('updated_at')) or datetime.datetime.now()
    session.flush()

    # 重建三餐明细
    session.execute(delete(CalorieMealItem).where(CalorieMealItem.record_id == row.id))
    for meal in _MEALS:
        for pos, item in enumerate(record.get(f'{meal}_foods') or []):
            session.add(CalorieMealItem(
                record_id=row.id, meal=meal, position=pos,
                name=str(item.get('name', '') or '')[:64],
                calories=float(item.get('calories', 0) or 0)))
    session.flush()


def delete_record(session, record_id):
    """删除记录（明细由外键级联清理）；返回是否存在并删除"""
    row = session.get(CalorieRecord, record_id)
    if row is None:
        return False
    session.execute(delete(CalorieMealItem).where(CalorieMealItem.record_id == record_id))
    session.delete(row)
    session.flush()
    return True


def update_record_fields(session, record_id, **fields):
    """批量更新记录的指定列（用于配置变更后重算 bmr/缺口/累计值）"""
    row = session.get(CalorieRecord, record_id)
    if row is None:
        return None
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    return row


def bulk_update_records(session, updates):
    """批量更新多条记录，合并为一条 UPDATE...CASE SQL 一次发出。

    updates: [{'id': 记录id, 列名: 新值, ...}, ...]
    用于保存/删除/配置变更后全量重算回写：避免逐条 UPDATE 在远端 MySQL
    上产生 N 次串行网络往返（N 条记录×跨地域 RTT 是热量页保存卡顿的主因）。
    """
    if not updates:
        return
    ids = [u['id'] for u in updates]
    cols = set()
    for u in updates:
        cols.update(k for k in u.keys() if k != 'id')
    values = {}
    for col in cols:
        if not hasattr(CalorieRecord, col):
            continue
        values[col] = case(
            *[(CalorieRecord.id == u['id'], u[col]) for u in updates if col in u])
    if not values:
        return
    session.execute(
        update(CalorieRecord)
        .where(CalorieRecord.id.in_(ids))
        .values(**values))
    session.flush()


# =============================================================================
# 食物热量库
# =============================================================================

def load_foods(session):
    """全部食物（按库内插入顺序：id 升序，与 JSON 数组顺序一致）"""
    rows = session.execute(select(CalorieFood).order_by(CalorieFood.id)).scalars().all()
    return [f.to_dict() for f in rows]


def get_food(session, food_id):
    row = session.get(CalorieFood, food_id)
    return row.to_dict() if row else None


def find_food_by_name(session, name):
    """按名称查找（不区分大小写，依赖 utf8mb4_general_ci）"""
    row = session.execute(
        select(CalorieFood).where(CalorieFood.name == (name or '').strip())
    ).scalars().first()
    return row.to_dict() if row else None


def find_food_by_name_excluding(session, name, exclude_id):
    """按名称查找但排除指定 id（更新时查重用）"""
    row = session.execute(
        select(CalorieFood).where(
            CalorieFood.name == (name or '').strip(),
            CalorieFood.id != exclude_id)
    ).scalars().first()
    return row.to_dict() if row else None


def next_food_id(session):
    """新食物 id：food_{现有最大序号+1:03d}（对齐 JSON 版生成规则，删除后不复用缺口前序号）"""
    max_num = 0
    ids = session.execute(select(CalorieFood.id)).scalars().all()
    for fid in ids:
        try:
            max_num = max(max_num, int(str(fid).split('_')[-1]))
        except (ValueError, IndexError):
            continue
    return f'food_{max_num + 1:03d}'


def add_food(session, food):
    """新增食物（food 为含 id/name/unit/calories/category/created_at 的 dict）"""
    session.add(CalorieFood(
        id=food['id'],
        name=food['name'],
        unit=food.get('unit', '100克'),
        calories=float(food.get('calories', 0) or 0),
        category=food.get('category', '其他'),
        created_at=_parse_ts(food.get('created_at'))))
    session.flush()


def update_food_fields(session, food_id, **fields):
    """更新食物指定字段；返回更新后 dict 或 None"""
    row = session.get(CalorieFood, food_id)
    if row is None:
        return None
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.flush()
    return row.to_dict()


def delete_food(session, food_id):
    row = session.get(CalorieFood, food_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
