#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务计划模块数据访问层（MySQL 版）
====================================
替代原 plan_routes.py 中的 _load_plans/_save_plans（JSON 整文件读写）。

对外语义与原 JSON 版完全一致：
- load_plans_data(session)  → {'initialized': bool, 'plans': [...]}（整树）
- save_plans_data(session, data) ← 整树写回

内部实现为差量同步（非全表重写）：
- plans/cards 按 id upsert，消失的删除（外键级联清理子表）
- slots 按 (card_id, slot_index) 逐格比对，仅写入发生变化的格子
  （单次打卡只 UPDATE 1 行，避免整卡 100 格重写）

存储约定：
- sort_order 保持 plans/cards 原数组顺序
- daily_rule/round_config/milestones/todos/notes/settlement 为 JSON 文本列
- slot.record 拆列存储，has_record 区分 None 与 {}
- initialized 标记存 kv_store（key='task_plans_initialized'），
  保证"删光全部计划"后不会像空表那样重建默认计划
"""

import json
import logging

from sqlalchemy import select

from .models import PlanPlan, PlanCard, PlanSlot, KVStore

logger = logging.getLogger(__name__)

_INITIALIZED_KEY = 'task_plans_initialized'

_SLOT_RECORD_COLUMNS = (
    'filled', 'filled_at', 'has_record', 'content', 'duration_minutes',
    'prediction', 'actual', 'hit', 'market_analysis', 'action_advice',
    'account_balance', 'analysis_ids', 'analysis_hour', 'bypass_analysis'
)


def _json_loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# =============================================================================
# 读取：DB → 整树 dict
# =============================================================================

def _record_from_row(row, card_type):
    """slot 行 → record dict（None 表示空格子；按卡类型还原字段集）"""
    if not row.has_record:
        return None
    if card_type == 'trade':
        record = {
            'prediction': row.prediction or '',
            'duration_minutes': row.duration_minutes or 0,
            'actual': row.actual or '',
            'market_analysis': row.market_analysis or '',
            'action_advice': row.action_advice or '',
            'account_balance': row.account_balance or '',
            # 分析纪律（批次11）：本次打卡依据的分析记录（库内逗号串 ↔ 对外 id 数组）
            'analysis_ids': _parse_analysis_ids(row.analysis_ids),
            'analysis_hour': row.analysis_hour or '',
            'bypass_analysis': bool(row.bypass_analysis),
        }
        if row.hit is not None:
            record['hit'] = bool(row.hit)
        return record
    return {
        'content': row.content or '',
        'duration_minutes': row.duration_minutes or 0
    }


def _card_to_dict(row, slots_by_index):
    card = {
        'id': row.id,
        'type': row.type,
        'round': row.round,
        'title': row.title,
        'goal': row.goal,
        'reward': row.reward,
        'start_time': row.start_time,
        'end_time': row.end_time,
        'status': row.status,
        'milestones': _json_loads(row.milestones, []),
        'slots': [
            {
                'slot_index': idx,
                'filled': bool(s.filled),
                'filled_at': s.filled_at or '',
                'record': _record_from_row(s, row.type)
            }
            for idx, s in sorted(slots_by_index.items())
        ],
        'notes': _json_loads(row.notes, []),
        'review': row.review or '',
        'settlement': _json_loads(row.settlement, None),
        'created_at': row.created_at or '',
        'updated_at': row.updated_at or ''
    }
    if row.base_reward is not None:
        card['base_reward'] = row.base_reward
    if row.hourly_rate is not None:
        card['hourly_rate'] = row.hourly_rate
    todos = _json_loads(row.todos, [])
    if todos:
        card['todos'] = todos
    return card


def _plan_to_dict(row, cards):
    plan = {
        'id': row.id,
        'type': row.type,
        'name': row.name,
        'grand_goal': row.grand_goal or '',
        'total_hours': row.total_hours,
        'round_count': row.round_count,
        'per_round_hours': row.per_round_hours,
        'daily_rule': _json_loads(row.daily_rule, {}),
        'cards': cards,
        'created_at': row.created_at or ''
    }
    if row.round_config is not None:
        plan['round_config'] = _json_loads(row.round_config, None)
    return plan


def load_plans_data(session):
    """整树读取；未初始化（无标记且表为空）时返回 initialized=False

    性能约定：全树仅 3 条 SQL（plans/cards/slots 各一条全表查询）
    + 内存分组，避免逐卡查询在远程 MySQL 上的大量网络往返。
    """
    kv = session.get(KVStore, _INITIALIZED_KEY)
    plan_rows = session.execute(
        select(PlanPlan).order_by(PlanPlan.sort_order)).scalars().all()
    if kv is None and not plan_rows:
        return {'initialized': False, 'plans': []}

    # 一次性拉取全部 cards/slots，内存按外键分组
    cards_by_plan = {}
    for c in session.execute(
            select(PlanCard).order_by(PlanCard.sort_order)).scalars().all():
        cards_by_plan.setdefault(c.plan_id, []).append(c)
    slots_by_card = {}
    for s in session.execute(
            select(PlanSlot).order_by(PlanSlot.slot_index)).scalars().all():
        slots_by_card.setdefault(s.card_id, {})[s.slot_index] = s

    plans = []
    for p in plan_rows:
        cards = []
        for c in cards_by_plan.get(p.id, []):
            cards.append(_card_to_dict(c, slots_by_card.get(c.id, {})))
        plans.append(_plan_to_dict(p, cards))
    return {'initialized': True, 'plans': plans}


# =============================================================================
# 写入：整树 dict → DB（差量同步）
# =============================================================================

def _parse_analysis_ids(text):
    """逗号串 → id 整数数组（非法段静默丢弃）"""
    return [int(x) for x in str(text or '').split(',') if x.strip().isdigit()]


def _format_analysis_ids(value) -> str:
    """id 数组/逗号串 → 库内逗号串（上限 255 字符，超出部分丢弃）"""
    if isinstance(value, str):
        parts = [x.strip() for x in value.split(',')]
    elif isinstance(value, (list, tuple)):
        parts = [str(x).strip() for x in value]
    else:
        parts = []
    ids = [p for p in parts if p.isdigit()]
    return ','.join(ids)[:255]


def _slot_values(slot):
    """slot dict → plan_slots 列值 dict（record 拆平）"""
    record = slot.get('record')
    vals = {
        'filled': bool(slot.get('filled')),
        'filled_at': str(slot.get('filled_at') or ''),
        'has_record': record is not None,
        'content': '',
        'duration_minutes': 0,
        'prediction': '',
        'actual': '',
        'hit': None,
        'market_analysis': '',
        'action_advice': '',
        'account_balance': '',
        'analysis_ids': '',
        'analysis_hour': '',
        'bypass_analysis': False
    }
    if isinstance(record, dict):
        vals['content'] = str(record.get('content', '') or '')
        vals['duration_minutes'] = int(record.get('duration_minutes', 0) or 0)
        vals['prediction'] = str(record.get('prediction', '') or '')[:8]
        vals['actual'] = str(record.get('actual', '') or '')[:8]
        if 'hit' in record:
            vals['hit'] = bool(record['hit'])
        vals['market_analysis'] = str(record.get('market_analysis', '') or '')
        vals['action_advice'] = str(record.get('action_advice', '') or '')
        vals['account_balance'] = str(record.get('account_balance', '') or '')[:32]
        vals['analysis_ids'] = _format_analysis_ids(record.get('analysis_ids'))
        vals['analysis_hour'] = str(record.get('analysis_hour', '') or '')[:13]
        vals['bypass_analysis'] = bool(record.get('bypass_analysis'))
    return vals


def _sync_slots(session, card_id, slots, existing_slots=None):
    """同步单张卡的格子（差量：新增/修改/删除）。

    existing_slots（{slot_index: row}）由调用方预取传入时不再查库；
    未传入时回退为按 card_id 查询（兼容单独调用）。
    """
    if existing_slots is None:
        existing_slots = {s.slot_index: s for s in session.execute(
            select(PlanSlot).where(PlanSlot.card_id == card_id)).scalars().all()}
    existing = existing_slots
    keep = set()
    for slot in slots or []:
        idx = slot.get('slot_index')
        if idx is None:
            continue
        keep.add(idx)
        vals = _slot_values(slot)
        row = existing.get(idx)
        if row is None:
            session.add(PlanSlot(card_id=card_id, slot_index=idx, **vals))
        else:
            for col, val in vals.items():
                if getattr(row, col) != val:
                    setattr(row, col, val)
    for idx, row in existing.items():
        if idx not in keep:
            session.delete(row)
    session.flush()


def _sync_cards(session, plan_id, cards, existing_cards=None, slots_by_card=None):
    """同步单个计划下的卡片列表（差量：新增/修改/删除），并递归同步格子。

    existing_cards（{card_id: row}）/ slots_by_card（{card_id: {slot_index: row}}）
    由调用方预取传入时直接使用，消除逐计划/逐卡查询；未传入时回退为
    按 plan_id/card_id 查询。卡片/格子均为客户端生成的字符串 ID，
    无需逐卡 flush 获取自增主键，统一由末尾 flush 按外键顺序批量提交。
    """
    if existing_cards is None:
        existing_cards = {c.id: c for c in session.execute(
            select(PlanCard).where(PlanCard.plan_id == plan_id)).scalars().all()}
    existing = existing_cards
    keep = set()
    for idx, c in enumerate(cards or []):
        cid = c.get('id', '')
        if not cid:
            continue
        keep.add(cid)
        row = existing.get(cid)
        if row is None:
            row = PlanCard(id=cid, plan_id=plan_id)
            session.add(row)
        row.sort_order = idx
        row.type = c.get('type', 'learn')
        row.round = int(c.get('round', 0) or 0)
        row.title = c.get('title', '') or ''
        row.goal = c.get('goal', '') or ''
        row.reward = int(c.get('reward', 0) or 0)
        row.base_reward = int(c['base_reward']) if c.get('base_reward') is not None else None
        row.hourly_rate = int(c['hourly_rate']) if c.get('hourly_rate') is not None else None
        row.status = c.get('status', 'pending')
        row.start_time = c.get('start_time', '') or ''
        row.end_time = c.get('end_time', '') or ''
        row.milestones = json.dumps(c.get('milestones') or [], ensure_ascii=False)
        row.todos = json.dumps(c.get('todos') or [], ensure_ascii=False)
        row.notes = json.dumps(c.get('notes') or [], ensure_ascii=False)
        row.review = c.get('review', '') or ''
        settlement = c.get('settlement')
        row.settlement = json.dumps(settlement, ensure_ascii=False) if settlement is not None else None
        row.created_at = c.get('created_at', '') or ''
        row.updated_at = c.get('updated_at', '') or ''
        _sync_slots(session, cid, c.get('slots'),
                    existing_slots=(slots_by_card or {}).get(cid)
                    if slots_by_card is not None else None)
    for cid, row in existing.items():
        if cid not in keep:
            session.delete(row)  # 级联删除 slots
    session.flush()


def save_plans_data(session, data):
    """整树写回（差量同步）；同时维护 initialized 标记。

    性能约定：写阶段无论计划/卡片数量多少，仅 3 条 SELECT（plans/cards/slots
    各一次全表预取 + 内存分组），其余为差量 INSERT/UPDATE/DELETE，避免逐计划/
    逐卡查询在远端 MySQL 上产生大量串行网络往返（打卡慢的主因）。
    """
    kv = session.get(KVStore, _INITIALIZED_KEY)
    if kv is None:
        session.add(KVStore(key=_INITIALIZED_KEY,
                            value='true' if data.get('initialized', True) else 'false'))
    else:
        kv.value = 'true' if data.get('initialized', True) else 'false'
    session.flush()

    plans = data.get('plans', [])
    existing = {p.id: p for p in session.execute(select(PlanPlan)).scalars().all()}
    # 一次性预取全部 cards/slots，内存按计划/卡片分组，供差量同步直接使用
    cards_by_plan = {}
    for c in session.execute(select(PlanCard)).scalars().all():
        cards_by_plan.setdefault(c.plan_id, {})[c.id] = c
    slots_by_card = {}
    for s in session.execute(select(PlanSlot)).scalars().all():
        slots_by_card.setdefault(s.card_id, {})[s.slot_index] = s
    keep = set()
    for idx, p in enumerate(plans):
        pid = p.get('id', '')
        if not pid:
            continue
        keep.add(pid)
        row = existing.get(pid)
        if row is None:
            row = PlanPlan(id=pid)
            session.add(row)
        row.sort_order = idx
        row.type = p.get('type', 'custom')
        row.name = p.get('name', '') or ''
        row.grand_goal = p.get('grand_goal', '') or ''
        row.total_hours = int(p.get('total_hours', 100) or 0)
        row.round_count = int(p.get('round_count', 1) or 0)
        row.per_round_hours = int(p.get('per_round_hours', 100) or 0)
        row.daily_rule = json.dumps(p.get('daily_rule') or {}, ensure_ascii=False)
        round_config = p.get('round_config')
        row.round_config = json.dumps(round_config, ensure_ascii=False) if round_config is not None else None
        row.created_at = p.get('created_at', '') or ''
        _sync_cards(session, pid, p.get('cards'),
                    existing_cards=cards_by_plan.get(pid, {}),
                    slots_by_card=slots_by_card)
    for pid, row in existing.items():
        if pid not in keep:
            session.delete(row)  # 级联删除 cards/slots
    session.flush()
