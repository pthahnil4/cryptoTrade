#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
监控告警引擎 — 异常行情与持仓盈亏监控报警（迁移批次9）
=========================================================

两类监控：
1. 行情异动 (price)：监控币种在 N 分钟窗口内的涨跌幅，
   超 warning/critical 阈值分别触发预警/严重告警，回落后发缓解告警。
2. 持仓盈亏 (pnl)：监控指定账号的永续合约持仓浮动盈亏率（标的价格口径，
   多头 (现价-均价)/均价、空头 (均价-现价)/均价），亏损超阈值告警。

【级别递进状态机】（运行时状态存 kv_store key='alert_runtime_state'）
- 新命中           → 发送告警，记录级别
- 同级别重复命中   → 抑制不发（alert_log 仍留档，notify_sent=0）
- warning→critical → 升级发送
- critical→warning → 降级抑制（仍在风险区，不重复打扰）
- 回落至阈值以内   → 发送 recover 缓解告警，清除状态

【邮件】同一检测轮多个命中合并为一封摘要邮件（防批量轰炸），
单命中发专用邮件；冷却完全由上述状态机控制，不叠加通知器冷却。

【旁路约定】alert_log 落库失败只记日志，不阻断监控主循环。

配置存 kv_store key='alert_config'（config_store_repo 整份存取），
由 alert_routes 的配置保存接口热更新并调 register_alert_job 重注册。
"""

import copy
import datetime
import logging

# 双模式导入：Flask 包内（crypto.task.monitor）/ 独立脚本（task 目录在 sys.path）
try:
    from ..utils.logger import get_task_logger
    from ..notification.message_notifier import MessageNotifier
except ImportError:
    from utils.logger import get_task_logger
    from notification.message_notifier import MessageNotifier

try:
    from crypto.database import session_scope
    from crypto import config_store_repo
    from crypto.models import AlertLog
    from crypto.api_config import get_api_config
except ImportError:
    session_scope = None
    config_store_repo = None
    AlertLog = None
    get_api_config = None

import okx.MarketData as MarketData
import okx.Account as Account

logger = logging.getLogger(__name__)

_MARKET_API = None


def _get_market_api():
    """模块级单例：复用 OKX 行情客户端，避免每轮新建连接/会话 churn"""
    global _MARKET_API
    if _MARKET_API is None:
        _MARKET_API = MarketData.MarketAPI(flag='0')
    return _MARKET_API

task_log = get_task_logger()

# kv_store 键名
KEY_ALERT_CONFIG = 'alert_config'
KEY_ALERT_RUNTIME_STATE = 'alert_runtime_state'

# 调度任务 ID（与 scheduler 注册/热重注册口径一致）
ALERT_JOB_ID = 'alert_monitor'

# 默认配置（页面未保存过配置时使用；数值均可在风险警报页调整）
DEFAULT_ALERT_CONFIG = {
    'enabled': True,              # 监控总开关
    'interval_seconds': 300,      # 检测周期（秒）
    'account': '',                # 持仓监控账号（空=默认账号）
    'price': {
        'enabled': True,          # 行情异动监控开关
        'inst_ids': [],           # 监控币种；为空时跟随实盘交易配置的 currencies
        'window_minutes': 15,     # 涨跌幅检测窗口（分钟）
        'warning_pct': 5.0,       # 预警阈值（绝对值 %）
        'critical_pct': 8.0,      # 严重阈值（绝对值 %）
    },
    'pnl': {
        'enabled': True,          # 持仓盈亏监控开关
        'warning_pct': -10.0,     # 预警阈值（浮亏 %，负值）
        'critical_pct': -20.0,    # 严重阈值（浮亏 %，负值）
    },
}

# 最近一轮检测摘要（供 alert_routes 状态接口展示）
_LAST_RUN = {
    'finished_at': None,     # 上一轮结束时间
    'duration_ms': 0,        # 耗时
    'hit_count': 0,          # 命中数（含被抑制）
    'sent_count': 0,         # 实际发出邮件的命中数
    'email_ok': None,        # 汇总邮件是否发送成功（无命中时为 None）
    'error': None,           # 本轮异常信息
}


def get_last_run() -> dict:
    """最近一轮检测摘要（副本）"""
    return dict(_LAST_RUN)


# =============================================================================
# 配置 / 运行时状态存取（DB 优先，失败降级）
# =============================================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """默认配置深合并用户配置，保证新增字段自动补齐"""
    merged = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_alert_config() -> dict:
    """读取监控配置：带 TTL 缓存的 kv_store 优先，DB 不可用/无数据时用默认配置；
    save_alert_config 写入时写穿透失效缓存"""
    if session_scope is None or config_store_repo is None:
        return copy.deepcopy(DEFAULT_ALERT_CONFIG)
    try:
        saved = config_store_repo.load_json_config_cached(KEY_ALERT_CONFIG)
        return _deep_merge(DEFAULT_ALERT_CONFIG, saved or {})
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取监控配置失败，使用默认配置: {e}')
        return copy.deepcopy(DEFAULT_ALERT_CONFIG)


def save_alert_config(cfg: dict):
    """整份覆盖保存监控配置到 kv_store"""
    with session_scope() as s:
        config_store_repo.save_json_config(s, KEY_ALERT_CONFIG, cfg)


def _load_runtime_state() -> dict:
    """读取级别递进状态机；DB 不可用/无数据时返回空（本轮按新命中处理）"""
    if session_scope is None or config_store_repo is None:
        return {}
    try:
        with session_scope() as s:
            state = config_store_repo.load_json_config(s, KEY_ALERT_RUNTIME_STATE)
        return state if isinstance(state, dict) else {}
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取运行时状态失败: {e}')
        return {}


def _save_runtime_state(state: dict):
    try:
        with session_scope() as s:
            config_store_repo.save_json_config(s, KEY_ALERT_RUNTIME_STATE, state)
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 保存运行时状态失败（不影响本轮告警发送）: {e}')


# =============================================================================
# 数据获取
# =============================================================================

def _resolve_price_symbols(cfg: dict) -> list:
    """行情监控币种：配置显式指定优先；为空时跟随实盘交易配置的 currencies"""
    inst_ids = [x.strip() for x in (cfg.get('price', {}).get('inst_ids') or []) if str(x).strip()]
    if inst_ids:
        return inst_ids
    try:
        trading_cfg = config_store_repo.load_json_config_cached(
            config_store_repo.KEY_STRATEGY_CONFIG)
        return [c.get('instId', '') for c in (trading_cfg or {}).get('currencies', [])
                if c.get('instId')]
    except Exception as e:
        task_log.warning(f'[AlertMonitor] 读取实盘交易配置失败，行情监控本轮跳过: {e}')
        return []


def _fetch_window_change_pct(market_api, inst_id: str, window_minutes: int):
    """取 1m K 线计算窗口内涨跌幅 %；数据不足返回 None"""
    limit = min(int(window_minutes) + 2, 100)
    res = market_api.get_candlesticks(instId=inst_id, bar='1m', limit=str(limit))
    data = (res or {}).get('data') or []
    if len(data) < 2:
        return None
    # OKX 返回新→旧，反转为旧→新；用窗口起点收盘与最新收盘对比
    closes = [float(row[4]) for row in reversed(data)]
    base, last = closes[0], closes[-1]
    if base <= 0:
        return None
    return (last / base - 1.0) * 100.0


def _fetch_positions(account: str):
    """查询指定账号的永续合约有效持仓（原始字段，含 avgPx/last/pos/posSide）"""
    api_cfg = get_api_config(account or None)
    account_api = Account.AccountAPI(
        api_cfg['api_key'], api_cfg['secret_key'], api_cfg['passphrase'],
        False, api_cfg['flag'])
    res = account_api.get_positions(instType='SWAP')
    if not res or res.get('code') != '0':
        raise RuntimeError(f"持仓查询失败: {(res or {}).get('msg', res)}")
    positions = []
    for pos in res.get('data', []):
        try:
            if float(pos.get('pos', '0') or 0) == 0:
                continue
        except (TypeError, ValueError):
            continue
        positions.append(pos)
    return positions


def _position_pnl_pct(pos: dict):
    """按标的价格口径计算浮动盈亏率 %（多头 (现价-均价)/均价，空头反向）。

    返回 (pnl_pct, direction, avg_px, last_px)，数据缺失返回 None。
    """
    try:
        avg_px = float(pos.get('avgPx') or 0)
        last_px = float(pos.get('last') or 0)
        qty = float(pos.get('pos') or 0)
    except (TypeError, ValueError):
        return None
    if avg_px <= 0 or last_px <= 0 or qty == 0:
        return None
    pos_side = pos.get('posSide', 'net')
    if pos_side == 'short' or (pos_side == 'net' and qty < 0):
        direction = '空头'
        pnl_pct = (avg_px - last_px) / avg_px * 100.0
    else:
        direction = '多头'
        pnl_pct = (last_px - avg_px) / avg_px * 100.0
    return pnl_pct, direction, avg_px, last_px


# =============================================================================
# 级别递进状态机
# =============================================================================

def _classify_price_level(change_pct: float, price_cfg: dict) -> str:
    """行情异动级别判定（阈值取绝对值比较）；未命中返回 ''"""
    if abs(change_pct) >= float(price_cfg.get('critical_pct', 8.0)):
        return 'critical'
    if abs(change_pct) >= float(price_cfg.get('warning_pct', 5.0)):
        return 'warning'
    return ''


def _classify_pnl_level(pnl_pct: float, pnl_cfg: dict) -> str:
    """持仓盈亏级别判定（阈值为负值，亏损越深级别越高）；未命中返回 ''"""
    if pnl_pct <= float(pnl_cfg.get('critical_pct', -20.0)):
        return 'critical'
    if pnl_pct <= float(pnl_cfg.get('warning_pct', -10.0)):
        return 'warning'
    return ''


_LEVEL_RANK = {'': 0, 'warning': 1, 'critical': 2}


def _transition(state: dict, key: str, level: str):
    """状态机迁移，返回 (动作, 需落库的记录级别)。

    动作: 'send' 发送 | 'suppress' 抑制 | 'recover' 缓解 | 'none' 无动作
    """
    prev_level = (state.get(key) or {}).get('level', '')
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if level:
        if prev_level == level:
            return 'suppress', level          # 同级别重复命中 → 抑制
        if _LEVEL_RANK[level] > _LEVEL_RANK[prev_level]:
            state[key] = {'level': level, 'updated_at': now_str}
            return 'send', level              # 新命中或升级 → 发送
        # critical → warning 降级：仍在风险区，不打扰
        state[key] = {'level': level, 'updated_at': now_str}
        return 'suppress', level
    if prev_level:
        state.pop(key, None)
        return 'recover', 'recover'           # 回落阈值以内 → 缓解
    return 'none', ''


# =============================================================================
# 检测主循环
# =============================================================================

def run_alert_check():
    """执行一轮监控检测（调度器周期调用 / 页面手动触发共用）"""
    global _LAST_RUN
    started = datetime.datetime.now()
    run_id = started.strftime('A%Y%m%d-%H%M%S')
    hits = []          # 全部命中（含抑制），写 alert_log
    notify_items = []  # 实际需发送的命中
    error = None

    try:
        cfg = load_alert_config()
        if not cfg.get('enabled'):
            task_log.info(f'[{run_id}] 监控告警已关闭，本轮跳过')
            return

        state = _load_runtime_state()
        price_cfg, pnl_cfg = cfg.get('price', {}), cfg.get('pnl', {})
        market_api = _get_market_api()

        # ---------- 行情异动检测 ----------
        if price_cfg.get('enabled'):
            window = max(1, int(price_cfg.get('window_minutes', 15)))
            for inst_id in _resolve_price_symbols(cfg):
                try:
                    change = _fetch_window_change_pct(market_api, inst_id, window)
                except Exception as e:
                    task_log.warning(f'[{run_id}] {inst_id} K线获取失败: {e}')
                    continue
                if change is None:
                    continue
                level = _classify_price_level(change, price_cfg)
                action, rec_level = _transition(state, f'price|{inst_id}', level)
                threshold = float(price_cfg.get('critical_pct', 8.0) if level == 'critical'
                                  else price_cfg.get('warning_pct', 5.0))
                if action == 'none':
                    continue
                if action == 'recover':
                    threshold = float(price_cfg.get('warning_pct', 5.0))
                message = f'{inst_id} 近{window}分钟涨跌幅 {change:+.2f}%'
                item = {'type': 'price', 'inst_id': inst_id, 'level': rec_level,
                        'value': change, 'threshold': threshold, 'message': message,
                        'sent': action in ('send', 'recover'),
                        'window_minutes': window}
                hits.append(item)
                if item['sent']:
                    notify_items.append(item)
                    task_log.info(f'[{run_id}] 【行情告警-{rec_level}】{message}')
                else:
                    task_log.info(f'[{run_id}] 【行情告警-抑制】{message}（级别未变化）')

        # ---------- 持仓盈亏检测 ----------
        if pnl_cfg.get('enabled'):
            try:
                positions = _fetch_positions(cfg.get('account', ''))
            except Exception as e:
                task_log.warning(f'[{run_id}] 持仓查询失败，盈亏监控本轮跳过: {e}')
                positions = None
            if positions is not None:
                for pos in positions:
                    calc = _position_pnl_pct(pos)
                    if calc is None:
                        continue
                    pnl_pct, direction, avg_px, last_px = calc
                    inst_id = pos.get('instId', '')
                    key = f'pnl|{inst_id}|{direction}'
                    level = _classify_pnl_level(pnl_pct, pnl_cfg)
                    action, rec_level = _transition(state, key, level)
                    threshold = float(pnl_cfg.get('critical_pct', -20.0) if level == 'critical'
                                      else pnl_cfg.get('warning_pct', -10.0))
                    if action == 'none':
                        continue
                    if action == 'recover':
                        threshold = float(pnl_cfg.get('warning_pct', -10.0))
                    message = f'{inst_id} {direction}浮动盈亏 {pnl_pct:+.2f}%'
                    item = {'type': 'pnl', 'inst_id': inst_id, 'level': rec_level,
                            'value': pnl_pct, 'threshold': threshold, 'message': message,
                            'sent': action in ('send', 'recover'),
                            'direction': direction, 'avg_px': avg_px, 'last_px': last_px,
                            'mgn_mode': pos.get('mgnMode', '')}
                    hits.append(item)
                    if item['sent']:
                        notify_items.append(item)
                        task_log.info(f'[{run_id}] 【盈亏告警-{rec_level}】{message}')
                    else:
                        task_log.info(f'[{run_id}] 【盈亏告警-抑制】{message}（级别未变化）')

        _save_runtime_state(state)
        _persist_alert_log(hits)
        _send_notifications(run_id, notify_items)
    except Exception as e:
        error = str(e)
        task_log.error(f'[{run_id}] 监控告警检测异常: {e}')

    _LAST_RUN = {
        'finished_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'duration_ms': int((datetime.datetime.now() - started).total_seconds() * 1000),
        'hit_count': len(hits),
        'sent_count': len(notify_items),
        'email_ok': _LAST_RUN.get('email_ok'),  # _send_notifications 已就地更新
        'error': error,
    }


def _persist_alert_log(hits: list):
    """命中记录旁路落库：失败只记日志，不阻断监控主循环"""
    if not hits or AlertLog is None or session_scope is None:
        return
    try:
        with session_scope() as s:
            for it in hits:
                s.add(AlertLog(
                    alert_type=it['type'], inst_id=it['inst_id'], level=it['level'],
                    metric_value=float(it['value']), threshold=float(it['threshold']),
                    message=it['message'], notify_sent=bool(it['sent'])))
    except Exception as e:
        task_log.warning(f'[AlertMonitor] alert_log 落库失败（不影响告警发送）: {e}')


def _send_notifications(run_id: str, notify_items: list):
    """本轮需发送的命中：单条发专用邮件，多条合并为一封摘要（防轰炸）"""
    if not notify_items:
        _LAST_RUN['email_ok'] = None
        return
    try:
        notifier = MessageNotifier()
        if len(notify_items) == 1:
            it = notify_items[0]
            if it['type'] == 'price':
                ok = notifier.send_price_volatility_alert(
                    it['inst_id'], it['value'], it.get('window_minutes', 15), it['level'])
            else:
                ok = notifier.send_position_pnl_alert(
                    it['inst_id'], {'cross': '全仓', 'isolated': '逐仓'}.get(it.get('mgn_mode'), ''),
                    it.get('direction', ''), it['value'],
                    it.get('avg_px', 0), it.get('last_px', 0), it['level'])
        else:
            ok = notifier.send_alert_digest([
                {'type': it['type'], 'level': it['level'], 'inst_id': it['inst_id'],
                 'value': it['value'], 'threshold': it['threshold'], 'message': it['message']}
                for it in notify_items])
        _LAST_RUN['email_ok'] = bool(ok)
        task_log.info(f'[{run_id}] 告警邮件{"发送成功" if ok else "发送失败"}'
                      f'（{len(notify_items)} 项命中）')
    except Exception as e:
        _LAST_RUN['email_ok'] = False
        task_log.error(f'[{run_id}] 告警邮件发送异常: {e}')


# =============================================================================
# 调度注册（scheduler.register_default_jobs 与 alert_routes 配置保存共用）
# =============================================================================

def register_alert_job() -> int:
    """按当前配置注册/热重注册监控任务，返回检测周期（秒）。

    关闭监控时移除已注册任务。APScheduler replace_existing 保证热重注册幂等。
    """
    from ..scheduler import task_scheduler

    cfg = load_alert_config()
    interval = max(30, int(cfg.get('interval_seconds', 300)))
    if cfg.get('enabled'):
        task_scheduler.register_job(
            run_alert_check, trigger='interval', seconds=interval,
            job_id=ALERT_JOB_ID, job_name='监控告警（行情异动/持仓盈亏）')
    else:
        task_scheduler.remove_job(ALERT_JOB_ID)
    return interval
