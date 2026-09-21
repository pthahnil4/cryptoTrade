#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘分析记录 - 数据访问层（迁移批次10）
==========================================
task_analysis_records 表的增删改查 + 事后复盘价格惰性回填：

    add_record(session, rec)               新增一条分析记录，返回自增 id
    query_records(session, ...)            按币种/时间段/判断筛选（ts 降序）
    query_slot_records(session, hour_slot) 按小时槽取记录（分析纪律闸门口径）
    query_by_ids(session, ids)             按 id 批量取记录（打卡反查分析依据）
    update_user_fields(session, rec_id, ..) 修改个人判断/分析原因
    delete_record(session, rec_id)         删除记录
    backfill_due_reviews(session, limit)   惰性回填记录后「N×短周期」近/远窗口价格
    compute_stats(records)                 个人判断 vs 策略方向命中率统计

复盘回填语义：
- 复盘窗口按每条记录自身短周期动态计算：近=REVIEW_NEAR_MULT×短周期，远=REVIEW_FAR_MULT×短周期
- 仅对已到窗口期（now >= ts + 窗口）且对应价格字段为空的记录回填
- 取价：以短周期 K 线，ts+窗口时刻所在 bar 的 close（bar 必须已收线确认）
- K线拉取失败（断网等）静默跳过该记录，下次查询时重试，不阻塞列表返回
"""

import os
import re
import sys
import math
import logging
import datetime
from typing import Dict, List, Optional

from sqlalchemy import select, or_, delete as sa_delete

from .models import TaskAnalysisRecord

logger = logging.getLogger(__name__)

_TS_FMT = '%Y-%m-%d %H:%M:%S'

# 复盘窗口倍数：以每条记录自身的短周期为基准动态计算
#   近窗口 = REVIEW_NEAR_MULT × 短周期，远窗口 = REVIEW_FAR_MULT × 短周期
#   例：短周期 15m → 近 1H(4×)、远 2H(8×)；5m → 20m/40m；30m → 2H/4H
#   想调窗口只改这两个倍数即可，前端标签会自动同步（stats 里回传倍数）
REVIEW_NEAR_MULT = 4
REVIEW_FAR_MULT = 8

# 命中率中性带（横盘死区）：实际涨跌幅 |chg%| <= θ 判为「横盘」，否则按方向判「涨/跌」
#   θ = max(HIT_FLOOR_PCT, HIT_ATR_K × atr_pct × 窗口缩放)
#   - ATR 自适应：波动大的币自动放宽死区、横盘币自动收紧（atr_pct 每条记录已存库）
#   - 近/远窗口按时间开方缩放（随机游走尺度 ~ √时长）：远窗口 θ = 近窗口 θ × √(far/near)
#   - HIT_FLOOR_PCT 地板：atr_pct 缺失/极小时仍保留最小死区，避免退化成 0 阈值
#   实测近/远窗口净波动中位≈1×ATR，取 0.3×ATR 约卡住最低 15% 噪声归为横盘
HIT_ATR_K = 0.3
HIT_FLOOR_PCT = 0.1
HIT_FAR_SCALE = math.sqrt(REVIEW_FAR_MULT / REVIEW_NEAR_MULT)   # ≈1.414（远窗口时长是近窗口 2 倍）

_UNIT_MIN = {'M': 1, 'H': 60, 'W': 10080, 'D': 1440}


def period_to_minutes(p) -> int:
    """把 '5m'/'15m'/'1H'/'4H'/'1D' 等周期串解析为分钟数，无法解析返回 0"""
    m = re.match(r'^\s*(\d+)\s*([mMhHwWdD])\s*$', str(p or ''))
    if not m:
        return 0
    return int(m.group(1)) * _UNIT_MIN.get(m.group(2).upper(), 0)


def review_windows_for(short_period: str):
    """返回该记录的两个复盘窗口: ((price_col, ts_col, kline_bar, offset_minutes), ...)

    以短周期 K 线为取价粒度（最贴合用户实际操作周期）；短周期无法解析时
    回退到旧的固定 1H/4H，保证兼容。
    """
    sm = period_to_minutes(short_period)
    if sm <= 0:
        return (('price_1h', 'ts_1h', '1H', 60),
                ('price_4h', 'ts_4h', '4H', 240))
    bar = str(short_period).strip()
    return (('price_1h', 'ts_1h', bar, sm * REVIEW_NEAR_MULT),
            ('price_4h', 'ts_4h', bar, sm * REVIEW_FAR_MULT))


def add_record(session, rec: dict) -> int:
    """新增分析记录，返回自增 id（rec 为路由层校验后的字段 dict）

    分析纪律（批次11）：hour_slot / source 由服务端自动推导，不信任前端传入——
      hour_slot  取 ts 前 13 位 'YYYY-MM-DD HH'，闸门/巡检按此聚合
      source     |now - ts| <= 宽限期 → live，否则 backfill（事后补记）
    前端无法把补记伪造成"当时就分析了"，看板则把补记率作为诚实指标呈现。
    """
    ts = str(rec.get('ts') or '')
    grace = int(rec.get('_grace_minutes') or 15)
    source = str(rec.get('source') or '') or _classify_source(ts, grace)
    row = TaskAnalysisRecord(
        ts=ts,
        inst_id=str(rec.get('inst_id') or ''),
        price=float(rec.get('price') or 0),
        short_period=str(rec.get('short_period') or ''),
        long_period=str(rec.get('long_period') or ''),
        short_dir=str(rec.get('short_dir') or ''),
        long_dir=str(rec.get('long_dir') or ''),
        long_dir_prev=rec.get('long_dir_prev') or None,
        atr_pct=float(rec.get('atr_pct') or 0),
        user_judgment=str(rec.get('user_judgment') or ''),
        user_reason=str(rec.get('user_reason') or ''),
        hour_slot=ts[:13] if len(ts) >= 13 else '',
        source=source,
    )
    session.add(row)
    session.flush()
    return row.id


def _classify_source(ts: str, grace_minutes: int) -> str:
    """按 |now - ts| 判定记录来源；ts 非法时按 live（此刻正在写入）"""
    try:
        rec_ts = datetime.datetime.strptime(str(ts or '').strip()[:19], _TS_FMT)
    except ValueError:
        return 'live'
    delta = abs((datetime.datetime.now() - rec_ts).total_seconds())
    return 'live' if delta <= max(0, int(grace_minutes)) * 60 else 'backfill'


def query_slot_records(session, hour_slot: str) -> List[dict]:
    """按小时槽取分析记录（走 idx_tar_slot，ts 升序）"""
    if not hour_slot:
        return []
    stmt = (select(TaskAnalysisRecord)
            .where(TaskAnalysisRecord.hour_slot == hour_slot)
            .order_by(TaskAnalysisRecord.ts.asc(), TaskAnalysisRecord.id.asc()))
    return [r.to_dict() for r in session.execute(stmt).scalars().all()]


def query_by_ids(session, ids: List[int]) -> List[dict]:
    """按 id 批量取记录（打卡详情反查“这次打卡依据的是哪几条分析”）"""
    ids = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not ids:
        return []
    stmt = (select(TaskAnalysisRecord)
            .where(TaskAnalysisRecord.id.in_(ids))
            .order_by(TaskAnalysisRecord.ts.asc()))
    return [r.to_dict() for r in session.execute(stmt).scalars().all()]


def query_records(session, inst_id: str = None, start: str = None,
                  end: str = None, judgment: str = None) -> List[dict]:
    """按条件查询分析记录，返回 dict 列表（ts 降序，同 ts 按 id 降序）。

    start/end 为 'YYYY-MM-DD HH:MM:SS' 字符串闭区间（与项目其他模块一致）。
    """
    stmt = select(TaskAnalysisRecord)
    if inst_id:
        stmt = stmt.where(TaskAnalysisRecord.inst_id == inst_id)
    if judgment:
        stmt = stmt.where(TaskAnalysisRecord.user_judgment == judgment)
    if start:
        stmt = stmt.where(TaskAnalysisRecord.ts >= start)
    if end:
        stmt = stmt.where(TaskAnalysisRecord.ts <= end)
    stmt = stmt.order_by(TaskAnalysisRecord.ts.desc(), TaskAnalysisRecord.id.desc())
    rows = session.execute(stmt).scalars().all()
    return [r.to_dict() for r in rows]


def update_user_fields(session, rec_id: int, judgment: str, reason: str) -> bool:
    """修改个人判断与分析原因；记录不存在返回 False"""
    row = session.get(TaskAnalysisRecord, rec_id)
    if row is None:
        return False
    row.user_judgment = judgment
    row.user_reason = reason or ''
    return True


def delete_record(session, rec_id: int) -> bool:
    """删除记录；记录不存在返回 False"""
    result = session.execute(
        sa_delete(TaskAnalysisRecord).where(TaskAnalysisRecord.id == rec_id))
    return (result.rowcount or 0) > 0


def delete_records(session, rec_ids: List[int]) -> int:
    """批量删除记录，返回实际删除的条数（不存在的 id 自动忽略）。"""
    ids = [int(i) for i in (rec_ids or []) if str(i).strip() != '']
    if not ids:
        return 0
    result = session.execute(
        sa_delete(TaskAnalysisRecord).where(TaskAnalysisRecord.id.in_(ids)))
    return result.rowcount or 0


# =============================================================================
# 复盘价格惰性回填
# =============================================================================

def _fetch_kline_module():
    """惰性导入策略模块的K线工具（路径处理与 strategy_adapter 同款）"""
    strategy_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'strategy')
    crypto_dir = os.path.dirname(os.path.abspath(__file__))
    for d in (strategy_dir, crypto_dir):
        if d not in sys.path:
            sys.path.insert(0, d)
    import pro3_singletimeframe as _single
    return _single


def _review_price_at(single_mod, inst_id: str, bar: str, target: datetime.datetime):
    """取 target 时刻所在K线 bar 的 close 价。

    返回 (price, bar_ts_str)；bar 未收线/数据不足/异常时返回 (None, None)，
    由调用方静默跳过下次重试。
    """
    try:
        df = single_mod._fetch_kline_data(inst_id, bar)
    except Exception as e:
        logger.warning(f'[AnalysisRecord] {inst_id} {bar} K线拉取失败，跳过回填: {e}')
        return None, None
    if df is None or len(df) == 0:
        return None, None
    # 所在 bar = 起始时间 <= target 的最后一根（K线时间戳为 bar 起点，东八区）
    past = df[df.index <= target]
    if len(past) == 0:
        return None, None
    row = past.iloc[-1]
    # 未收线的 bar（OKX confirm=0）close 仍在变动，等待下次查询再回填
    confirm = float(row.get('confirm', 1) or 0)
    if confirm < 1:
        return None, None
    price = float(row.get('close', 0) or 0)
    if price <= 0:
        return None, None
    return price, past.index[-1].strftime(_TS_FMT)


def backfill_due_reviews(session, limit: int = 500) -> int:
    """对已到窗口期且未回填的记录补齐"近/远窗口"后续价格，返回回填字段数。

    复盘窗口按每条记录自身短周期动态计算（近=REVIEW_NEAR_MULT×短周期，
    远=REVIEW_FAR_MULT×短周期），以短周期 K 线取价。

    在独立 session_scope 内调用（K线拉取为网络 I/O，提交后再查列表，
    避免长事务占用远端高RTT数据库连接）。
    """
    now = datetime.datetime.now()
    stmt = (select(TaskAnalysisRecord)
            .where(or_(TaskAnalysisRecord.price_1h.is_(None),
                       TaskAnalysisRecord.price_4h.is_(None)))
            .order_by(TaskAnalysisRecord.ts.asc())
            .limit(limit))
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return 0

    single_mod = _fetch_kline_module()
    filled = 0
    for row in rows:
        try:
            base_ts = datetime.datetime.strptime(row.ts, _TS_FMT)
        except (TypeError, ValueError):
            continue
        for price_col, ts_col, bar, offset_min in review_windows_for(row.short_period):
            if getattr(row, price_col) is not None:
                continue
            target = base_ts + datetime.timedelta(minutes=offset_min)
            if now < target:
                continue
            price, bar_ts = _review_price_at(single_mod, row.inst_id, bar, target)
            if price is None:
                continue
            setattr(row, price_col, price)
            setattr(row, ts_col, bar_ts)
            filled += 1
    if filled:
        session.flush()
        logger.info(f'[AnalysisRecord] 复盘回填完成：{len(rows)} 条记录补齐 {filled} 个价格字段')
    return filled


def historical_prices(inst_id: str, targets: List[datetime.datetime],
                      bar: str = '1H') -> Dict[str, Optional[float]]:
    """批量取历史时点价格：一个币种只拉一次 K 线，再从同一 DataFrame 里查全部时点。

    供「回溯分析」录入使用：为过去的小时槽补记分析记录时，快照价必须是
    当时的真实价格而不是现价，否则复盘口径彻底失真。

    取价口径：target 所在 bar 的【上一根已收线 bar】的 close——bar 时间戳为
    起点，上一根的 close 恰好就是 target 时刻的价格，不引入未来数据。
    K 线拉不到/超出历史深度时该时点返回 None，由调用方如实报错。

    返回 {target.strftime('%Y-%m-%d %H:%M:%S'): price or None}
    """
    keys = [t.strftime(_TS_FMT) for t in targets]
    result = {k: None for k in keys}
    if not targets:
        return result
    try:
        single_mod = _fetch_kline_module()
        df = single_mod._fetch_kline_data(inst_id, bar)
    except Exception as e:
        logger.warning(f'[AnalysisRecord] {inst_id} {bar} 历史K线拉取失败: {e}')
        return result
    if df is None or len(df) == 0:
        return result

    for t in targets:
        key = t.strftime(_TS_FMT)
        # 严格小于 target 的最后一根 = 已在 target 时刻收线的 bar
        past = df[df.index < t]
        if len(past) == 0:
            continue
        row = past.iloc[-1]
        if float(row.get('confirm', 1) or 0) < 1:
            continue
        price = float(row.get('close', 0) or 0)
        if price > 0:
            result[key] = price
    return result


# =============================================================================
# 命中率统计（路由层聚合口径）
# =============================================================================

def _empty_bucket():
    return {'total': 0, 'hit': 0, 'rate': 0.0, 'avg_pct': 0.0}


def _empty_confusion():
    """个人判断混淆矩阵骨架：行=预测(rise/watch/fall)，列=实际(up/flat/down)"""
    return {j: {'up': 0, 'flat': 0, 'down': 0} for j in ('rise', 'watch', 'fall')}


# 判断/方向 → 期望的实际类别；命中 = 期望类别与实际类别一致
_JUDGMENT_EXPECT = {'rise': 'up', 'fall': 'down', 'watch': 'flat'}
_DIR_EXPECT = {'long': 'up', 'short': 'down'}


def neutral_theta(atr_pct, is_far: bool) -> float:
    """中性带阈值 θ(%)：θ = max(地板, K × atr_pct × 窗口缩放)。远窗口按 √时长比放大。"""
    scale = HIT_FAR_SCALE if is_far else 1.0
    return max(HIT_FLOOR_PCT, HIT_ATR_K * scale * float(atr_pct or 0.0))


def classify_move(price, follow, atr_pct, is_far: bool) -> Optional[str]:
    """把后续价相对快照价的涨跌归为 'up'/'flat'/'down'（θ 内为横盘）；数据无效返回 None。"""
    if price is None or follow is None:
        return None
    price = float(price)
    follow = float(follow)
    if price <= 0 or follow <= 0:
        return None
    chg = (follow / price - 1) * 100
    th = neutral_theta(atr_pct, is_far)
    if chg > th:
        return 'up'
    if chg < -th:
        return 'down'
    return 'flat'


def compute_stats(records: List[dict]) -> dict:
    """基于记录列表计算个人判断与策略方向的命中率（近/远窗口双口径 + 混淆矩阵）。

    实际走势按 ATR 中性带三分类：涨(chg>θ)/横盘(|chg|<=θ)/跌(chg<-θ)，θ 见 neutral_theta。
    个人判断与策略方向采用统一口径：横盘都计入分母（遇横盘即判错，不再对策略桶排除）。
    - 个人判断：rise/fall/watch 三分类全部计分，命中=判断类别与实际类别一致（观望命中横盘）
    - 策略方向：取 long_dir_prev（实际决策方向，缺省回退 long_dir），long/short 计分；
      实际横盘时同样计入分母（方向单在震荡里未兑现＝判错）
    - confusion：个人判断的「预测×实际」3×3 计数，供前端混淆矩阵定位误判模式
    - 回传 near_mult/far_mult/hit_k/hit_floor_pct/hit_far_scale，前端本地重算据此对齐口径
    """
    stats = {'total': len(records),
             'near_mult': REVIEW_NEAR_MULT, 'far_mult': REVIEW_FAR_MULT,
             'hit_k': HIT_ATR_K, 'hit_floor_pct': HIT_FLOOR_PCT, 'hit_far_scale': HIT_FAR_SCALE,
             'user': {'near': _empty_bucket(), 'far': _empty_bucket()},
             'strategy': {'near': _empty_bucket(), 'far': _empty_bucket()},
             'confusion': {'near': _empty_confusion(), 'far': _empty_confusion()}}
    for rec in records:
        price = float(rec.get('price') or 0)
        if price <= 0:
            continue
        atr_pct = float(rec.get('atr_pct') or 0)
        judgment = rec.get('user_judgment') or ''
        strategy_dir = rec.get('long_dir_prev') or rec.get('long_dir') or ''
        for win_key, price_col, is_far in (('near', 'price_1h', False), ('far', 'price_4h', True)):
            follow = rec.get(price_col)
            if follow is None or float(follow) <= 0:
                continue
            actual = classify_move(price, float(follow), atr_pct, is_far)
            if actual is None:
                continue
            chg = (float(follow) / price - 1) * 100
            # 个人判断：三分类全计分 + 混淆矩阵计数
            if judgment in _JUDGMENT_EXPECT:
                b = stats['user'][win_key]
                b['total'] += 1
                b['avg_pct'] += chg
                if _JUDGMENT_EXPECT[judgment] == actual:
                    b['hit'] += 1
                stats['confusion'][win_key][judgment][actual] += 1
            # 策略方向：与个人判断统一口径，横盘也计入分母（方向单遇横盘＝判错）
            if strategy_dir in _DIR_EXPECT:
                b = stats['strategy'][win_key]
                b['total'] += 1
                b['avg_pct'] += chg
                if _DIR_EXPECT[strategy_dir] == actual:
                    b['hit'] += 1
    for side in ('user', 'strategy'):
        for win_key in ('near', 'far'):
            b = stats[side][win_key]
            if b['total'] > 0:
                b['rate'] = round(b['hit'] / b['total'] * 100, 1)
                b['avg_pct'] = round(b['avg_pct'] / b['total'], 3)
            else:
                b['avg_pct'] = 0.0
    return stats
