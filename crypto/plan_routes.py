#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务计划系统 - Flask 蓝图（长期主义版本 Longtermism v1.1）
============================================================
所有 /plan 和 /plan/api/* 路由
包括：计划管理、任务卡管理、100格打卡、核算引擎

【长期主义设计理念】
- 低门槛启动：随时想开始就开始，几分钟也算在场，没有任何心理负担
- 坚持比强度更重要：核心指标是"在场天数"，而非"每日时长"
- 不惧中断：允许任意中断与回归，回来永远被欢迎，绝不罚款
- 今天在场，比今天做多少更重要
- 身份认同：你是一个长期主义者——行为由身份驱动，而非压力驱动
- 数据诚实、情绪友好：断档如实呈现在场日历，大字只展示历史最佳连续

【存储】MySQL（迁移批次3）：数据访问统一走 plan_repo（整树语义，
与原 JSON 版 _load_plans/_save_plans 契约一致），连接配置见 database.py。
对外 API 契约与 JSON 文件版完全一致。
"""

import datetime
import uuid
import logging
import os
import sys
from flask import Blueprint, jsonify, request, render_template

from .database import session_scope
from . import plan_repo as repo

# =============================================================================
# 【关键】确保优先加载项目根目录的 api_config.py（与 api_routes 同款守卫）
# 策略模块会向 sys.path 头部插入含另一份 api_config.py 的子目录，
# 若不加守卫，懒加载导入时可能取到错误密钥导致 OKX 签名失败。
# =============================================================================
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT in sys.path:
    sys.path.remove(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)

plan_bp = Blueprint('plan_bp', __name__)

# =============================================================================
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台
# 预热（warmup_async）完成，蓝图导入期不再同步建表，避免阻塞启动。
# =============================================================================


# =============================================================================
# 数据操作层（整树读写语义，底层为 MySQL 差量同步，见 plan_repo）
# =============================================================================

def _load_plans(session):
    """读取整树数据；未初始化（空库）时自动预置默认计划"""
    data = repo.load_plans_data(session)
    if not data.get('initialized', False):
        return _create_default_plans(session)
    # 数据迁移：每日达标线 12→10，严重线 8→6
    _migrate_daily_rule(session, data)
    # 数据迁移：交易打卡记录字段重构（operation_notes/reflection → market_analysis/action_advice）
    _migrate_record_fields(session, data)
    return data


def _migrate_daily_rule(session, data):
    """迁移为长期主义模式：在场优先，取消强制每日时长与罚款机制"""
    changed = False
    for plan in data.get('plans', []):
        rule = plan.get('daily_rule', {})
        if rule.get('mode') != 'longtermism':
            # 旧字段（target_hours / severe_hours / 罚款倍数）语义废弃，仅保留作参考
            rule['mode'] = 'longtermism'
            rule.pop('window', None)
            rule['min_record_minutes'] = 1   # 最低记录 1 分钟：几分钟也算在场
            changed = True
        # v1.1：提前通关奖励不再翻倍（×1）——提前通关本身就是奖励（时间自由）
        if rule.get('early_bonus_multiplier', 1) != 1:
            rule['early_bonus_multiplier'] = 1
            changed = True
    if changed:
        _save_plans(session, data)


# 交易打卡记录规范字段（v2）：行情分析 / 操作建议 / 账户金额
_TRADE_RECORD_FIELDS = ('market_analysis', 'action_advice', 'account_balance')


def _migrate_record_fields(session, data):
    """迁移交易打卡记录字段（v2 重构）

    - 旧「深度思考/复盘」(reflection) 内容映射为「行情分析」(market_analysis)
    - 旧「具体操作内容」(operation_notes) 字段废弃移除
    - 新增「操作建议」(action_advice)，缺失时补空字符串
    - 「账户金额」(account_balance) 缺失时补空字符串
    确保旧数据在加载时自动完成重命名与补齐，与前端提交格式完全一致。
    """
    changed = False
    for plan in data.get('plans', []):
        if plan.get('type') != 'trade':
            continue
        for card in plan.get('cards', []):
            for slot in card.get('slots', []):
                if not slot.get('filled') or not slot.get('record'):
                    continue
                record = slot['record']
                # reflection → market_analysis（行情分析取代深度思考/复盘）
                if 'market_analysis' not in record:
                    record['market_analysis'] = record.get('reflection', '') or ''
                    changed = True
                # 补齐操作建议 / 账户金额默认空值
                for field in ('action_advice', 'account_balance'):
                    if field not in record:
                        record[field] = ''
                        changed = True
                # 移除废弃旧字段
                for legacy in ('operation_notes', 'reflection'):
                    if legacy in record:
                        record.pop(legacy)
                        changed = True
    if changed:
        _save_plans(session, data)


def _normalize_trade_record(record, analysis=None):
    """规范化交易打卡记录，保证后端存储结构与前端提交格式完全一致

    仅保留规范字段：prediction / duration_minutes / actual /
    market_analysis / action_advice / account_balance /
    analysis_ids / analysis_hour / bypass_analysis，
    丢弃旧字段（operation_notes / reflection 等），缺失字段补空字符串。

    analysis：服务端闸门判定结果（{analysis_ids, analysis_hour, bypass_analysis}）。
    fill/backfill 路径一律由服务端显式传入覆盖，客户端无法伪造；
    为 None 时沿用 record 内已有值（update-slot 局部更新场景，避免抹除关联）。
    """
    if not isinstance(record, dict):
        record = {}
    normalized = {
        'prediction': str(record.get('prediction', '') or ''),
        'duration_minutes': record.get('duration_minutes', 0) or 0,
        'actual': str(record.get('actual', '') or ''),
    }
    for field in _TRADE_RECORD_FIELDS:
        normalized[field] = str(record.get(field, '') or '')

    src = analysis if isinstance(analysis, dict) else record
    raw_ids = src.get('analysis_ids') or []
    if isinstance(raw_ids, str):
        raw_ids = raw_ids.split(',')
    ids = [int(str(x).strip()) for x in raw_ids if str(x).strip().isdigit()]
    normalized['analysis_ids'] = ids[:40]
    normalized['analysis_hour'] = str(src.get('analysis_hour') or '')[:13]
    normalized['bypass_analysis'] = bool(src.get('bypass_analysis'))
    return normalized


# =============================================================================
# 分析纪律闸门（批次11）：先分析记录，然后才允许交易打卡
# -----------------------------------------------------------------------------
# 只约束 card['type'] == 'trade'，学习卡不受影响。
# 闸门自身异常一律 fail-open：纪律功能绝不能因为自己的 bug 拦住正常打卡。
# =============================================================================

def _discipline_gate(session, filled_at_str: str = '', cfg: dict = None):
    """判定一个打卡时点的小时槽是否允许交易打卡。

    返回 (allowed, analysis, block_data)：
      allowed    是否放行
      analysis   服务端判定的关联载荷（直接交给 _normalize_trade_record）
      block_data 不放行时回给前端的结构化数据（need_analysis 等）；放行时为 None
    闸门不可用/已关闭时返回 (True, None, None)，行为与改造前完全一致。
    """
    try:
        from . import discipline_repo as disc
    except Exception as e:
        logger.warning(f'[Plan] 分析纪律模块不可用，闸门跳过: {e}')
        return True, None, None
    try:
        cfg = cfg if cfg is not None else disc.load_config()
        if not cfg.get('enabled', True):
            return True, None, None
        target = disc.parse_dt(filled_at_str) if filled_at_str else datetime.datetime.now()
        gate = disc.gate_check(session, target, cfg)
    except Exception as e:
        logger.error(f'[Plan] 分析闸门判定异常，按放行处理: {e}', exc_info=True)
        return True, None, None

    ev = gate.get('evaluation') or {}
    records = ev.get('records') or []
    analysis = {
        'analysis_ids': [r['id'] for r in records],
        'analysis_hour': gate.get('hour_slot') or '',
        # soft 模式：放行但留痕，看板单列“无分析打卡”
        'bypass_analysis': gate.get('mode') == 'soft_bypass',
    }
    if gate.get('allowed'):
        return True, analysis, None

    block_data = {
        'need_analysis': True,
        'strict_mode': cfg.get('strict_mode', 'strict'),
        'hour_slot': gate.get('hour_slot') or '',
        'required': ev.get('required', 1),
        'actual': ev.get('actual', 0),
        'missing_coins': ev.get('missing_coins') or [],
        'is_history': bool(filled_at_str),
        'records': [{
            'id': r['id'], 'ts': r['ts'], 'inst_id': r['inst_id'],
            'user_judgment': r.get('user_judgment', ''),
            'source': r.get('source', 'live'),
        } for r in records],
    }
    return False, analysis, block_data


def _gate_block_response(block_data: dict, message: str):
    """闸门拦截的统一 403 响应"""
    return jsonify({'code': 403, 'message': message, 'data': block_data})


def _gate_message(block_data: dict) -> str:
    """拦截文案：区分当小时与历史补录，直接告诉用户下一步该做什么"""
    slot = block_data.get('hour_slot') or ''
    miss = block_data.get('missing_coins') or []
    if miss:
        detail = f'未覆盖币种：{"、".join(miss)}'
    else:
        detail = f"分析记录 {block_data.get('actual', 0)} 条，要求 {block_data.get('required', 1)} 条"
    if block_data.get('is_history'):
        return (f'📝 历史槽 {slot}:00 缺分析记录（{detail}）\n'
                f'请先为这些小时补上回溯分析记录，再重新补录打卡')
    return (f'📝 先分析，再打卡\n本小时（{slot}:00）{detail}\n'
            f'弹窗内可一键批量快照并录入判断，完成后自动放行')


def _save_plans(session, data):
    """整树写回 MySQL（plan_repo 内部差量同步）"""
    repo.save_plans_data(session, data)


def _new_id(prefix='card'):
    """生成唯一 ID"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _now_str():
    """当前时间字符串"""
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _ts_to_str(ts_ms):
    """将13位毫秒时间戳转为可读字符串"""
    try:
        ts_int = int(ts_ms)
        if ts_int <= 0:
            return 'N/A'
        return datetime.datetime.fromtimestamp(ts_int / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError, TypeError):
        return 'N/A'


def _today_str():
    """今天日期字符串"""
    return datetime.datetime.now().strftime('%Y-%m-%d')


def _normalize_datetime_str(value):
    """校验并规范化时间字符串为 YYYY-MM-DD HH:MM:SS

    接受 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS；空值返回 ''；非法返回 None。
    供开始/结束时间、补录打卡时间统一使用，保证存储格式一致。
    """
    if not value:
        return ''
    value = str(value).strip()
    try:
        if len(value) == 16:
            dt = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M')
        elif len(value) == 19:
            dt = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        else:
            return None
    except ValueError:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _create_empty_slots(count=100):
    """创建 N 个空格子"""
    return [
        {
            "slot_index": i,
            "filled": False,
            "filled_at": "",
            "record": None
        }
        for i in range(count)
    ]


def _create_learn_cards(count=10, base_reward=2000):
    """预置学习任务卡"""
    cards = []
    for i in range(1, count + 1):
        reward = base_reward
        cards.append({
            "id": _new_id('learn'),
            "type": "learn",
            "round": i,
            "title": f"学习任务{i} · 100小时",
            "goal": f"第{i}个100小时学习任务",
            "reward": reward,
            "start_time": "",
            "end_time": "",
            "status": "pending" if i > 1 else "in_progress",
            "milestones": [],
            "slots": _create_empty_slots(100),
            "notes": [],
            "review": "",
            "settlement": None,
            "created_at": _now_str(),
            "updated_at": _now_str()
        })
    return cards


def _create_trade_cards(count=10):
    """预置交易轮次卡"""
    cards = []
    for i in range(1, count + 1):
        base_reward = 2000 * i
        hourly_rate = base_reward // 100
        cards.append({
            "id": _new_id('trade'),
            "type": "trade",
            "round": i,
            "title": f"第{i}轮 · 翻倍挑战",
            "goal": f"100交易小时内平均每小时盈利1%，本金翻倍（第{i}轮）",
            "reward": base_reward,
            "base_reward": base_reward,
            "hourly_rate": hourly_rate,
            "start_time": "",
            "end_time": "",
            "status": "pending" if i > 1 else "in_progress",
            "milestones": [],
            "slots": _create_empty_slots(100),
            "notes": [],
            "review": "",
            "settlement": None,
            "created_at": _now_str(),
            "updated_at": _now_str()
        })
    return cards


def _create_default_plans(session):
    """创建默认数据：学习计划 + 交易计划"""
    data = {
        "initialized": True,
        "plans": [
            {
                "id": "plan_learn_1000",
                "type": "learn",
                "name": "1000小时学习计划",
                "grand_goal": "彻底做出稳定运行的量化交易系统",
                "total_hours": 1000,
                "round_count": 10,
                "per_round_hours": 100,
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "daily_rule": {
                    "mode": "longtermism",
                    "min_record_minutes": 1,
                    "early_bonus_multiplier": 1,
                    "welcome_message": "今天也想开始了吗？随时欢迎 👋"
                },
                "cards": _create_learn_cards(10, 2000),
                "created_at": _now_str()
            },
            {
                "id": "plan_trade_1000",
                "type": "trade",
                "name": "1000小时翻10番交易计划",
                "grand_goal": "资金翻10番达到1000倍（10万U）",
                "total_hours": 1000,
                "round_count": 10,
                "per_round_hours": 100,
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "daily_rule": {
                    "mode": "longtermism",
                    "min_record_minutes": 1,
                    "early_bonus_multiplier": 1,
                    "welcome_message": "今天也想开始了吗？随时欢迎 👋"
                },
                "round_config": {
                    "base_reward_first": 2000,
                    "base_reward_step": 2000,
                    "early_bonus_multiplier": 1,
                    "pass_avg_hourly_profit_pct": 1.0
                },
                "cards": _create_trade_cards(10),
                "created_at": _now_str()
            }
        ]
    }
    _save_plans(session, data)
    return data


# =============================================================================
# 核算引擎（长期主义版本：无罚款，核心指标=在场天数）
# =============================================================================

def _calc_presence_days(slots):
    """统计在场天数：有任意打卡记录（filled）的日期数量

    长期主义核心指标——今天在场，比今天做多少更重要。
    只要当天勾选了 1 格（哪怕几分钟），就算"在场一天"。
    """
    days = set()
    for s in slots:
        if s.get('filled') and s.get('filled_at'):
            days.add(s['filled_at'][:10])
    return len(days)


def _calc_streak_days(all_slots):
    """计算连续在场天数（streak）：从今天/最近在场日往前连续不中断

    中断不产生任何惩罚：重新开始计数即可，回来本身就该被庆祝。
    """
    days = set()
    for slots in all_slots:
        for s in slots:
            if s.get('filled') and s.get('filled_at'):
                days.add(s['filled_at'][:10])
    if not days:
        return 0
    today = datetime.date.today()
    # 今天还没打卡不算断档：从昨天开始往前数
    cursor = today if today.isoformat() in days else today - datetime.timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= datetime.timedelta(days=1)
    return streak


def _early_multiplier(plan):
    """提前通关奖励倍率（v1.1 起固定 ×1）

    提前通关本身就是奖励——省下的时间比翻倍奖金更值钱。
    读取顺序：计划 round_config → daily_rule → 默认 1。
    """
    if plan:
        rc = plan.get('round_config', {})
        if rc.get('early_bonus_multiplier') is not None:
            return rc.get('early_bonus_multiplier')
        rule = plan.get('daily_rule', {})
        if rule.get('early_bonus_multiplier') is not None:
            return rule.get('early_bonus_multiplier')
    return 1


def _calc_best_streak_days(all_slots):
    """计算历史最佳连续在场天数（v1.1：展示最佳而非当前连续）

    断档不归零、不惩罚——大字永远显示"历史上最长的坚持段"；
    断档如实记录在在场日历里，随时可以续上。
    """
    days = set()
    for slots in all_slots:
        for s in slots:
            if s.get('filled') and s.get('filled_at'):
                days.add(s['filled_at'][:10])
    if not days:
        return 0
    best = 1
    cur = 1
    prev = None
    for ds in sorted(days):
        d = datetime.date.fromisoformat(ds)
        if prev is not None and (d - prev).days == 1:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 1
        prev = d
    return best


_CALENDAR_WEEKS = 15  # 在场日历显示近 15 周（GitHub 贡献图风格）


def _calendar_level(minutes):
    """在场时长分级（0-4）：0 无在场 → 4 两小时以上"""
    if minutes <= 0:
        return 0
    if minutes < 30:
        return 1
    if minutes < 60:
        return 2
    if minutes < 120:
        return 3
    return 4


def _build_presence_calendar(all_slots, weeks=_CALENDAR_WEEKS):
    """构建在场日历：从今天所在周的周一起，往前 weeks 周

    每天返回 {date, minutes, level, future}——断档如实呈现（level 0），
    与"最佳连续在场"配合：数据诚实，情绪友好。
    """
    minutes_by_day = {}
    for s in all_slots:
        if s.get('filled') and s.get('filled_at'):
            key = s['filled_at'][:10]
            minutes_by_day[key] = minutes_by_day.get(key, 0) + ((s.get('record') or {}).get('duration_minutes') or 0)
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    start = monday - datetime.timedelta(weeks=weeks - 1)
    days = []
    for i in range(weeks * 7):
        d = start + datetime.timedelta(days=i)
        ds = d.isoformat()
        minutes = minutes_by_day.get(ds, 0)
        days.append({
            'date': ds,
            'minutes': minutes,
            'level': _calendar_level(minutes),
            'future': d > today
        })
    return {'start_date': start.isoformat(), 'weeks': weeks, 'days': days}


def _calc_milestones_all_done(card):
    """子目标是否全部完成（学习卡提前通关的资格判定）"""
    milestones = card.get('milestones', [])
    return len(milestones) > 0 and all(m.get('done') for m in milestones)


def _calc_early_eligible(card):
    """是否具备提前通关资格：
    - 交易卡：始终具备（由资金倍数判定通关）
    - 学习卡：子目标全部完成（任务完成即通关，无需硬耗 100 小时）
    """
    if card['type'] == 'trade':
        return True
    return _calc_milestones_all_done(card)


def _calc_slot_hours_by_date(slots, date_str):
    """统计某日 slots 中 filled 的总时长（小时数）"""
    total_minutes = 0
    for slot in slots:
        if not slot.get('filled') or not slot.get('filled_at'):
            continue
        if slot['filled_at'].startswith(date_str):
            record = slot.get('record')
            if record:
                total_minutes += record.get('duration_minutes', 0)
    return total_minutes / 60.0


def _calc_checkin_count(slots, date_str=None):
    """统计打卡次数（filled 格子数）；传 date_str 时只统计当日"""
    count = 0
    for slot in slots:
        if not slot.get('filled') or not slot.get('filled_at'):
            continue
        if date_str and not slot['filled_at'].startswith(date_str):
            continue
        count += 1
    return count


def _build_checkin_daily(learn_slots, trade_slots):
    """按日聚合学习/交易打卡次数，从首次打卡日到今日逐日补齐（无打卡日补 0），
    供前端绘制每日/累积打卡双曲线折线图"""
    learn_by_day = {}
    trade_by_day = {}
    for s in learn_slots:
        if s.get('filled') and s.get('filled_at'):
            d = s['filled_at'][:10]
            learn_by_day[d] = learn_by_day.get(d, 0) + 1
    for s in trade_slots:
        if s.get('filled') and s.get('filled_at'):
            d = s['filled_at'][:10]
            trade_by_day[d] = trade_by_day.get(d, 0) + 1
    all_dates = set(learn_by_day) | set(trade_by_day)
    if not all_dates:
        return []
    fmt = '%Y-%m-%d'
    start = datetime.datetime.strptime(min(all_dates), fmt)
    end = datetime.datetime.strptime(max(_today_str(), max(all_dates)), fmt)
    series = []
    cur = start
    while cur <= end:
        d = cur.strftime(fmt)
        series.append({'date': d, 'learn': learn_by_day.get(d, 0), 'trade': trade_by_day.get(d, 0)})
        cur += datetime.timedelta(days=1)
    return series


def _calc_total_hours(slots):
    """统计 slots 中 filled 的总格子数（即总小时数）"""
    return sum(1 for s in slots if s.get('filled'))


def _calc_card_reward(plan, card):
    """计算任务卡的奖励预估（长期主义版：无罚款扣减）

    奖励构成 = 基础奖励 + 提前完成奖励
    - 交易卡：始终预估提前奖励（通关时按资金倍数正式结算）
    - 学习卡：子目标全部完成后具备提前通关资格，此时才计入提前奖励
    """
    filled_count = _calc_total_hours(card.get('slots', []))
    presence_days = _calc_presence_days(card.get('slots', []))

    if card['type'] == 'trade':
        base_reward = card.get('base_reward', 2000)
        hourly_rate = card.get('hourly_rate', 20)
    else:
        base_reward = card.get('reward', 2000)
        hourly_rate = base_reward // 100

    # 提前奖励：每提前 1 小时 = 时薪 × 1（v1.1 起不再翻倍——提前通关本身就是奖励：时间自由）
    early_eligible = _calc_early_eligible(card)
    early_hours = max(0, 100 - filled_count) if early_eligible else 0
    early_bonus = early_hours * hourly_rate * _early_multiplier(plan) if early_eligible else 0
    final_reward = base_reward + early_bonus

    return {
        'filled_count': filled_count,
        'total_hours': round(filled_count, 1),
        'presence_days': presence_days,
        'base_reward': base_reward,
        'early_eligible': early_eligible,
        'early_hours': early_hours,
        'early_bonus': round(early_bonus, 1),
        'final_reward': round(final_reward, 1)
    }


def _settle_card(plan, card, profit_multiplier=None):
    """正式结算一张任务卡（生成 settlement）

    长期主义版：
    - 学习卡：满 100 小时自动通关；子目标全部完成可提前通关（含提前奖励）
    - 交易卡：资金倍数 ≥ 2 判定翻倍通关；无任何罚款扣减
    """
    reward_info = _calc_card_reward(plan, card)
    filled_count = reward_info['filled_count']

    if card['type'] == 'trade':
        # 交易卡：需用户确认资金倍数
        multiplier = profit_multiplier if profit_multiplier is not None else 0
        is_pass = multiplier >= 2
        settlement = {
            "base_reward": card.get('base_reward', 2000),
            "total_hours_used": filled_count,
            "early_hours": max(0, 100 - filled_count),
            "early_bonus": reward_info['early_bonus'] if is_pass else 0,
            "final_reward": reward_info['final_reward'] if is_pass else 0,
            "profit_multiplier": multiplier,
            "settled_at": _now_str()
        }
        card['settlement'] = settlement
        card['status'] = 'completed' if is_pass else 'failed'
    else:
        # 学习卡：满 100 小时 或 子目标全部完成 → 通关（长期主义：任务完成即通关）
        is_pass = filled_count >= 100 or _calc_milestones_all_done(card)
        settlement = {
            "base_reward": card.get('reward', 2000),
            "total_hours_used": filled_count,
            "early_hours": reward_info['early_hours'],
            "early_bonus": reward_info['early_bonus'],
            "final_reward": reward_info['final_reward'],
            "settled_at": _now_str()
        }
        card['settlement'] = settlement
        card['status'] = 'completed' if is_pass else 'failed'

    # 解锁下一张卡
    _unlock_next_card(plan, card)
    card['updated_at'] = _now_str()
    return card


def _unlock_next_card(plan, current_card):
    """解锁下一张卡"""
    cards = plan.get('cards', [])
    current_round = current_card.get('round', 0)
    for c in cards:
        if c.get('round') == current_round + 1 and c.get('status') == 'pending':
            c['status'] = 'in_progress'
            c['start_time'] = _now_str()
            c['updated_at'] = _now_str()
            break


def _recompute_serial_chain(plan):
    """按 round 顺序重算串行链：第一张未结束的卡为进行中，其后未结束的卡全部锁定为待开始"""
    cards = sorted(plan.get('cards', []), key=lambda x: x.get('round', 0))
    active_found = False
    for c in cards:
        if c.get('status') in ('completed', 'failed', 'abandoned'):
            continue
        if not active_found:
            active_found = True
            if c.get('status') != 'in_progress':
                c['status'] = 'in_progress'
                if not c.get('start_time'):
                    c['start_time'] = _now_str()
        elif c.get('status') != 'pending':
            c['status'] = 'pending'


# =============================================================================
# 统计助手
# =============================================================================

def _calc_card_hit_rate(card):
    """单卡预测命中率（仅交易卡有 actual/hit 概念；学习卡返回 0）"""
    total_hit = 0
    total_filled = 0
    for s in card.get('slots', []):
        r = s.get('record')
        if r and r.get('actual') is not None and r.get('actual') != '':
            total_filled += 1
            if r.get('hit'):
                total_hit += 1
    return round(total_hit / total_filled * 100, 1) if total_filled > 0 else 0


def _calc_locked_ids(plan):
    """串行锁定：第一张未结束的卡为当前卡，其后未结束卡全部锁定"""
    locked = set()
    active_found = False
    for c in sorted(plan.get('cards', []), key=lambda x: x.get('round', 0)):
        if c['status'] in ('pending', 'in_progress'):
            if not active_found:
                active_found = True
            else:
                locked.add(c['id'])
    return locked


def _is_card_locked(plan, card_id):
    """判断指定卡是否被锁定（供 card-detail 接口使用）"""
    return card_id in _calc_locked_ids(plan)


def _build_plan_stats(plan):
    """构建计划级别的统计数据"""
    cards = plan.get('cards', [])
    learn_cards = [c for c in cards if c['type'] == 'learn']
    trade_cards = [c for c in cards if c['type'] == 'trade']

    learn_completed = sum(1 for c in learn_cards if c['status'] == 'completed')
    trade_completed = sum(1 for c in trade_cards if c['status'] == 'completed')
    learn_total_hours = sum(_calc_total_hours(c.get('slots', [])) for c in learn_cards)
    trade_total_hours = sum(_calc_total_hours(c.get('slots', [])) for c in trade_cards)

    # 长期主义核心指标：在场天数（按日期去重合计）与连续在场天数
    learn_presence_days = _calc_presence_days([s for c in learn_cards for s in c.get('slots', [])])
    trade_presence_days = _calc_presence_days([s for c in trade_cards for s in c.get('slots', [])])
    all_slots = [s for c in cards for s in c.get('slots', [])]
    total_presence_days = _calc_presence_days(all_slots)
    # v1.1：展示"最佳连续在场"（历史最长段），断档不归零——数据诚实，情绪友好
    best_streak_days = _calc_best_streak_days([c.get('slots', []) for c in cards])
    presence_calendar = _build_presence_calendar(all_slots)

    total_reward = sum(
        (c.get('settlement', {}) or {}).get('final_reward', 0)
        for c in cards if c.get('settlement')
    )

    # 预测命中率（交易卡）
    total_hit = 0
    total_filled_predict = 0
    for c in trade_cards:
        for s in c.get('slots', []):
            r = s.get('record')
            if r and r.get('actual') is not None and r.get('actual') != '':
                total_filled_predict += 1
                if r.get('hit'):
                    total_hit += 1

    hit_rate = round(total_hit / total_filled_predict * 100, 1) if total_filled_predict > 0 else 0

    # 当前进行中的卡片 + 串行锁定
    locked_ids = _calc_locked_ids(plan)
    active_card = None
    for c in sorted(cards, key=lambda x: x.get('round', 0)):
        if c['status'] in ('pending', 'in_progress') and c['id'] not in locked_ids:
            active_card = {
                'id': c['id'],
                'title': c['title'],
                'round': c['round'],
                'type': c['type'],
                'filled_count': _calc_total_hours(c.get('slots', [])),
            }
            break

    # 列表页只需摘要字段；完整 slots/notes/milestones 走 /plan/api/card-detail 按需加载
    result = []
    for c in sorted(cards, key=lambda x: x.get('round', 0)):
        c_info = {
            'id': c['id'],
            'type': c['type'],
            'round': c.get('round', 0),
            'title': c.get('title', ''),
            'reward': c.get('reward', 0),
            'base_reward': c.get('base_reward', c.get('reward', 0)),
            'hourly_rate': c.get('hourly_rate', 0),
            'status': c.get('status', 'pending'),
            'filled_count': _calc_total_hours(c.get('slots', [])),
            'locked': c['id'] in locked_ids,
            'hit_rate': _calc_card_hit_rate(c),
            'start_time': c.get('start_time', ''),
            'end_time': c.get('end_time', ''),
            'created_at': c.get('created_at', ''),
            'reward_info': _calc_card_reward(plan, c),
        }
        result.append(c_info)

    # 打卡次数统计：严格区分学习/交易，累计与当日分开计数
    today = _today_str()
    learn_slots_all = [s for c in learn_cards for s in c.get('slots', [])]
    trade_slots_all = [s for c in trade_cards for s in c.get('slots', [])]
    learn_checkin_total = _calc_checkin_count(learn_slots_all)
    trade_checkin_total = _calc_checkin_count(trade_slots_all)
    learn_checkin_today = _calc_checkin_count(learn_slots_all, today)
    trade_checkin_today = _calc_checkin_count(trade_slots_all, today)
    # 每日打卡历史序列（供前端绘制每日/累积双曲线）
    checkin_daily = _build_checkin_daily(learn_slots_all, trade_slots_all)

    return {
        'learn_completed': learn_completed,
        'learn_total': len(learn_cards),
        'trade_completed': trade_completed,
        'trade_total': len(trade_cards),
        'learn_total_hours': learn_total_hours,
        'trade_total_hours': trade_total_hours,
        'learn_checkin_total': learn_checkin_total,
        'trade_checkin_total': trade_checkin_total,
        'learn_checkin_today': learn_checkin_today,
        'trade_checkin_today': trade_checkin_today,
        'checkin_daily': checkin_daily,
        'learn_presence_days': learn_presence_days,
        'trade_presence_days': trade_presence_days,
        'total_presence_days': total_presence_days,
        'best_streak_days': best_streak_days,
        'presence_calendar': presence_calendar,
        'total_reward': round(total_reward, 1),
        'hit_rate': hit_rate,
        'active_card': active_card,
        'cards': result,
    }


def _build_plan_info(plan):
    """构建 /plan/api/list 的单个计划返回结构（含 stats）。

    抽取为独立函数，供列表接口与打卡变更接口（fill/update/unfill/backfill）
    复用，保证局部刷新返回的 plan 结构与整表拉取完全一致。
    """
    return {
        'id': plan['id'],
        'type': plan['type'],
        'name': plan['name'],
        'grand_goal': plan.get('grand_goal', ''),
        'total_hours': plan.get('total_hours', 1000),
        'round_count': plan.get('round_count', 10),
        'per_round_hours': plan.get('per_round_hours', 100),
        'daily_rule': plan.get('daily_rule', {}),
        'round_config': plan.get('round_config'),
        'stats': _build_plan_stats(plan),
        'created_at': plan.get('created_at', '')
    }


def _build_today_entry(plan):
    """构建 /plan/api/today-status 的单个计划返回结构（今日在场 + 在场日历）。"""
    today = _today_str()
    learn_hours = 0
    learn_presence = 0
    trade_hours = 0
    trade_presence = 0
    for c in plan.get('cards', []):
        if c['type'] == 'learn':
            learn_hours += _calc_slot_hours_by_date(c.get('slots', []), today)
            learn_presence += _calc_presence_days(c.get('slots', []))
        elif c['type'] == 'trade':
            trade_hours += _calc_slot_hours_by_date(c.get('slots', []), today)
            trade_presence += _calc_presence_days(c.get('slots', []))
    all_slots = [s for c in plan.get('cards', []) for s in c.get('slots', [])]
    daily_rule = plan.get('daily_rule', {})
    return {
        'plan_id': plan['id'],
        'plan_name': plan['name'],
        'today_learn_hours': round(learn_hours, 1),
        'today_trade_hours': round(trade_hours, 1),
        'today_present': (learn_hours + trade_hours) > 0,
        'learn_presence_days': learn_presence,
        'trade_presence_days': trade_presence,
        'welcome': daily_rule.get('welcome_message', '今天也想开始了吗？随时欢迎 👋'),
        'presence_calendar': _build_presence_calendar(all_slots)
    }


def _build_card_detail(plan, card):
    """构建 /plan/api/card-detail 的返回结构（单卡完整数据）。"""
    return {
        'id': card['id'],
        'plan_id': plan['id'],
        'type': card['type'],
        'round': card.get('round', 0),
        'title': card.get('title', ''),
        'goal': card.get('goal', ''),
        'reward': card.get('reward', 0),
        'base_reward': card.get('base_reward', card.get('reward', 0)),
        'hourly_rate': card.get('hourly_rate', 0),
        'status': card.get('status', 'pending'),
        'filled_count': _calc_total_hours(card.get('slots', [])),
        'locked': _is_card_locked(plan, card['id']),
        'hit_rate': _calc_card_hit_rate(card),
        'milestones': card.get('milestones', []),
        'todos': card.get('todos', []),
        'slots': card.get('slots', []),
        'notes': card.get('notes', []),
        'review': card.get('review', ''),
        'settlement': card.get('settlement'),
        'start_time': card.get('start_time', ''),
        'end_time': card.get('end_time', ''),
        'created_at': card.get('created_at', ''),
        'reward_info': _calc_card_reward(plan, card),
    }


def _build_refresh_payload(plan, card):
    """打卡变更后一次性返回前端局部刷新所需数据（纯内存计算，零额外 DB 往返）。

    fill/update/unfill/backfill 接口内部已 _load_plans + _save_plans，变更后的完整
    数据已在内存，直接复用上述构造器返回 plan/today/card 三块，前端据此就地更新
    总览、趋势图、计划详情、今日面板与卡片弹窗，无需再发 list/today-status/card-detail 三次请求。

    防御性降级：统计构造若因异常数据报错，返回 None，前端自动退回整表刷新，
    确保已成功保存的打卡操作不会因附带载荷构建失败而退化成 500。
    """
    try:
        return {
            'plan': _build_plan_info(plan),
            'today': _build_today_entry(plan),
            'card': _build_card_detail(plan, card),
        }
    except Exception:
        logger.warning('[Plan] refresh 载荷构建失败，前端将退回整表刷新', exc_info=True)
        return None


# =============================================================================
# API 路由
# =============================================================================

@plan_bp.route('/plan/api/list', methods=['GET'])
def api_list():
    """获取所有计划 + 统计数据 + 当日状态"""
    try:
        with session_scope() as session:
            data = _load_plans(session)
            plans = data.get('plans', [])
            result = []
            for plan in plans:
                result.append(_build_plan_info(plan))
        return jsonify({"code": 200, "message": "success", "data": result})
    except Exception as e:
        logger.error(f"[Plan] api_list 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/card-detail', methods=['GET'])
def api_card_detail():
    """按需获取单张任务卡的完整数据（slots/notes/milestones/review/settlement）

    列表接口只返回摘要，点击卡片打开详情弹窗时调本接口取完整数据，
    避免列表页一次性传输所有卡的 100 格明细。
    """
    try:
        plan_id = request.args.get('plan_id', '')
        card_id = request.args.get('card_id', '')
        if not plan_id or not card_id:
            return jsonify({"code": 400, "message": "plan_id 和 card_id 不能为空", "data": None})
        with session_scope() as session:
            data = _load_plans(session)
            plan = next((p for p in data.get('plans', []) if p['id'] == plan_id), None)
            if not plan:
                return jsonify({"code": 404, "message": "计划不存在", "data": None})
            card = next((c for c in plan.get('cards', []) if c['id'] == card_id), None)
            if not card:
                return jsonify({"code": 404, "message": "任务卡不存在", "data": None})
            detail = _build_card_detail(plan, card)
        return jsonify({"code": 200, "message": "success", "data": detail})
    except Exception as e:
        logger.error(f"[Plan] api_card_detail 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/create-plan', methods=['POST'])
def api_create_plan():
    """创建新计划"""
    try:
        body = request.get_json() or {}
        plan_type = body.get('type', 'custom')
        name = body.get('name', '自定义计划')

        new_plan = {
            "id": _new_id('plan'),
            "type": plan_type,
            "name": name,
            "grand_goal": body.get('grand_goal', ''),
            "total_hours": body.get('total_hours', 100),
            "round_count": body.get('round_count', 1),
            "per_round_hours": body.get('per_round_hours', 100),
            "daily_rule": {
                # 长期主义模式：无强制每日时长、无罚款，只有欢迎
                "mode": "longtermism",
                "min_record_minutes": 1,
                "early_bonus_multiplier": 1,
                "welcome_message": "今天也想开始了吗？随时欢迎 👋"
            },
            "cards": [],
            "created_at": _now_str()
        }

        # 如果是学习/交易模板，自动生成卡片
        if plan_type == 'learn':
            new_plan['cards'] = _create_learn_cards(new_plan['round_count'], body.get('base_reward', 2000))
        elif plan_type == 'trade':
            new_plan['round_config'] = {
                "base_reward_first": 2000,
                "base_reward_step": 2000,
                "early_bonus_multiplier": 1,
                "pass_avg_hourly_profit_pct": 1.0
            }
            new_plan['cards'] = _create_trade_cards(new_plan['round_count'])

        with session_scope() as session:
            data = _load_plans(session)
            data['plans'].append(new_plan)
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "创建成功", "data": new_plan})
    except Exception as e:
        logger.error(f"[Plan] api_create_plan 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/delete-plan', methods=['POST'])
def api_delete_plan():
    """删除计划"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        with session_scope() as session:
            data = _load_plans(session)
            data['plans'] = [p for p in data['plans'] if p['id'] != plan_id]
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "删除成功", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/create-card', methods=['POST'])
def api_create_card():
    """在指定计划下创建任务卡（手动创建，round 从 11 开始）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_type = body.get('type', 'learn')

        with session_scope() as session:
            data = _load_plans(session)
            plan = None
            for p in data['plans']:
                if p['id'] == plan_id:
                    plan = p
                    break
            if not plan:
                return jsonify({"code": 404, "message": "计划不存在", "data": None})

            # 计算 round：找到最大 round + 1（至少 11）
            max_round = max((c.get('round', 0) for c in plan.get('cards', [])), default=10)
            new_round = max(max_round + 1, 11)

            if card_type == 'learn':
                # 默认继承上一张学习卡奖励（不传 reward 时）
                prev_rewards = [c.get('reward', 2000) for c in plan.get('cards', []) if c['type'] == 'learn']
                reward = body.get('reward', prev_rewards[-1] if prev_rewards else 2000)
                new_card = {
                    "id": _new_id('learn'),
                    "type": "learn",
                    "round": new_round,
                    "title": body.get('title', f'学习任务{new_round} · 100小时'),
                    "goal": body.get('goal', ''),
                    "reward": reward,
                    "start_time": _normalize_datetime_str(body.get('start_time', '')) or '',
                    "end_time": _normalize_datetime_str(body.get('end_time', '')) or '',
                    "status": "pending",
                    "milestones": [],
                    "slots": _create_empty_slots(100),
                    "notes": [],
                    "review": "",
                    "settlement": None,
                    "created_at": _now_str(),
                    "updated_at": _now_str()
                }
            else:
                # 默认继承上一张交易卡基础奖金 + 2000（不传 base_reward 时）
                prev_trades = [c for c in plan.get('cards', []) if c['type'] == 'trade']
                if prev_trades:
                    prev_base = prev_trades[-1].get('base_reward') or prev_trades[-1].get('reward', 2000)
                    default_base = prev_base + 2000
                else:
                    default_base = 2000
                base_reward = body.get('base_reward', default_base)
                hourly_rate = base_reward // 100
                new_card = {
                    "id": _new_id('trade'),
                    "type": "trade",
                    "round": new_round,
                    "title": body.get('title', f'第{new_round}轮 · 翻倍挑战'),
                    "goal": body.get('goal', ''),
                    "reward": base_reward,
                    "base_reward": base_reward,
                    "hourly_rate": hourly_rate,
                    "start_time": _normalize_datetime_str(body.get('start_time', '')) or '',
                    "end_time": _normalize_datetime_str(body.get('end_time', '')) or '',
                    "status": "pending",
                    "milestones": [],
                    "slots": _create_empty_slots(100),
                    "notes": [],
                    "review": "",
                    "settlement": None,
                    "created_at": _now_str(),
                    "updated_at": _now_str()
                }

            # 如果当前没有进行中的卡，新卡直接进入进行中（否则永远无法打卡）
            if not any(c.get('status') == 'in_progress' for c in plan.get('cards', [])):
                new_card['status'] = 'in_progress'
                if not new_card['start_time']:
                    new_card['start_time'] = _now_str()

            plan['cards'].append(new_card)
            _save_plans(session, data)
        return jsonify({"code": 200, "message": "创建成功", "data": new_card})
    except Exception as e:
        logger.error(f"[Plan] api_create_card 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/update-card', methods=['POST'])
def api_update_card():
    """更新任务卡（目标/状态/复盘/奖励等）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        fields = body.get('fields', {})

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    for key, value in fields.items():
                        if key in ('milestones', 'todos', 'start_time', 'end_time', 'status', 'goal', 'reward', 'review', 'notes'):
                            # TodoList 只接受列表结构，防止脏数据写入
                            if key == 'todos' and not isinstance(value, list):
                                return jsonify({"code": 400, "message": "todos 字段必须为数组", "data": None})
                            # 开始/结束时间：校验并统一存储为 YYYY-MM-DD HH:MM:SS
                            if key in ('start_time', 'end_time'):
                                normalized = _normalize_datetime_str(value)
                                if normalized is None:
                                    return jsonify({"code": 400, "message": "时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
                                value = normalized
                            card[key] = value

                    # 交易卡修改奖励时同步基础奖金与时薪
                    if card['type'] == 'trade' and 'reward' in fields:
                        card['base_reward'] = fields['reward']
                        card['hourly_rate'] = fields['reward'] // 100

                    # 放弃任务时解锁下一张卡
                    if fields.get('status') == 'abandoned' and card.get('status') == 'abandoned':
                        _unlock_next_card(plan, card)

                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "更新成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/delete-card', methods=['POST'])
def api_delete_card():
    """删除任务卡"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                plan['cards'] = [c for c in plan['cards'] if c['id'] != card_id]
                _save_plans(session, data)
                return jsonify({"code": 200, "message": "删除成功", "data": None})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/fill-slot', methods=['POST'])
def api_fill_slot():
    """勾选格子（填写小时记录）

    参数:
        plan_id, card_id, slot_index,
        record: 学习卡 {content, duration_minutes}；
                交易卡 {prediction, duration_minutes, actual,
                        market_analysis, action_advice, account_balance}
        filled_at: 可选，自定义打卡时间（补录历史记录），格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')
        record = body.get('record', {})
        custom_filled_at = body.get('filled_at', '').strip()

        if slot_index is None or slot_index < 0 or slot_index >= 100:
            return jsonify({"code": 400, "message": "格子索引无效(0~99)", "data": None})

        # 验证自定义时间格式（统一规范化为 YYYY-MM-DD HH:MM:SS）
        if custom_filled_at:
            normalized = _normalize_datetime_str(custom_filled_at)
            if normalized is None:
                return jsonify({"code": 400, "message": "时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
            custom_filled_at = normalized

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue

                    # 检查串行锁定：只有 in_progress 的卡才能勾选
                    if card['status'] != 'in_progress':
                        return jsonify({"code": 403, "message": "当前卡不可勾选（状态: " + card['status'] + "）", "data": None})

                    slots = card.get('slots', [])
                    if slot_index >= len(slots):
                        return jsonify({"code": 400, "message": "格子索引超出范围", "data": None})

                    slot = slots[slot_index]
                    if slot.get('filled'):
                        return jsonify({"code": 400, "message": "该格子已勾选", "data": None})

                    # 交易卡：先过分析纪律闸门（先分析记录，然后才允许打卡），
                    # 再按新规范字段结构化，与前端提交格式保持一致
                    analysis = None
                    if card['type'] == 'trade':
                        allowed, analysis, block = _discipline_gate(session, custom_filled_at)
                        if not allowed:
                            return _gate_block_response(block, _gate_message(block))
                        record = _normalize_trade_record(record, analysis)

                    # 填写记录（支持自定义时间补录，默认当前时间）
                    slot['filled'] = True
                    slot['filled_at'] = custom_filled_at if custom_filled_at else _now_str()
                    slot['record'] = record

                    # 交易回填后的自动计算
                    if card['type'] == 'trade' and record.get('prediction') and record.get('actual'):
                        record['hit'] = (record['prediction'] == record['actual'])

                    card['updated_at'] = _now_str()

                    # 检查是否满 100 格（学习卡自动完成；交易卡由用户确认翻倍后手动结算）
                    filled_count = _calc_total_hours(slots)
                    if card['type'] == 'learn' and filled_count >= 100:
                        _settle_card(plan, card)

                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "✅ 勾选成功 · 欢迎回来，长期主义者 🌱", "data": {
                        "slot": slot,
                        "card_status": card['status'],
                        "filled_count": filled_count,
                        "refresh": _build_refresh_payload(plan, card)
                    }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_fill_slot 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/update-slot', methods=['POST'])
def api_update_slot():
    """更新格子记录（如回填实际涨跌）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')
        record = body.get('record', {})

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    slots = card.get('slots', [])
                    if slot_index < 0 or slot_index >= len(slots):
                        return jsonify({"code": 400, "message": "格子索引无效", "data": None})
                    slot = slots[slot_index]
                    if not slot.get('filled'):
                        return jsonify({"code": 400, "message": "该格子未勾选，无法更新", "data": None})

                    # 交易卡记录按新规范字段结构化，与前端提交格式保持一致
                    if card['type'] == 'trade':
                        # 分析关联字段以库内既有值为准（表单不回传这些字段，
                        # 避免“回填实际涨跌”时把打卡与分析记录的关联抹除）
                        prev = slot.get('record') or {}
                        keep_analysis = {
                            'analysis_ids': prev.get('analysis_ids') or record.get('analysis_ids') or [],
                            'analysis_hour': prev.get('analysis_hour') or record.get('analysis_hour') or '',
                            'bypass_analysis': bool(prev.get('bypass_analysis')
                                                    or record.get('bypass_analysis')),
                        }
                        slot['record'] = _normalize_trade_record(record, keep_analysis)
                    elif slot['record'] is None:
                        slot['record'] = record
                    else:
                        slot['record'].update(record)

                    # 重新计算命中
                    r = slot['record']
                    if card['type'] == 'trade' and r.get('prediction') and r.get('actual'):
                        r['hit'] = (r['prediction'] == r['actual'])

                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "更新成功", "data": {
                        "slot": slot,
                        "refresh": _build_refresh_payload(plan, card)
                    }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/unfill-slot', methods=['POST'])
def api_unfill_slot():
    """删除单个打卡记录（取消勾选格子）

    参数: plan_id, card_id, slot_index
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        slot_index = body.get('slot_index')

        if slot_index is None or slot_index < 0 or slot_index >= 100:
            return jsonify({"code": 400, "message": "格子索引无效(0~99)", "data": None})

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    slots = card.get('slots', [])
                    if slot_index >= len(slots):
                        return jsonify({"code": 400, "message": "格子索引超出范围", "data": None})
                    slot = slots[slot_index]
                    if not slot.get('filled'):
                        return jsonify({"code": 400, "message": "该格子未勾选，无需删除", "data": None})

                    slot['filled'] = False
                    slot['filled_at'] = ''
                    slot['record'] = None

                    # 若已结算且不满足通关条件（不足 100 小时且子目标未全部完成），撤销结算并重算串行链
                    if card.get('settlement') and _calc_total_hours(slots) < 100 and not _calc_milestones_all_done(card):
                        card['settlement'] = None
                        card['status'] = 'pending'
                        _recompute_serial_chain(plan)

                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "删除成功", "data": {
                        "card_status": card['status'],
                        "filled_count": _calc_total_hours(slots),
                        "refresh": _build_refresh_payload(plan, card)
                    }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_unfill_slot 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/reset-card', methods=['POST'])
def api_reset_card():
    """重置任务卡：清空所有打卡记录、小记、目标拆解、结算，恢复到初始状态

    参数: plan_id, card_id
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    # 保留配置（标题/目标/奖励），清空全部过程数据
                    card['slots'] = _create_empty_slots(100)
                    card['milestones'] = []
                    card['todos'] = []
                    card['notes'] = []
                    card['review'] = ''
                    card['settlement'] = None
                    card['start_time'] = ''
                    card['end_time'] = ''
                    card['status'] = 'pending'
                    # 重算串行链（若前面卡都已结束，本卡自动恢复为进行中）
                    _recompute_serial_chain(plan)
                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "重置成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_reset_card 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/add-note', methods=['POST'])
def api_add_note():
    """添加过程小记"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        content = body.get('content', '')

        if not content:
            return jsonify({"code": 400, "message": "内容不能为空", "data": None})

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    note = {
                        "time": _now_str(),
                        "content": content
                    }
                    card['notes'].append(note)
                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "添加成功", "data": note})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/toggle-milestone', methods=['POST'])
def api_toggle_milestone():
    """切换子目标完成状态"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        milestone_index = body.get('milestone_index')

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    milestones = card.get('milestones', [])
                    if milestone_index < 0 or milestone_index >= len(milestones):
                        return jsonify({"code": 400, "message": "子目标索引无效", "data": None})
                    milestones[milestone_index]['done'] = not milestones[milestone_index]['done']
                    card['updated_at'] = _now_str()
                    _save_plans(session, data)
                    # 返回子目标完成状态与提前通关资格，供前端引导结算
                    all_done = _calc_milestones_all_done(card)
                    early_eligible = _calc_early_eligible(card)
                    return jsonify({"code": 200, "message": "切换成功", "data": {
                        "milestones": card['milestones'],
                        "all_done": all_done,
                        "early_eligible": early_eligible,
                        "card_status": card['status'],
                        "filled_count": _calc_total_hours(card.get('slots', []))
                    }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/settle-round', methods=['POST'])
def api_settle_round():
    """手动触发轮次结算（交易卡需传入 profit_multiplier 资金倍数）"""
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        profit_multiplier = body.get('profit_multiplier')

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue
                    if card['status'] == 'completed' or card['status'] == 'failed':
                        return jsonify({"code": 400, "message": "已结算，无需重复结算", "data": card.get('settlement')})

                    # 交易卡必须提供资金倍数；学习卡可自动/提前通关（满100小时或子目标全部完成）
                    if card['type'] == 'trade':
                        if profit_multiplier is None:
                            return jsonify({"code": 400, "message": "请提供当前资金倍数（本金 × N）", "data": None})
                        if profit_multiplier < 2 and _calc_total_hours(card.get('slots', [])) < 100:
                            return jsonify({"code": 400, "message": "未翻倍且未满 100 小时，暂不能结算", "data": None})
                    else:
                        if _calc_total_hours(card.get('slots', [])) < 100 and not _calc_milestones_all_done(card):
                            return jsonify({"code": 400, "message": "未满 100 小时且子目标未全部完成，暂不能结算", "data": None})

                    _settle_card(plan, card, profit_multiplier)
                    _save_plans(session, data)
                    return jsonify({"code": 200, "message": "结算成功", "data": card})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/account-balance', methods=['GET', 'POST'])
def api_account_balance():
    """获取当前 OKX 账户总权益（USDT），供交易打卡时自动填入

    后端代理调用 OKX /api/v5/account/balance（密钥仅存服务端，不暴露给前端）：
    默认读取主账号凭证，可通过 account 参数指定其他账号。
    返回: { totalEq: '12345.67', available: '8901.23', updateTime: '2026-08-22 12:00:00' }
    查询失败时返回 code=500 并附带错误信息，前端可降级为空值。
    """
    try:
        body = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
        acct = str(body.get('account') or request.args.get('account') or '').strip()

        from api_config import get_api_config
        config = get_api_config(acct if acct else None)
        import okx.Account as Account
        account_api = Account.AccountAPI(
            config['api_key'], config['secret_key'],
            config['passphrase'], False, config['flag']
        )
        # 只读接口统一限频/退避（问题#8）：复用 api_routes 的节流出口，
        # 不另写一份；延迟导入避开蓝图互相依赖，导入失败则直连原接口
        try:
            from .api_routes import _rl_call as _rl
        except ImportError:
            _rl = None
        result = (_rl(account_api, 'get_account_balance') if _rl
                  else account_api.get_account_balance())
        if result.get('code') != '0' or not result.get('data'):
            return jsonify({
                'code': 500,
                'message': result.get('msg', 'OKX API 查询失败'),
                'data': None
            })
        acct_data = result['data'][0]
        total_eq_raw = acct_data.get('totalEq', '0')

        # 查询成功后追加余额快照（与 /api/account/balance-history 一致），
        # 为「历史补录」的范围余额数据源持续积累本地快照
        try:
            from .api_routes import _append_balance_snapshot
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            _append_balance_snapshot(config['account'], now_ms, round(float(total_eq_raw or 0), 4))
        except Exception as snap_err:
            logger.warning(f"[Plan] 追加余额快照失败（不影响余额返回）: {snap_err}")

        return jsonify({
            'code': 200,
            'message': 'success',
            'data': {
                'totalEq': total_eq_raw,
                'available': acct_data.get('adjEqy', '0'),
                'updateTime': _ts_to_str(acct_data.get('uTime', '0')),
                'account': config['account'],
                'account_name': config.get('account_name', config['account'])
            }
        })
    except Exception as e:
        logger.error(f"[Plan] api_account_balance 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/backfill-balances', methods=['GET'])
def api_backfill_balances():
    """历史补录数据源：按日期范围返回本地余额快照的每日余额点

    仅查询本地 balance_history 快照表，不实时调用 OKX 接口，响应快且无签名风险。
    参数: start=YYYY-MM-DD, end=YYYY-MM-DD（必填，范围 ≤62 天）
    返回: days: [{date, balance, source}]，每天一条（取当日最后一个快照点），
          当日无快照时 balance 为 null。
    """
    try:
        start = (request.args.get('start') or '').strip()
        end = (request.args.get('end') or '').strip()
        try:
            d_start = datetime.datetime.strptime(start, '%Y-%m-%d')
            d_end = datetime.datetime.strptime(end, '%Y-%m-%d')
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式不正确，应为 YYYY-MM-DD', 'data': None})
        if d_start > d_end:
            return jsonify({'code': 400, 'message': '开始日期不能晚于结束日期', 'data': None})
        if (d_end - d_start).days > 62:
            return jsonify({'code': 400, 'message': '单次补录范围不能超过 62 天', 'data': None})

        from api_config import get_api_config
        from . import balance_repo
        config = get_api_config()
        account_key = config['account']

        start_ms = int(d_start.timestamp() * 1000)
        end_ms = int((d_end + datetime.timedelta(days=1)).timestamp() * 1000)

        with session_scope() as session:
            points = balance_repo.load_account_points(session, account_key)

        # 按日聚合：每天取时间戳最晚的一个快照点
        daily = {}
        for p in points:
            try:
                ts = int(p.get('ts') or 0)
            except (TypeError, ValueError):
                continue
            if ts < start_ms or ts >= end_ms:
                continue
            date_str = datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
            prev = daily.get(date_str)
            if prev is None or ts > prev['ts']:
                daily[date_str] = {'ts': ts, 'balance': p.get('balance'), 'source': p.get('source') or 'snapshot'}

        # 逐日生成结果（无快照的日期 balance=None，前端提示「无余额数据」）
        days = []
        cursor = d_start
        while cursor <= d_end:
            date_str = cursor.strftime('%Y-%m-%d')
            p = daily.get(date_str)
            balance_val = None
            if p and p.get('balance') is not None:
                try:
                    balance_val = round(float(p['balance']), 2)
                except (TypeError, ValueError):
                    balance_val = None
            days.append({
                'date': date_str,
                'balance': balance_val,
                'source': p['source'] if p else None
            })
            cursor += datetime.timedelta(days=1)

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'account': account_key,
            'account_name': config.get('account_name', account_key),
            'days': days
        }})
    except Exception as e:
        logger.error(f"[Plan] api_backfill_balances 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/backfill-batch', methods=['POST'])
def api_backfill_batch():
    """批量历史补录：将多条记录按时间先后依次填入空格子

    参数: plan_id, card_id,
          entries: [{filled_at: 'YYYY-MM-DD HH:MM', record: {...}}]
    依次占用空格子（0→99），空格子不足时多余条目跳过并在 skipped 中返回；
    交易卡记录走 _normalize_trade_record 规范化字段，prediction+actual 齐全时自动计算 hit。

    分析纪律（批次11）：交易卡逐条按各自历史槽独立判定闸门。
    不合格的条目不写入，汇总到 blocked/blocked_slots 返回（而不是整批 403），
    前端据此引导“为这些小时生成回溯分析”后重提，避免补一次历史被打回几十次。
    """
    try:
        body = request.get_json() or {}
        plan_id = body.get('plan_id', '')
        card_id = body.get('card_id', '')
        entries = body.get('entries') or []
        if not isinstance(entries, list) or not entries:
            return jsonify({"code": 400, "message": "补录列表不能为空", "data": None})
        if len(entries) > 100:
            return jsonify({"code": 400, "message": "单次补录不能超过 100 条", "data": None})

        # 逐条校验并规范化时间，随后按时间升序填入
        norm_entries = []
        for e in entries:
            if not isinstance(e, dict):
                return jsonify({"code": 400, "message": "补录条目格式不正确", "data": None})
            fa = _normalize_datetime_str(e.get('filled_at') or '')
            if not fa:
                return jsonify({"code": 400, "message": "补录时间格式不正确，应为 YYYY-MM-DD HH:MM", "data": None})
            norm_entries.append((fa, e.get('record') or {}))
        norm_entries.sort(key=lambda x: x[0])

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data['plans']:
                if plan['id'] != plan_id:
                    continue
                for card in plan.get('cards', []):
                    if card['id'] != card_id:
                        continue

                    # 与 fill-slot 保持一致的串行锁定校验
                    if card['status'] != 'in_progress':
                        return jsonify({"code": 403, "message": "当前卡不可补录（状态: " + card['status'] + "）", "data": None})

                    slots = card.get('slots', [])
                    free_indexes = [i for i, s in enumerate(slots) if not s.get('filled')]
                    is_trade = card['type'] == 'trade'

                    # 闸门开启时才逐条判定；同一小时槽只判一次（范围补录常多日同一时刻）
                    gate_cfg = None
                    if is_trade:
                        try:
                            from . import discipline_repo as _disc
                            _cfg = _disc.load_config()
                            if _cfg.get('enabled', True):
                                gate_cfg = _cfg
                        except Exception as e:
                            logger.warning(f'[Plan] 补录闸门不可用，按无闸门处理: {e}')
                    gate_cache = {}

                    filled = 0
                    blocked = []
                    for fa, record in norm_entries:
                        if filled >= len(free_indexes):
                            break
                        analysis = None
                        if gate_cfg is not None:
                            slot_key = fa[:13]
                            if slot_key not in gate_cache:
                                gate_cache[slot_key] = _discipline_gate(session, fa, gate_cfg)
                            allowed, analysis, block = gate_cache[slot_key]
                            if not allowed:
                                blocked.append({
                                    'filled_at': fa,
                                    'hour_slot': slot_key,
                                    'required': block.get('required'),
                                    'actual': block.get('actual'),
                                    'missing_coins': block.get('missing_coins') or [],
                                })
                                continue
                        slot = slots[free_indexes[filled]]
                        if is_trade:
                            record = _normalize_trade_record(record, analysis)
                            if record.get('prediction') and record.get('actual'):
                                record['hit'] = (record['prediction'] == record['actual'])
                        slot['filled'] = True
                        slot['filled_at'] = fa
                        slot['record'] = record
                        filled += 1

                    # 全部条目都被闸门挡住：整批 403，直接引导去补回溯分析
                    if blocked and filled == 0:
                        blocked_slots = sorted({b['hour_slot'] for b in blocked})
                        return jsonify({"code": 403, "message": (
                            f"📝 先分析，再打卡：{len(blocked)} 条补录全部被拦（缺 {len(blocked_slots)} 个小时的分析记录）"),
                            "data": {
                                "need_analysis": True,
                                "filled": 0,
                                "blocked": blocked,
                                "blocked_slots": blocked_slots,
                            }})

                    card['updated_at'] = _now_str()
                    filled_count = _calc_total_hours(slots)
                    # 学习卡满 100 格自动结算（与 fill-slot 逻辑一致）
                    if card['type'] == 'learn' and filled_count >= 100:
                        _settle_card(plan, card)
                    _save_plans(session, data)
                    blocked_slots = sorted({b['hour_slot'] for b in blocked})
                    msg = f"补录成功，共 {filled} 条"
                    if blocked:
                        msg += f"；{len(blocked)} 条因缺分析记录被拦（涉 {len(blocked_slots)} 个小时）"
                    return jsonify({"code": 200, "message": msg, "data": {
                        "filled": filled,
                        "skipped": len(norm_entries) - filled - len(blocked),
                        "blocked": blocked,
                        "blocked_slots": blocked_slots,
                        "filled_count": filled_count,
                        "card_status": card['status'],
                        "refresh": _build_refresh_payload(plan, card)
                    }})

        return jsonify({"code": 404, "message": "卡片不存在", "data": None})
    except Exception as e:
        logger.error(f"[Plan] api_backfill_batch 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@plan_bp.route('/plan/api/list-accounts', methods=['GET'])
def api_list_accounts():
    """获取可选 OKX 账号列表（供前端账号选择器使用）"""
    try:
        from api_config import list_accounts
        return jsonify({'code': 200, 'message': 'success', 'data': list_accounts()})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@plan_bp.route('/plan/api/today-status', methods=['GET'])
def api_today_status():
    """获取今日状态汇总（长期主义版：在场状态 + 累计在场天数，无罚款）

    只报告三件事：今天来了吗、来了多久、已经坚持了几天。
    绝不显示"缺口/达标/罚款"——那些只会带来心理负担。
    """
    try:
        result = []

        with session_scope() as session:
            data = _load_plans(session)
            for plan in data.get('plans', []):
                result.append(_build_today_entry(plan))

        return jsonify({"code": 200, "message": "success", "data": result})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e), "data": None})
