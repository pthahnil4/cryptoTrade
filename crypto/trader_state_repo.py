#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易运行时状态 - 数据访问层（迁移批次5）
==========================================
替代 5 个调度器状态 JSON，语义与 JSON 版完全一致：

    scheduler_state.json        → trader_directions   整树读写
    reverse_guard_state.json    → reverse_guard       整树读写
    manual_pause_state.json     → manual_pause        整树读写
    tp_runtime_state.json       → tp_runtime_state    按 key upsert/delete
    position_order_state.json   → pos_book/pos_slot/pos_algo/pos_lev
                                  按 inst_id 整段重写（单次落库 ≤9 行）

round-trip 约定（迁移校验用）：从 DB 重组的 dict 与源 JSON 逐字段相等：
- slot 的 qfail_logged 仅 True 时输出该键（对齐 JSON 版临时字段形态）
- algo 恒输出 {'long':..,'short':..} 双键（值为 rec dict 或 None）
- lev_set 行不存在 → None；行存在 → dict（两列均空即空 dict {}）
"""

import json

from sqlalchemy import select, delete

from .models import (
    TraderDirection, ReverseGuard, ManualPause, TpRuntimeState,
    PosBook, PosSlot, PosAlgo, PosLev)

_BUCKETS = ('trend', 'range')
_SLOTS = ('entry', 'exit')
_DIRECTIONS = ('long', 'short')

_SLOT_FIELDS = ('state', 'ord_id', 'price', 'amount', 'placed_ts',
                'acc_filled', 'dir')


# =============================================================================
# 方向记录 / 反向风控 / 人工冷却（整树读写，数据量极小）
# =============================================================================

def load_directions(session) -> dict:
    rows = session.execute(select(TraderDirection)).scalars().all()
    return {r.inst_id: {'short': r.short_dir, 'long': r.long_dir} for r in rows}


def save_directions(session, data: dict):
    session.execute(delete(TraderDirection))
    for inst_id, dirs in (data or {}).items():
        session.add(TraderDirection(
            inst_id=inst_id,
            short_dir=str((dirs or {}).get('short') or ''),
            long_dir=str((dirs or {}).get('long') or '')))


def load_reverse_guard(session) -> dict:
    rows = session.execute(select(ReverseGuard)).scalars().all()
    return {r.inst_id: {'detected_ts': r.detected_ts,
                        'long_direction': r.long_direction,
                        'reverse_side': r.reverse_side,
                        'reverse_mode': r.reverse_mode,
                        'reverse_amount': r.reverse_amount,
                        'warned': bool(r.warned)} for r in rows}


def save_reverse_guard(session, data: dict):
    session.execute(delete(ReverseGuard))
    for inst_id, g in (data or {}).items():
        g = g or {}
        session.add(ReverseGuard(
            inst_id=inst_id,
            detected_ts=float(g.get('detected_ts', 0) or 0),
            long_direction=str(g.get('long_direction') or ''),
            reverse_side=str(g.get('reverse_side') or ''),
            reverse_mode=str(g.get('reverse_mode') or ''),
            reverse_amount=float(g.get('reverse_amount', 0) or 0),
            warned=bool(g.get('warned', False))))


def load_manual_pause(session) -> dict:
    rows = session.execute(select(ManualPause)).scalars().all()
    return {r.inst_id: r.resume_ts for r in rows}


def save_manual_pause(session, data: dict):
    session.execute(delete(ManualPause))
    for inst_id, ts in (data or {}).items():
        session.add(ManualPause(inst_id=inst_id, resume_ts=float(ts or 0)))


# =============================================================================
# 止盈引擎运行时状态（按 key upsert/delete，evaluate 高频调用）
# =============================================================================

def load_tp_state(session) -> dict:
    rows = session.execute(select(TpRuntimeState)).scalars().all()
    out = {}
    for r in rows:
        try:
            ladder = json.loads(r.ladder_done) if r.ladder_done else []
        except (TypeError, ValueError):
            ladder = []
        out[r.state_key] = {'entry_ts': r.entry_ts, 'peak': r.peak,
                            'trough': r.trough, 'avg_px': r.avg_px,
                            'ladder_done': ladder}
    return out


def upsert_tp_state(session, key: str, st: dict):
    row = session.get(TpRuntimeState, key)
    if row is None:
        row = TpRuntimeState(state_key=key)
        session.add(row)
    row.entry_ts = float(st.get('entry_ts', 0) or 0)
    row.peak = float(st.get('peak', 0) or 0)
    row.trough = float(st.get('trough', 0) or 0)
    row.avg_px = float(st.get('avg_px', 0) or 0)
    row.ladder_done = json.dumps(st.get('ladder_done') or [])


def delete_tp_state(session, key: str):
    session.execute(delete(TpRuntimeState).where(TpRuntimeState.state_key == key))


# =============================================================================
# 双仓位本地账本（position_order_state.json 拆 4 表）
# =============================================================================

def _slot_from_row(row) -> dict:
    d = {k: getattr(row, k) for k in _SLOT_FIELDS}
    if row.qfail_logged:
        d['qfail_logged'] = True
    return d


def _algo_from_row(row) -> dict:
    return {'algo_id': row.algo_id, 'amount': row.amount,
            'sl': row.sl, 'tp': row.tp, 'ts': row.ts}


def load_position_state(session) -> dict:
    """整树读取：4 条全表 SQL + 内存分组，重组为原 JSON 嵌套结构"""
    books = session.execute(select(PosBook)).scalars().all()
    slots = session.execute(select(PosSlot)).scalars().all()
    algos = session.execute(select(PosAlgo)).scalars().all()
    levs = session.execute(select(PosLev)).scalars().all()

    slots_map = {}
    for r in slots:
        slots_map.setdefault((r.inst_id, r.bucket), {})[r.slot] = r
    algos_map = {}
    for r in algos:
        algos_map.setdefault((r.inst_id, r.bucket), {})[r.direction] = r
    lev_map = {r.inst_id: r for r in levs}

    state = {}
    for b in books:
        s = state.setdefault(b.inst_id, {})
        bk = {
            'held': {'long': b.held_long, 'short': b.held_short},
            'avg_px': {'long': b.avg_px_long, 'short': b.avg_px_short},
            'slots': {sl: _slot_from_row(r)
                      for sl, r in slots_map.get((b.inst_id, b.bucket), {}).items()},
            'prev_open_confirmed': bool(b.prev_open_confirmed),
            'prev_close_confirmed': bool(b.prev_close_confirmed),
            'last_desired': b.last_desired,
            'algo': {d: (_algo_from_row(r) if r else None)
                     for d, r in ((d, algos_map.get((b.inst_id, b.bucket), {}).get(d))
                                  for d in _DIRECTIONS)},
        }
        s[b.bucket] = bk
    # lev_set 与 algo 独立于 book 行存在性：补齐只有 lev/algo 记录的 inst
    for inst_id, r in lev_map.items():
        state.setdefault(inst_id, {})
    for (inst_id, _bucket) in algos_map:
        state.setdefault(inst_id, {})
    for inst_id, s in state.items():
        r = lev_map.get(inst_id)
        if r is None:
            s['lev_set'] = None
        else:
            lev = {}
            if r.cross_lev is not None:
                lev['cross'] = r.cross_lev
            if r.isolated_lev is not None:
                lev['isolated'] = r.isolated_lev
            s['lev_set'] = lev
    return state


def save_inst_position(session, inst_id: str, s: dict):
    """整段重写某合约的账本（先删后插，同事务原子；单次 ≤9 行）"""
    s = s or {}
    session.execute(delete(PosBook).where(PosBook.inst_id == inst_id))
    session.execute(delete(PosSlot).where(PosSlot.inst_id == inst_id))
    session.execute(delete(PosAlgo).where(PosAlgo.inst_id == inst_id))
    session.execute(delete(PosLev).where(PosLev.inst_id == inst_id))

    for bucket in _BUCKETS:
        bk = s.get(bucket)
        if not isinstance(bk, dict):
            continue
        held = bk.get('held') or {}
        avg = bk.get('avg_px') or {}
        session.add(PosBook(
            inst_id=inst_id, bucket=bucket,
            held_long=float(held.get('long', 0) or 0),
            held_short=float(held.get('short', 0) or 0),
            avg_px_long=float(avg.get('long', 0) or 0),
            avg_px_short=float(avg.get('short', 0) or 0),
            prev_open_confirmed=bool(bk.get('prev_open_confirmed', False)),
            prev_close_confirmed=bool(bk.get('prev_close_confirmed', False)),
            last_desired=bk.get('last_desired')))
        for slot, pt in (bk.get('slots') or {}).items():
            if not isinstance(pt, dict):
                continue
            session.add(PosSlot(
                inst_id=inst_id, bucket=bucket, slot=slot,
                state=str(pt.get('state') or 'IDLE'),
                ord_id=pt.get('ord_id'),
                price=float(pt.get('price', 0) or 0),
                amount=float(pt.get('amount', 0) or 0),
                placed_ts=float(pt.get('placed_ts', 0) or 0),
                acc_filled=float(pt.get('acc_filled', 0) or 0),
                dir=pt.get('dir'),
                qfail_logged=bool(pt.get('qfail_logged', False))))
        for direction, rec in (bk.get('algo') or {}).items():
            if not isinstance(rec, dict):
                continue
            session.add(PosAlgo(
                inst_id=inst_id, bucket=bucket, direction=direction,
                algo_id=rec.get('algo_id'),
                amount=float(rec.get('amount', 0) or 0),
                sl=float(rec.get('sl', 0) or 0),
                tp=float(rec.get('tp', 0) or 0),
                ts=float(rec.get('ts', 0) or 0)))

    lev = s.get('lev_set')
    if isinstance(lev, dict):
        session.add(PosLev(
            inst_id=inst_id,
            cross_lev=lev.get('cross'),
            isolated_lev=lev.get('isolated')))


def save_position_state(session, data: dict):
    """整树重写（迁移/兜底用；日常保存走 save_inst_position）"""
    for model in (PosBook, PosSlot, PosAlgo, PosLev):
        session.execute(delete(model))
    for inst_id, s in (data or {}).items():
        save_inst_position(session, inst_id, s)


def clear_position_state(session):
    for model in (PosBook, PosSlot, PosAlgo, PosLev):
        session.execute(delete(model))
