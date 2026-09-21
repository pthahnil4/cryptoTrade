#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实盘交易调度器 — 双仓位架构（趋势跟踪 + 区间波动）
=====================================================

交易所隔离约定：
- 全仓模式(cross)专门持有多头仓位
- 逐仓模式(isolated)专门持有空头仓位
- 净持仓模式下同方向两个仓位共享同一净头寸，各自持仓张数与加权入场均价
  由 DualPositionOrderManager 本地记账（position_order_state.json）

仓位A — 趋势跟踪(trend)：按 Pro3 趋势信号限价挂单
- period_mode='single' 单周期双向：只看短周期方向，翻转即平旧开新，多空都做
- period_mode='dual'   双周期单向：只做长周期方向，沿用三时段开/平仓窗口确认
  · 开仓窗口：上上时段=反向 + 上一时段=同向 + 当前=同向（翻转后第一个确认周期）
  · 平仓窗口：上上时段=原向 + 上一时段=反向 + 当前=反向
- 挂单价取反转 bar 的 open/close（由 entry_price_type / exit_price_type 人为切换）
- 不追价：挂出后等成交，或等下一次反转信号撤单重挂
- 六类止盈 + 止损仅对本仓位生效（本地评估 + 交易所兜底委托双保险）
- 止盈/止损全平成功后进入冷却期（global_settings.tp_cooldown_periods 个短周期，
  默认3）：期间两个仓位一律禁止自动开新仓，避免人工方向覆盖（开仓窗口恒开）
  下平仓后立即在极端价位追价重开

仓位B — 区间波动(range)：固定双周期单向，无限循环刷 BOLL 区间
- 长多 → 下轨挂限价买入、上轨挂 reduce-only 限价平仓；长空反之
- 边界漂移自动 amend 追价；平仓成交后立即重新挂开仓单
- 自带对侧边界止盈，不参与止盈止损引擎

长周期方向反转处理：
- 趋势仓：仅发送方向反转提醒邮件，不强平；但处于睡眠时段
  （night_force_close，默认东八区 0-6 点）则强制平仓
- 区间仓：始终强制处理 —— 未成交挂单撤销换方向重挂，已持仓市价强平

作者：AI Assistant
更新时间：2026-07-28
"""

import time
import datetime
import sys
import os
import signal
import threading
import logging
from typing import List, Dict, Optional

# 双模式导入：兼容独立运行 (__main__) 和 Web 包导入 (crypto.task.trend_range_trader)
try:
    from .strategy_adapter import DualPeriodStrategyAdapter
    from .notification.message_notifier import MessageNotifier
    from .utils.trade_executor import TradeExecutor
    from .utils.position_order_manager import (
        DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)
    from .utils.logger import (get_task_logger, get_trade_logger, generate_run_id,
                               fmt_dir, fmt_pct, fmt_qty)
    from .utils.trade_journal import record_fill as journal_fill
    from .utils.instrument_spec import InstrumentSpecCache
    from .tp_engine import TakeProfitEngine
except ImportError:
    from strategy_adapter import DualPeriodStrategyAdapter
    from notification.message_notifier import MessageNotifier
    from utils.trade_executor import TradeExecutor
    from utils.position_order_manager import (
        DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)
    from utils.logger import (get_task_logger, get_trade_logger, generate_run_id,
                               fmt_dir, fmt_pct, fmt_qty)
    from utils.trade_journal import record_fill as journal_fill
    from utils.instrument_spec import InstrumentSpecCache
    from tp_engine import TakeProfitEngine

from crypto.api_config import get_api_config

# 调度器运行时状态已迁移 MySQL（迁移批次5，原 scheduler_state.json /
# reverse_guard_state.json / manual_pause_state.json）：DB 不可用时
# 仅告警不中断交易，与 JSON 版容错行为一致
try:
    from crypto.database import session_scope, db_health
    from crypto import trader_state_repo as state_repo
    from crypto import config_store_repo
except ImportError:
    session_scope = None
    db_health = None
    state_repo = None
    config_store_repo = None

# 双日志系统：任务执行流 + 交易操作流
task_log = get_task_logger()
trade_log = get_trade_logger()

# 静默第三方库日志
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)


class TrendRangeTrader:
    """实盘数字货币交易调度器 — 趋势跟踪仓 + 区间波动仓 双仓位并行"""

    def __init__(self, manual_direction_config: Dict = None, account: str = None):
        self.strategy_adapter = None
        self.running = False
        self.stop_event = threading.Event()
        # 冷却提示去重：冷却期内每轮都会检查，只在首轮打一行日志避免刷屏
        # （需在 _manual_pause 加载前初始化，_manual_pause_remaining 会用到）
        self._pause_logged = set()

        # 止盈/止损冷却 {inst_id: 恢复自动开仓时间戳}：止盈/止损全平成功后
        # N 个短周期内禁止自动开新仓，防止平仓后立即追价重开。
        # 内存态即可：冷却时长仅数根K线，重启丢失后
        # 仍有交易所兜底委托与人工强平冷却兜底；结构化流水在 trade_operations.log 每轮留痕
        self._tp_close_until = {}
        # 止盈冷却提示去重：进入冷却后 task_log 首轮提示一次，后续轮只写结构化流水（防刷屏）
        self._tp_cd_logged = set()

        # 插针临时禁开仓（spike_guard，行为开关默认关闭）：
        #   _spike_prev   {inst_id: {'price','ts'}} 上一处理轮的现价与时间戳（环比基准）
        #   _spike_until  {inst_id: 恢复自动开仓时间戳} 判定插针后暂停开仓的到期时间
        #   _spike_logged 进入暂停后 task_log 首轮提示去重（防刷屏）
        # 内存态即可：暂停仅数分钟，重启丢失由其它冷却/交易所兜底委托补位。
        self._spike_prev = {}
        self._spike_until = {}
        self._spike_logged = set()

        # 止盈评估引擎（六类主止盈单选 + 时间止盈兜底，运行时状态持久化）
        self.tp_engine = TakeProfitEngine()

        # 人工指定方向配置
        self.manual_direction_config = manual_direction_config or {}

        # 交易所使用的账号标识（None 则用 api_config 默认账号）
        self.account = account

        # 配置文件路径
        self.config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'config', 'config_trend_range.json'
        )

        # 加载全局配置（迁移批次7a 起 DB 优先，失败回退本地文件）
        _cfg = self._load_config()
        _gs = _cfg.get('global_settings', {})

        # 邮件通知器
        _cooldown = int(_gs.get('email_cooldown_minutes', 30))
        self.message_notifier = MessageNotifier(
            config_file=self.config_path,
            email_cooldown_minutes=_cooldown
        )
        self.email_connection_ok = None
        self._check_email_connection()

        # 连续分析失败计数（inst_id → 连续失败轮数）：达阈值升级为告警邮件，
        # 避免 OKX API/网络持续故障时系统“静默失效”而持仓裸露
        self._consec_fail = {}

        # 下单连续被交易所拒/失败计数（inst_id → 连续"有拒单"的轮数）：
        # analyze_and_trade_real 只要分析+持仓查询成功就返回 success=True，
        # 单笔挂单被拒不会降级 success，因而 _note_cycle_result 抓不到它——
        # 交易所持续拒单（51008/价格过滤/最小量）时心跳照常、实则一单没挂上。
        # 本计数补上这个静默失效盲区，达阈值发风控告警（见 _handle_place_failures）。
        self._place_fail_streak = {}
        # 止盈/止损平仓失败连续轮数（inst_id → 计数）：止损名义在、实则没平=
        # 持仓裸露，需专项升级告警而非只靠通用交易失败邮件（见 _alert_sl_tp_fail）。
        self._sltp_fail_streak = {}
        # P2-b 兜底委托不在位：key=f"{inst_id}|{direction}" → 连续"该挂却挂不上"的轮数
        # （趋势仓有持仓+有有效触发价，但交易所侧兜底委托始终不在位=宕机时最后防线缺失）。
        self._algo_fail_streak = {}
        # P2-c 持久化降级：DB 连接连续失败达阈值只发一封叫醒级告警，恢复后清 latch（可发缓解）。
        self._persist_alerted = False
        # P2-d 链路健康聚合：连续"全批币种 OKX 读失败"的轮数 + 是否已发聚合告警（恢复后复位）。
        self._link_bad_rounds = 0
        self._link_alerted = False

        # 获取实盘 API 配置（按指定账号）
        api_config = get_api_config(self.account)
        is_valid, msg = self._validate_real_config(api_config)
        if not is_valid:
            raise ValueError(f"实盘配置验证失败: {msg}")

        # 记录当前交易账号（供状态展示/日志使用）
        self.account = api_config.get('account')
        self.account_name = api_config.get('account_name', self.account)
        task_log.info(f"交易账号: {self.account_name} ({self.account})")

        # 初始化交易执行器
        self.trade_executor = TradeExecutor(
            api_config['api_key'],
            api_config['secret_key'],
            api_config['passphrase'],
            api_config['flag']
        )

        # 合约规格缓存：金额(size_mode='usd')→张数换算依赖 ctVal/lotSz/minSz，
        # 复用执行器的公共通道客户端；规格带磁盘缓存，断网不阻断交易链路
        self.spec_cache = InstrumentSpecCache(
            public_api=self.trade_executor.public_api,
            flag=str(api_config['flag'] or '0'))

        # 双仓位挂单与本地账本管理器（趋势跟踪 + 区间波动）；
        # 必须注入规格缓存 —— 下单量取整/最小下单量判定需逐合约的 lotSz/minSz
        self.pos_mgr = DualPositionOrderManager(
            self.trade_executor, spec_cache=self.spec_cache)
        # 金额模式换算失败日志去重（恢复可换算后自动复位）
        self._size_warned = set()
        # 开仓窗口上升沿检测：{inst_id: 上一轮是否处于开仓窗口}
        # 窗口开启只提醒一次邮件，窗口结束后复位，下个窗口重新触发
        self._window_open_prev = {}

        # 观察模式（币种级 trade_enabled=false：只分析+发邮件，对交易所零操作）
        # _watch_active：当前处于观察模式的币种（进入首轮撤挂单+邮件）
        self._watch_active = set()
        # 观察模式事件上升沿 {f"{inst_id}|{信号名}": 上一轮状态}，邮件按上升沿去重
        self._watch_edges = {}
        # 观察模式长周期反转邮件已通知到的新方向（防每轮刷屏）
        self._watch_rev_notified = {}
        # 观察模式单周期模式的上一轮目标方向（翻转检测）
        self._watch_single_prev = {}

        # 执行间隔（秒）—— 每轮重新读取配置
        self._default_interval = int(_gs.get('execution_interval_seconds', 60))

        # 人工干预缓存
        self.manual_override_cache = {}

        # 上一次方向记录 {inst_id: {'short': direction, 'long': direction}}
        # 持久化到 MySQL（trader_directions 表）：定时任务重启后自动恢复，
        # 避免断网/重启期间因内存丢失而漏判长周期方向反转（旧方向持仓/挂单未清理）
        self.last_directions = self._load_directions()

        # 反向持仓风控计时器 {inst_id: {detected_ts, long_direction, reverse_side,
        #   reverse_mode, reverse_amount, warned}}。持久化到 MySQL（reverse_guard 表）：
        # 预警→强平的倒计时跨重启不重置，避免断网/重启期间反向持仓逃过强平时限。
        self._reverse_guard = self._load_reverse_guard()

        # 人工强平冷却 {inst_id: 恢复自动开仓的时间戳}。持久化到 MySQL
        # （manual_pause 表）：冷却期内重启程序也不会立即把刚被人工平掉的仓位重新开回来
        self._manual_pause = self._load_manual_pause()

        # 调度轮与人工强平（Flask线程）互斥：保证“对账→止盈止损→挂单”序列
        # 不被强平横插，强平的“清账本→撤单→平仓”也不被调度轮打断
        self._trade_lock = threading.RLock()

        # 任务运行计数器
        self._run_count = 0
        self.last_execution = {'batch': 0}

        # 信号处理器（仅主线程生效）
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

    # =================================================================
    # 辅助方法
    # =================================================================

    def _validate_real_config(self, config):
        required_keys = ['api_key', 'secret_key', 'passphrase', 'flag']
        for key in required_keys:
            if not config.get(key):
                return False, f"缺少必需的配置项: {key}"
        if config['flag'] != "0":
            return False, "配置错误：必须为实盘环境(flag=0)"
        return True, "实盘配置验证通过"

    def _signal_handler(self, signum, frame):
        self.stop()

    def _check_email_connection(self) -> bool:
        try:
            self.email_connection_ok = self.message_notifier.email_tool.test_connection()
            if not self.email_connection_ok:
                task_log.warning("邮件连接测试失败")
            return self.email_connection_ok
        except Exception as e:
            self.email_connection_ok = False
            task_log.error(f"邮件连接测试异常: {e}")
            return False

    def _sync_email_cooldown(self, config: Dict):
        """热加载邮件冷却分钟数并同步给通知器（修改后无需重启即可生效）。

        MessageNotifier 在每次发送前才读取自身 email_cooldown_minutes 属性，
        因此直接更新属性即可；非法/缺失值保持原值不动，避免把冷却关掉。
        """
        try:
            cd = int((config.get('global_settings', {}) or {})
                     .get('email_cooldown_minutes', 30))
        except (TypeError, ValueError):
            return
        if cd > 0 and cd != self.message_notifier.email_cooldown_minutes:
            task_log.info(
                f"【热加载】邮件冷却时间 "
                f"{self.message_notifier.email_cooldown_minutes}→{cd} 分钟")
            self.message_notifier.email_cooldown_minutes = cd

    def _load_config(self) -> Dict:
        """每次执行时重新加载交易配置（热加载）。

        迁移批次7a 起配置主存 MySQL（kv_store key='strategy_config'），
        带 TTL 缓存（同进程 Web 侧保存写穿透失效即时生效；外部直改库
        最晚 TTL 内生效）。DB 不可用/无数据时回退本地
        config_trend_range.json，保证 DB 故障时交易定时任务不受影响。
        """
        if session_scope is not None and config_store_repo is not None:
            try:
                # 按账号隔离：优先读本账号专属配置 strategy_config:{account}，
                # 缺失回退全局 key，再回退本地文件。多账号共用一个库时，本地
                # 测试账号不会读到实盘主账号的仓位参数（否则保证金不足 51008）。
                cfg = config_store_repo.load_strategy_config_cached(
                    getattr(self, 'account', None))
                if cfg is not None:
                    return cfg
            except Exception as e:
                task_log.warning(f"从数据库加载交易配置失败，回退本地文件: {e}")
        try:
            import json
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            task_log.warning(f"加载配置文件失败: {e}")
            return {}

    def _load_directions(self) -> Dict:
        """加载持久化的方向记录（重启恢复，防止漏判长周期反转）。"""
        try:
            with session_scope() as session:
                return state_repo.load_directions(session)
        except Exception as e:
            task_log.warning(f"加载方向状态失败: {e}")
        return {}

    def _save_directions(self):
        """持久化方向记录（每轮调度完成后落库，代表“已完整处理”的方向）。"""
        try:
            with session_scope() as session:
                state_repo.save_directions(session, self.last_directions)
        except Exception as e:
            task_log.warning(f"保存方向状态失败: {e}")

    def _load_reverse_guard(self) -> Dict:
        """加载持久化的反向持仓风控计时器（重启恢复，保证强平倒计时不重置）。"""
        try:
            with session_scope() as session:
                return state_repo.load_reverse_guard(session)
        except Exception as e:
            task_log.warning(f"加载反向持仓风控状态失败: {e}")
        return {}

    def _save_reverse_guard(self):
        """持久化反向持仓风控计时器（检测/强平/解除时立即落库，保障跨重启持续）。"""
        try:
            with session_scope() as session:
                state_repo.save_reverse_guard(session, self._reverse_guard)
        except Exception as e:
            task_log.warning(f"保存反向持仓风控状态失败: {e}")

    def _load_manual_pause(self) -> Dict:
        """加载人工强平冷却状态（冷却期内禁止自动开仓，跨重启保持）。"""
        try:
            with session_scope() as session:
                return state_repo.load_manual_pause(session)
        except Exception as e:
            task_log.warning(f"加载人工强平冷却状态失败: {e}")
        return {}

    def _save_manual_pause(self):
        """持久化人工强平冷却状态（设置/到期清除时立即落库）。"""
        try:
            with session_scope() as session:
                state_repo.save_manual_pause(session, self._manual_pause)
        except Exception as e:
            task_log.warning(f"保存人工强平冷却状态失败: {e}")

    def _manual_pause_remaining(self, inst_id: str) -> float:
        """人工强平冷却剩余秒数（<=0 表示未在冷却；到期自动清除记录）。"""
        until = float(self._manual_pause.get(inst_id, 0) or 0)
        if until <= 0:
            return 0.0
        remaining = until - time.time()
        if remaining <= 0:
            self._manual_pause.pop(inst_id, None)
            self._save_manual_pause()
            if hasattr(self, '_pause_logged'):
                self._pause_logged.discard(inst_id)
            task_log.info(f"{inst_id} | 【人工强平冷却】冷却结束，恢复自动开仓")
            return 0.0
        return remaining

    @staticmethod
    def _period_seconds(period: str) -> int:
        """K线周期字符串 → 秒数（'15m'→900 / '4H'→14400 / '1D'→86400），
        解析失败回退 15m，保证冷却时长计算不崩。"""
        s = str(period or '').strip()
        units = {'m': 60, 'h': 3600, 'd': 86400}
        if len(s) >= 2 and s[-1].lower() in units:
            try:
                return max(60, int(float(s[:-1])) * units[s[-1].lower()])
            except ValueError:
                pass
        return 900

    def _start_tp_cooldown(self, inst_id: str, reason: str, short_period: str,
                           run_id: str = '') -> bool:
        """止盈/止损全平成功后启动开仓冷却：N 个短周期内两仓禁止自动开新仓。

        若无此冷却，止盈平仓后开仓窗口仍然成立（尤其人工方向覆盖下
        open_window 恒为 True）时，调度会在平仓同轮/下轮立即按现价附近
        重新挂出开仓限价单，形成追高/杀跌。返回是否实际启动冷却
        （配置 tp_cooldown_periods=0 或缺失时不启动）。
        """
        rid = f"[{run_id}] " if run_id else ''
        periods = int((self._load_config().get('global_settings', {}) or {})
                      .get('tp_cooldown_periods', 3) or 0)
        if periods <= 0:
            return False
        secs = periods * self._period_seconds(short_period)
        self._tp_close_until[inst_id] = time.time() + secs
        self._tp_cd_logged.add(inst_id)
        task_log.info(
            f"{rid}{inst_id} | 【止盈止损冷却】启动：全平成功({reason})，"
            f"{periods}个短周期({short_period}，约{secs / 60:.0f}分钟)内禁止自动开新仓")
        trade_log.info(
            f"{rid}止盈止损冷却 | 启动 | {inst_id} | 原因:{reason} | "
            f"tp_cooldown_periods={periods} | 时长{secs / 60:.0f}分钟 | "
            f"期间两仓禁止自动开新仓")
        return True

    def _tp_cooldown_remaining(self, inst_id: str) -> float:
        """止盈/止损冷却剩余秒数（<=0 表示未在冷却；到期自动清除记录）。"""
        until = float(self._tp_close_until.get(inst_id, 0) or 0)
        if until <= 0:
            return 0.0
        remaining = until - time.time()
        if remaining <= 0:
            self._tp_close_until.pop(inst_id, None)
            self._tp_cd_logged.discard(inst_id)
            task_log.info(f"{inst_id} | 【止盈止损冷却】冷却结束，恢复自动开仓")
            return 0.0
        return remaining

    # =================================================================
    # 插针临时禁开仓（spike_guard）— 行为开关默认关闭，须实盘校准阈值后再开
    # 单轮环比价格突变视为插针，暂停自动开新仓 N 分钟（平仓/撤单照常），
    # 避免在插针造成的极端价位追价开仓。整体 fail-safe：任何异常只记日志。
    # =================================================================

    _SPIKE_GUARD_DEFAULTS = {
        'spike_enabled': False,        # 行为开关：默认关闭，开启才会暂停开仓
        'spike_change_pct': 5.0,       # 单轮环比涨跌 ≥ 此% 判为插针
        'spike_min_gap_seconds': 30,   # 距上轮 < 此秒不判定（防重启首轮/异常快轮误伤）
        'spike_pause_minutes': 5,      # 判定插针后暂停自动开新仓分钟数
    }

    def _spike_guard_cfg(self) -> Dict:
        """读取插针保护配置（global_settings.spike_guard，热加载、按账号隔离）；
        缺省字段用 _SPIKE_GUARD_DEFAULTS 兜底，读不到即返回缺省（不抛）。"""
        try:
            sg = ((self._load_config().get('global_settings', {}) or {})
                  .get('spike_guard', {}) or {})
        except Exception:
            sg = {}
        out = dict(self._SPIKE_GUARD_DEFAULTS)
        for k, v in sg.items():
            if v is not None:
                out[k] = v
        return out

    @staticmethod
    def _classify_spike(prev, cur_price, cur_ts, sg_cfg):
        """单轮环比插针判定（纯函数，不碰 self）。prev={'price','ts'} 上一轮现价/时间戳。
        返回 (is_spike, change_pct)。数据缺失 / 无基准(首轮) / 间隔过小一律
        (False, pct或0.0)——宁漏不误伤，避免刚重启或异常快轮把正常波动当插针。"""
        try:
            prev_price = float((prev or {}).get('price') or 0)
            prev_ts = float((prev or {}).get('ts') or 0)
            cur_price = float(cur_price)
            cur_ts = float(cur_ts)
        except (TypeError, ValueError):
            return False, 0.0
        if cur_price <= 0 or prev_price <= 0 or prev_ts <= 0:
            return False, 0.0
        if (cur_ts - prev_ts) < float(sg_cfg.get('spike_min_gap_seconds', 30)):
            return False, 0.0
        pct = (cur_price / prev_price - 1.0) * 100.0
        return abs(pct) >= float(sg_cfg.get('spike_change_pct', 5.0)), pct

    def _note_spike_guard(self, inst_id: str, cur_price: float, run_id: str = ''):
        """每处理轮调用：与上一轮现价环比，命中插针则设暂停到期并发风控邮件（叫醒级）。
        只更新环比基准与暂停状态，实际开仓拦截由主循环 5d 闸门完成。整体 fail-safe。"""
        try:
            now = time.time()
            sg = self._spike_guard_cfg()
            prev = self._spike_prev.get(inst_id)
            is_spike, pct = self._classify_spike(prev, cur_price, now, sg)
            # 无论开关都刷新环比基准，保证开启瞬间基准新鲜（不拿陈旧价乱判）
            try:
                if cur_price and float(cur_price) > 0:
                    self._spike_prev[inst_id] = {'price': float(cur_price), 'ts': now}
            except (TypeError, ValueError):
                pass
            if not sg.get('spike_enabled', False) or not is_spike:
                return
            pause_min = float(sg.get('spike_pause_minutes', 5))
            if pause_min <= 0:
                return
            until = now + pause_min * 60
            was_paused = float(self._spike_until.get(inst_id, 0) or 0) > now
            self._spike_until[inst_id] = max(
                float(self._spike_until.get(inst_id, 0) or 0), until)
            rid = f"[{run_id}] " if run_id else ''
            direction = '急涨' if pct >= 0 else '急跌'
            task_log.warning(
                f"{rid}{inst_id} | 【插针保护】单轮环比{direction}{pct:+.2f}% "
                f"≥ 阈值{sg.get('spike_change_pct', 5.0)}%，暂停自动开新仓{pause_min:.0f}分钟")
            trade_log.info(
                f"{rid}插针保护 | 触发 | {inst_id} | 环比{pct:+.2f}% | "
                f"暂停开仓{pause_min:.0f}分钟 | 平仓/撤单照常")
            if not was_paused:   # 已在暂停中则不重复发信（延长即可）
                self._safe_risk_alert(
                    inst_id, f'插针检测·暂停自动开新仓 {pause_min:.0f} 分钟',
                    rows=[('环比涨跌', f'{pct:+.2f}%（{direction}）'),
                          ('上一轮现价', f"{(prev or {}).get('price', 0):.6g}"),
                          ('本轮现价', f'{float(cur_price):.6g}'),
                          ('判定阈值', f"{sg.get('spike_change_pct', 5.0)}%"),
                          ('暂停时长', f'{pause_min:.0f} 分钟（期间平仓/撤单照常）'),
                          ('说明', '插针保护已触发，系统暂停该币自动开新仓，到期自动恢复；'
                                '如判定为误伤可将 global_settings.spike_guard.spike_enabled '
                                '设为 false 或调高 spike_change_pct。')],
                    level='critical')
        except Exception as e:
            task_log.error(f"{inst_id} | 插针保护处理异常（不影响交易）: {e}")

    def _spike_pause_remaining(self, inst_id: str) -> float:
        """插针暂停剩余秒数（<=0 未暂停；到期自动清除记录与去重集）。"""
        until = float(self._spike_until.get(inst_id, 0) or 0)
        if until <= 0:
            return 0.0
        remaining = until - time.time()
        if remaining <= 0:
            self._spike_until.pop(inst_id, None)
            self._spike_logged.discard(inst_id)
            task_log.info(f"{inst_id} | 【插针保护】暂停结束，恢复自动开仓")
            return 0.0
        return remaining

    def _detect_external_close(self, inst_id: str, pre_books: Dict, run_id: str = ''):
        """检测交易所侧手动平仓（系统外持仓减少）并触发人工平仓冷却。

        与对账前账本快照比对：任一篮子方向账本缩减≥最小步长(0.1张)即判定为
        系统外减仓（交易所侧手动平仓/强平/爆仓）。程序自身的平仓不会误触发：
        市价平仓已由 note_external_close 即时入账，限价成交已由 poll_fills 在
        reconcile 之前入账，兜底委托已由 poll_algo_triggers 入账。
        检测后复用 manual_close_pause_minutes 冷却（与网页“人工强平”同一机制，
        MySQL 持久化）：否则人工方向覆盖（开仓窗口恒开）下，下一轮(80s内)就会
        在刚平仓的高位把仓位重新挂单开回来。冷却只拦开仓，平仓/撤单照常。
        """
        rid = f"[{run_id}] " if run_id else ''
        shrinks = []
        for bucket in (BUCKET_TREND, BUCKET_RANGE):
            post = self.pos_mgr.get_book(inst_id, bucket)['held']
            for d in ('long', 'short'):
                pre = float((pre_books.get(bucket) or {}).get(d, 0) or 0)
                lost = round(pre - float(post.get(d, 0) or 0), 4)
                if lost >= 0.0999:
                    shrinks.append((bucket, d, lost))
        if not shrinks:
            return
        detail = '、'.join(
            f"{'趋势仓' if b == BUCKET_TREND else '区间仓'}"
            f"{'多头' if d == 'long' else '空头'}{fmt_qty(a)}张"
            for b, d, a in shrinks)
        pause_min = float((self._load_config().get('global_settings', {})
                           .get('manual_close_pause_minutes', 30)) or 0)
        if pause_min > 0:
            pause_text = f"→ 进入冷却期{pause_min:.0f}分钟禁止自动开新仓"
            cd_text = f"暂停自动开仓{int(pause_min)}分钟"
        else:
            pause_text = "（manual_close_pause_minutes=0 未冷却，将立即重新挂单！）"
            cd_text = "未冷却(配置0)"
        task_log.warning(
            f"{rid}{inst_id} | 【手动平仓检测】检测到系统外持仓减少({detail})，"
            f"判定为交易所侧手动平仓/强平 {pause_text}")
        trade_log.info(
            f"{rid}手动平仓 | 检测到 | {inst_id} | {detail} | {cd_text}")
        if pause_min > 0:
            self._manual_pause[inst_id] = time.time() + pause_min * 60
            self._save_manual_pause()
            self._pause_logged.add(inst_id)  # 本轮已提示，步骤5b不重复打日志

    def _detect_reverse_position(self, inst_id: str, long_direction: str,
                                 cross_pos: float, isolated_pos: float) -> Optional[Dict]:
        """检测反向冲突持仓：与长周期方向相反的“另一模式”净持仓。

        程序约定：多头仅用全仓(cross)、空头仅用逐仓(isolated)。反向净持仓来自两处，
        两者都与策略方向冲突、都必须清理，故分别计量后一并返回：
        ① 账本内(ledger) —— 程序自身的旧方向残留仓。双周期单向模式下两个篮子都只
           应持有长周期方向，账本里的反向持仓即与设计方向冲突（2026-09-02 LIT 实盘
           事故：长周期反转发生在白天，步骤6 只在睡眠时段强平趋势仓，旧方向多头既
           逃过反转清理、又因旧实现“扣除本地账本”被判为无反向持仓，永久裸露且无
           止盈止损保护）；
        ② 账本外(unbooked) —— 人工反向开仓（真实净持仓超出账本记录的部分）。
        返回 {'side','mode','amount','ledger':[(bucket, held)], 'unbooked'} 或 None。
        """
        rev_dir = 'short' if long_direction == 'long' else 'long'
        if rev_dir == 'short':
            real = abs(isolated_pos) if isolated_pos < -0.01 else 0.0
            mode = 'isolated'
        else:
            real = cross_pos if cross_pos > 0.01 else 0.0
            mode = 'cross'
        if real <= 0.01:
            return None
        ledger = []
        owned = 0.0
        for bucket in (BUCKET_TREND, BUCKET_RANGE):
            held, _ = self.pos_mgr.get_position(inst_id, bucket, rev_dir)
            owned += held
            # 粉尘级账本残渣不单列为冲突仓（_close_bucket 内部会自行清零账本）
            if held >= self.POS_DUST:
                ledger.append((bucket, round(float(held), 4)))
        # 账本外超出量阈值取该合约 minSz：更小的量低于最小可交易量，_close_amount 会
        # 拒单，导致“检测→强平失败→重试”每轮死循环永不收敛。硬编码 0.05 会在 POL
        # (minSz=1) 上把不可平的零头当反向单反复强平失败
        _lot, min_sz = self._steps(inst_id)
        unbooked = round(real - owned, 4)
        if unbooked + 1e-9 < min_sz:
            unbooked = 0.0
        if not ledger and unbooked <= 0:
            return None
        amount = round(sum(h for _, h in ledger) + unbooked, 4)
        return {'side': rev_dir, 'mode': mode, 'amount': amount,
                'ledger': ledger, 'unbooked': unbooked}

    def _close_reverse_conflict(self, inst_id: str, reverse: Dict, reason: str,
                                run_id: str, short_period: str, long_period: str,
                                price: float, leverage: float = 0.0) -> bool:
        """强平反向冲突持仓：账本内按篮子平（同步扣账本），账本外定量平。

        - 账本内旧方向仓走 _close_bucket_smart：scene='reverse_guard' 不在智能减仓
          白名单内，退化为全量市价平仓并扣减账本；低于该合约 minSz 的粉尘持仓由
          _close_bucket 直接清零账本（下单必被拒，清零才能收敛）。
        - 账本外人工反向单走 _close_amount 定量 reduce-only。
        两部分合计恰为该模式的真实净持仓，不会误伤与长周期同向的仓位，也不会超量
        下单。任一部分失败即返回 False，由调用方保留计时器下轮重试（自愈）：
        失败时账本内部分未平掉，真实持仓不变，下轮重新检测出的账本外量仍准确。
        """
        ok_all = True
        for bucket, _held in (reverse.get('ledger') or []):
            bucket_cn = '趋势仓' if bucket == BUCKET_TREND else '区间仓'
            ok = self._close_bucket_smart(
                inst_id, bucket, reverse['side'], reason, run_id,
                short_period, long_period, price,
                leverage=leverage, scene='reverse_guard')
            if not ok:
                task_log.error(
                    f"[{run_id}] {inst_id} | 反向持仓风控 | {bucket_cn}旧方向"
                    f"{fmt_qty(_held)}张强平失败，保留计时器下轮重试")
            ok_all = ok_all and ok
        unbooked = float(reverse.get('unbooked') or 0)
        if unbooked > 0:
            ok_all = self._close_amount(
                inst_id, reverse['side'], unbooked, reason=reason, run_id=run_id,
                short_period=short_period, long_period=long_period,
                price=price) and ok_all
        return ok_all

    def _check_reverse_position_guard(self, inst_id: str, long_direction: str,
                                      cross_pos: float, isolated_pos: float,
                                      short_period: str, long_period: str,
                                      current_price: float, long_dir_changed,
                                      run_id: str = '',
                                      observe_only: bool = False,
                                      leverage: float = 0.0) -> Dict:
        """反向持仓风控：检测→预警（提前 grace 分钟）→超时强平。

        检测口径覆盖两类与长周期方向冲突的持仓（见 _detect_reverse_position）：
        账本内程序自身的旧方向残留仓 + 账本外的人工反向单。
        计时器持久化（MySQL reverse_guard 表），重启不重置。方向反转轮跳过检测：
        旧方向持仓本轮由反转清理逻辑处理，跳过避免同一轮对同一仓位重复下单。
        observe_only=True（观察模式）时检测与预警照常，但超时不执行强平。
        返回 {'closed': bool, 'mode': str, 'text': 风控状态文本(供本轮摘要展示)}。
        """
        rid = f"[{run_id}] " if run_id else ''
        result = {'closed': False, 'text': '无反向持仓'}
        try:
            cfg = self._load_config()
            guard_cfg = cfg.get('global_settings', {}).get('reverse_position_guard', {})
            if not guard_cfg.get('enabled', False):
                if inst_id in self._reverse_guard:
                    self._reverse_guard.pop(inst_id, None)
                    self._save_reverse_guard()
                result['text'] = '风控未启用'
                return result

            # 方向刚反转：旧方向持仓本轮会被平仓逻辑清理，跳过检测避免误判
            if long_dir_changed:
                if inst_id in self._reverse_guard:
                    self._reverse_guard.pop(inst_id, None)
                    self._save_reverse_guard()
                result['text'] = '本轮方向反转，跳过检测'
                return result

            grace_minutes = float(guard_cfg.get('grace_minutes', 10) or 10)
            grace_seconds = grace_minutes * 60

            reverse = self._detect_reverse_position(inst_id, long_direction,
                                                    cross_pos, isolated_pos)
            rec = self._reverse_guard.get(inst_id)

            if not reverse:
                # 反向持仓已消除（用户自行处理 / 已强平）→ 解除预警
                if rec:
                    self._reverse_guard.pop(inst_id, None)
                    self._save_reverse_guard()
                    task_log.info(
                        f"{rid}{inst_id} | 【反向风控】反向持仓已消除，解除预警")
                    result['text'] = '反向持仓已消除，解除预警'
                return result

            now = time.time()
            rev_cn = '空头' if reverse['side'] == 'short' else '多头'
            dir_cn = '看多' if long_direction == 'long' else '看空'
            # 冲突来源文案：区分“程序自身旧方向残留仓”与“账本外人工反向单”，
            # 供日志/心跳/预警邮件展示，避免把程序仓误报成人工操作
            ledger_part = reverse.get('ledger') or []
            ledger_amt = round(sum(h for _, h in ledger_part), 4)
            unbooked_amt = float(reverse.get('unbooked') or 0)
            if ledger_amt > 0 and unbooked_amt > 0:
                src_cn = (f"程序旧方向{fmt_qty(ledger_amt)}张"
                          f"+账本外人工{fmt_qty(unbooked_amt)}张")
            elif ledger_amt > 0:
                b_cn = '、'.join('趋势仓' if b == BUCKET_TREND else '区间仓'
                                 for b, _ in ledger_part)
                src_cn = f"程序旧方向持仓({b_cn})"
            else:
                src_cn = '账本外人工反向单'

            if not rec:
                # 首次检测到反向持仓 → 立即发送预警邮件（提前 grace 分钟预警）
                self._reverse_guard[inst_id] = {
                    'detected_ts': now,
                    'long_direction': long_direction,
                    'reverse_side': reverse['side'],
                    'reverse_mode': reverse['mode'],
                    'reverse_amount': reverse['amount'],
                    'warned': True,
                }
                self._save_reverse_guard()
                task_log.warning(
                    f"{rid}{inst_id} | 【反向风控】检测到反向{rev_cn}持仓"
                    f"{fmt_qty(reverse['amount'])}张 | 来源：{src_cn} | 长周期{dir_cn} | "
                    f"已预警，{grace_minutes:g}分钟后未处理将强制平仓")
                result['text'] = (f"检测到反向{rev_cn}{fmt_qty(reverse['amount'])}张"
                                  f"（{src_cn}），已预警，"
                                  f"{grace_minutes:g}分钟后未处理将强平")
                try:
                    self.message_notifier.send_reverse_position_warning(
                        symbol=inst_id, long_direction=long_direction,
                        reverse_side=reverse['side'], reverse_amount=reverse['amount'],
                        current_price=current_price, grace_minutes=grace_minutes,
                        cross_pos=cross_pos, isolated_pos=isolated_pos,
                        source_note=src_cn)
                except Exception as e:
                    task_log.warning(f"{rid}{inst_id} | 反向持仓预警邮件发送失败: {e}")
                return result

            # 已有记录：判断是否到达强平时限
            elapsed = now - float(rec.get('detected_ts', now))
            if elapsed >= grace_seconds:
                if observe_only:
                    # 观察模式：用户在线可自行处理，只提醒不强平（提示只打一次）
                    if rec.get('_log_min') != -1:
                        rec['_log_min'] = -1
                        task_log.warning(
                            f"{rid}{inst_id} | 【反向风控·观察】反向{rev_cn}"
                            f"{fmt_qty(reverse['amount'])}张已超{grace_minutes:g}分钟未处理，"
                            f"观察模式不执行强平，请人工处理")
                    result['text'] = (f"反向{rev_cn}{fmt_qty(reverse['amount'])}张待人工处理"
                                      f"（观察模式不强平）")
                    return result
                task_log.warning(
                    f"{rid}{inst_id} | 【反向风控·强平】反向{rev_cn} "
                    f"{fmt_qty(reverse['amount'])}张 | 来源：{src_cn} | "
                    f"原因：超过{grace_minutes:g}分钟未处理")
                # 账本内旧方向仓按篮子市价平（同步扣账本），账本外人工反向单定量平，
                # 合计=该模式真实净持仓，不误伤与长周期同向的仓位
                result['mode'] = reverse['mode']
                ok = self._close_reverse_conflict(
                    inst_id, reverse, reason='反向持仓冲突超时强平', run_id=run_id,
                    short_period=short_period, long_period=long_period,
                    price=current_price, leverage=leverage)
                if ok:
                    # 强平成功：解除计时器并发送“已强平”通知
                    result['closed'] = True
                    result['text'] = f"反向{rev_cn}{fmt_qty(reverse['amount'])}张已超时强平"
                    self._reverse_guard.pop(inst_id, None)
                    self._save_reverse_guard()
                    try:
                        self.message_notifier.send_reverse_position_closed(
                            symbol=inst_id, long_direction=long_direction,
                            reverse_side=reverse['side'], reverse_amount=reverse['amount'],
                            current_price=current_price, source_note=src_cn)
                    except Exception as e:
                        task_log.warning(f"{rid}{inst_id} | 反向持仓强平邮件发送失败: {e}")
                else:
                    # 强平失败：保留计时器（不重置 detected_ts，下轮 elapsed 仍超时→立即重试），
                    # 不解除记录、不发送“已强平”误报，避免 API 持续失败时反向持仓永远关不掉
                    task_log.error(
                        f"{rid}{inst_id} | 反向持仓风控 | 强制平仓失败（平仓委托未成功），"
                        f"保留计时器下轮重试")
                    result['text'] = f"反向{rev_cn}超时强平失败，下轮重试"
            else:
                remaining = max(0.0, grace_seconds - elapsed)
                mins = int(remaining // 60)
                # 倒计时同一分钟内不重复打印，避免每轮刷屏
                if rec.get('_log_min') != mins:
                    rec['_log_min'] = mins
                    task_log.info(
                        f"{rid}{inst_id} | 【反向风控】反向{rev_cn}"
                        f"{fmt_qty(reverse['amount'])}张待用户处理 | "
                        f"剩余{remaining / 60:.1f}分钟后强平")
                result['text'] = (f"反向{rev_cn}{fmt_qty(reverse['amount'])}张待处理，"
                                  f"剩余{remaining / 60:.1f}分钟后强平")
            return result
        except Exception as e:
            task_log.error(f"{rid}{inst_id} | 反向持仓风控异常: {e}")
            result['text'] = '风控检查异常'
            return result

    def _get_strategy_adapter(self) -> DualPeriodStrategyAdapter:
        if not self.strategy_adapter:
            self.strategy_adapter = DualPeriodStrategyAdapter()
        return self.strategy_adapter

    def _fmt_book(self, inst_id: str, bucket: str, price: float) -> str:
        """格式化某仓位的本地账本持仓（含按现价折算的浮动盈亏%），供日志使用"""
        parts = []
        for d in ('long', 'short'):
            held, avg = self.pos_mgr.get_position(inst_id, bucket, d)
            if held > 0.01 and avg > 0:
                pnl = ((price - avg) if d == 'long' else (avg - price)) / avg * 100 \
                    if price > 0 else 0.0
                parts.append(
                    f"{fmt_dir(d)}{fmt_qty(held)}张@均价{avg:.6g}(盈亏{fmt_pct(pnl)})")
        return ' '.join(parts) if parts else '空仓'

    def _print_positions_report(self):
        """打印持仓明细"""
        try:
            positions = self.trade_executor.get_positions()
        except Exception:
            positions = []
        total_count = len(positions)
        total_upl = 0.0
        lines = ["【持仓明细】"]
        for idx, pos in enumerate(positions, 1):
            inst_id = pos.get("instId") or pos.get("inst_id") or "-"
            mgn_mode = pos.get("mgnMode") or "isolated"
            mode_cn = "全仓" if mgn_mode == "cross" else "逐仓"
            pos_side = pos.get("posSide") or "net"
            size = float(pos.get("pos", 0) or 0)
            dir_cn = "多" if size > 0 else ("空" if size < 0 else "-")
            avg_px = float(pos.get("avgPx", 0) or 0)
            upl = float(pos.get("upl", 0) or 0)
            upl_ratio = float(pos.get("uplRatio", 0) or 0) * 100
            total_upl += upl
            sign = "+" if upl >= 0 else "-"
            lines.append(
                f"{idx}. {inst_id} {mode_cn} {dir_cn} {abs(size):.1f}张 "
                f"均价{avg_px:.4f} 盈亏{sign}{abs(upl):.2f}U ({upl_ratio:+.2f}%)"
            )
        total_sign = "+" if total_upl >= 0 else "-"
        lines.append(f"总计：{total_count}笔，净盈亏 {total_sign}{abs(total_upl):.2f} USDT")
        task_log.info("\n".join(lines))

    # =================================================================
    # 平仓操作
    # =================================================================

    def _close_cross_positions(self, inst_id: str, run_id: str = '',
                                short_period: str = '', long_period: str = '') -> bool:
        """平掉全仓模式的所有持仓（多头）。

        返回是否已确保全仓无残留持仓：无持仓或全部平仓成功返回 True；
        任一平仓委托失败或查询异常返回 False（供反向风控等调用方判断是否重试，
        避免把失败误当成功而错误解除计时器/发送“已强平”误报）。
        """
        rid = f"[{run_id}] " if run_id else ''
        try:
            # 用严格查询：失败返回 None（区别于真实空仓），避免把“查询失败”
            # 误当“无持仓成功”而谎报平仓成功（反向风控会据此错误清除计时器/
            # 发送“已强平”误报，方向反转会在旧仓未平时继续开新方向）
            positions_by_mode = self.trade_executor.try_get_positions_by_mode(inst_id)
            if positions_by_mode is None:
                trade_log.error(
                    f"{rid}平多 | 查询失败 | {inst_id} | 全仓持仓查询失败(网络/API)，"
                    f"无法确认，视为未平仓")
                return False
            cross_positions = positions_by_mode.get('cross', [])
            if not cross_positions:
                trade_log.info(f"{rid}平多 | 跳过 | {inst_id} | 全仓无持仓")
                return True

            all_ok = True
            for pos in cross_positions:
                amount = float(pos.get('pos', 0))
                if abs(amount) > 0:
                    close_side = 'sell' if amount > 0 else 'buy'
                    pos_avg = float(pos.get('avgPx', 0) or 0)
                    pos_upl = float(pos.get('upl', 0) or 0)
                    pos_upl_ratio = float(pos.get('uplRatio', 0) or 0) * 100
                    trade_log.info(
                        f"{rid}平多 | 发起 | {inst_id} | 全仓 | {fmt_qty(abs(amount))}张 | 市价 | "
                        f"持仓均价{pos_avg:.6g} 浮动盈亏{pos_upl:+.2f}U({fmt_pct(pos_upl_ratio)})")
                    res = self.trade_executor.execute_reduce_only_order(
                        inst_id=inst_id, side=close_side, amount=abs(amount), trading_mode='cross'
                    )
                    exec_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if res.get('success'):
                        ord_id = res.get('data', {}).get('ordId', res.get('order_id', 'N/A'))
                        close_price = res.get('price') or self.trade_executor.get_last_price(inst_id)
                        trade_log.info(
                            f"{rid}平多 | 成交成功 | {inst_id} | {fmt_qty(abs(amount))}张 @ "
                            f"约{float(close_price or 0):.6g} | ordId: {ord_id} | 全仓多头已清")
                        journal_fill(inst_id, 'account', 'long', 'close',
                                     close_price, abs(amount), reason='人工强平',
                                     ord_id=ord_id, run_id=run_id)
                        self._send_trade_email(
                            inst_id, short_period, long_period, '平多',
                            exec_time, close_price, True
                        )
                    else:
                        all_ok = False
                        err = res.get('error', '未知错误')
                        trade_log.error(
                            f"{rid}平多 | 失败 | {inst_id} | {fmt_qty(abs(amount))}张未成交 | {err}")
                        self._send_trade_email(
                            inst_id, short_period, long_period, '平多',
                            exec_time, 0, False, err
                        )
            return all_ok
        except Exception as e:
            trade_log.error(f"{rid}平多 | 异常 | {inst_id} | {e}")
            self._send_trade_email(
                inst_id, short_period, long_period, '平多',
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0, False, str(e)
            )
            return False

    def _close_isolated_positions(self, inst_id: str, run_id: str = '',
                                   short_period: str = '', long_period: str = '') -> bool:
        """平掉逐仓模式的所有持仓（空头）。

        返回是否已确保逐仓无残留持仓：无持仓或全部平仓成功返回 True；
        任一平仓委托失败或查询异常返回 False（供反向风控等调用方判断是否重试）。
        """
        rid = f"[{run_id}] " if run_id else ''
        try:
            # 用严格查询：失败返回 None（区别于真实空仓），避免把“查询失败”
            # 误当“无持仓成功”而谎报平仓成功（反向风控会据此错误清除计时器/
            # 发送“已强平”误报，方向反转会在旧仓未平时继续开新方向）
            positions_by_mode = self.trade_executor.try_get_positions_by_mode(inst_id)
            if positions_by_mode is None:
                trade_log.error(
                    f"{rid}平空 | 查询失败 | {inst_id} | 逐仓持仓查询失败(网络/API)，"
                    f"无法确认，视为未平仓")
                return False
            isolated_positions = positions_by_mode.get('isolated', [])
            if not isolated_positions:
                trade_log.info(f"{rid}平空 | 跳过 | {inst_id} | 逐仓无持仓")
                return True

            all_ok = True
            for pos in isolated_positions:
                amount = float(pos.get('pos', 0))
                if abs(amount) > 0:
                    close_side = 'sell' if amount > 0 else 'buy'
                    pos_avg = float(pos.get('avgPx', 0) or 0)
                    pos_upl = float(pos.get('upl', 0) or 0)
                    pos_upl_ratio = float(pos.get('uplRatio', 0) or 0) * 100
                    trade_log.info(
                        f"{rid}平空 | 发起 | {inst_id} | 逐仓 | {fmt_qty(abs(amount))}张 | 市价 | "
                        f"持仓均价{pos_avg:.6g} 浮动盈亏{pos_upl:+.2f}U({fmt_pct(pos_upl_ratio)})")
                    res = self.trade_executor.execute_reduce_only_order(
                        inst_id=inst_id, side=close_side, amount=abs(amount), trading_mode='isolated'
                    )
                    exec_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if res.get('success'):
                        ord_id = res.get('data', {}).get('ordId', res.get('order_id', 'N/A'))
                        close_price = res.get('price') or self.trade_executor.get_last_price(inst_id)
                        trade_log.info(
                            f"{rid}平空 | 成交成功 | {inst_id} | {fmt_qty(abs(amount))}张 @ "
                            f"约{float(close_price or 0):.6g} | ordId: {ord_id} | 逐仓空头已清")
                        journal_fill(inst_id, 'account', 'short', 'close',
                                     close_price, abs(amount), reason='人工强平',
                                     ord_id=ord_id, run_id=run_id)
                        self._send_trade_email(
                            inst_id, short_period, long_period, '平空',
                            exec_time, close_price, True
                        )
                    else:
                        all_ok = False
                        err = res.get('error', '未知错误')
                        trade_log.error(
                            f"{rid}平空 | 失败 | {inst_id} | {fmt_qty(abs(amount))}张未成交 | {err}")
                        self._send_trade_email(
                            inst_id, short_period, long_period, '平空',
                            exec_time, 0, False, err
                        )
            return all_ok
        except Exception as e:
            trade_log.error(f"{rid}平空 | 异常 | {inst_id} | {e}")
            self._send_trade_email(
                inst_id, short_period, long_period, '平空',
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0, False, str(e)
            )
            return False

    # =================================================================
    # 下单量步长（一律按合约规格，绝不硬编码）
    # =================================================================

    # “视为无持仓”的账本粉尘阈值（张）：低于此为对账浮点残渣
    POS_DUST = 0.01

    def _steps(self, inst_id: str) -> tuple:
        """该合约的 (lotSz 下单步长, minSz 最小下单量)，规格缺失回退 (0.1, 0.1)。

        区分两种"没规格"：spec_cache 压根没注入（离线冒烟用 __new__ 造的裸对象，
        生产 __init__ 必然注入）属正常兜底路径，静默返回；已注入却读失败才是真故障，
        必须告警。改前两者共用 except，导致每跑一次冒烟就往实盘日志里灌一批
        "读取合约下单步长失败: 'TrendRangeTrader' object has no attribute ..." 假故障行。
        """
        spec_cache = getattr(self, 'spec_cache', None)
        if spec_cache is None:
            return 0.1, 0.1
        try:
            return spec_cache.steps(inst_id)
        except Exception as e:
            task_log.warning(f"{inst_id} | 读取合约下单步长失败，按兜底0.1张: {e}")
            return 0.1, 0.1

    def _q(self, inst_id: str, amount: float) -> float:
        """下单量向下取整到该合约 lotSz；低于 minSz 返回 0（下单必被交易所拒）。

        各合约步长差异极大（XRP=0.01 / NEAR=0.1 / POL=1），历史实现把 0.1 张
        当成通用步长，使 XRP 折算出的 0.03 张被 round(…,1) 抹成 0（区间仓永不
        挂单）、POL 配的 0.5 张必被拒单（2026-09-01 排查结论）。
        """
        try:
            return self.spec_cache.quantize(inst_id, amount)
        except Exception:
            lot, _ = self._steps(inst_id)
            amount = float(amount or 0)
            out = round(int(amount / lot + 1e-9) * lot, 4)
            return out if out >= lot - 1e-9 else 0.0

    def _close_amount(self, inst_id: str, direction: str, amount: float,
                      reason: str = '', run_id: str = '', short_period: str = '',
                      long_period: str = '', price: float = 0.0,
                      bucket: str = 'account') -> bool:
        """定量市价 reduce-only 平仓（多头走全仓、空头走逐仓）。

        净持仓模式下两个仓位共享同一模式的净头寸，“平掉该模式全部持仓”
        会误伤另一个仓位，故一律按张数定量平仓。
        """
        amount = self._q(inst_id, amount)
        if amount <= 0:
            return False
        rid = f"[{run_id}] " if run_id else ''
        side = 'sell' if direction == 'long' else 'buy'
        mode = 'cross' if direction == 'long' else 'isolated'
        act_cn = '平多' if direction == 'long' else '平空'
        bucket_cn = {BUCKET_TREND: '趋势仓', BUCKET_RANGE: '区间仓'}.get(bucket, '账户')
        # 平仓前账本快照：用于成交后输出预估盈亏与剩余持仓（交易日志上下文）
        held_before, avg_before = (
            self.pos_mgr.get_position(inst_id, bucket, direction)
            if bucket in (BUCKET_TREND, BUCKET_RANGE) else (0.0, 0.0))
        trade_log.info(
            f"{rid}{act_cn} | 发起 | {inst_id} | {bucket_cn} | {mode} | {fmt_qty(amount)}张 | 市价 | "
            f"原因:{reason or '-'}"
            + (f" | 账本均价{avg_before:.6g} 持仓{fmt_qty(held_before)}张" if avg_before > 0 else ''))
        res = None
        try:
            res = self.trade_executor.execute_reduce_only_order(
                inst_id=inst_id, side=side, amount=amount, trading_mode=mode)
        except Exception as e:
            trade_log.error(f"{rid}{act_cn} | 异常 | {inst_id} | {e}")
        ok = bool(res and res.get('success'))
        err = '' if ok else ((res or {}).get('error') or '无返回')
        if ok:
            # 结构化流水：市价平仓价格优先取下单返回价，其次最新市场价（近似成交价）
            px = float(price or 0) or float((res or {}).get('price') or 0)
            if not px:
                try:
                    px = float(self.trade_executor.get_last_price(inst_id) or 0)
                except Exception:
                    px = 0.0
            # 成交上下文：预估盈亏%（基于账本均价）+ 该仓位平仓后剩余持仓
            extra = ''
            if avg_before > 0 and px > 0:
                pnl_pct = ((px - avg_before) if direction == 'long'
                           else (avg_before - px)) / avg_before * 100
                remaining = max(0.0, round(held_before - amount, 4))
                extra = (f" | 预估盈亏{fmt_pct(pnl_pct)} | "
                         f"{bucket_cn}剩余{fmt_qty(remaining)}张")
            trade_log.info(
                f"{rid}{act_cn} | 成交成功 | {inst_id} | {bucket_cn} | "
                f"{fmt_qty(amount)}张 @ 约{px:.6g}{extra}")
            journal_fill(inst_id, bucket, direction, 'close', px, amount,
                         reason=reason or '市价平仓',
                         ord_id=(res or {}).get('order_id'), run_id=run_id)
        else:
            trade_log.error(
                f"{rid}{act_cn} | 失败 | {inst_id} | {bucket_cn} | "
                f"{fmt_qty(amount)}张未成交 | 原因:{reason or '-'} | {err}")
        self._send_trade_email(
            inst_id, short_period, long_period,
            f'{act_cn}({reason})' if reason else act_cn,
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            price, ok, err)
        return ok

    def _close_bucket(self, inst_id: str, bucket: str, direction: str,
                      reason: str = '', run_id: str = '', short_period: str = '',
                      long_period: str = '', price: float = 0.0,
                      amount: float = None, cancel_orders: bool = True) -> bool:
        """平掉某仓位某方向的本地账本持仓，成功后同步扣减账本。

        amount=None 表示全平该仓位该方向；cancel_orders=True 时先撤本仓位挂单，
        避免残留 reduce-only 挂单与本次平仓互相挤占可平数量。
        """
        held, _ = self.pos_mgr.get_position(inst_id, bucket, direction)
        _lot, min_sz = self._steps(inst_id)
        if amount is None and self.POS_DUST <= held < min_sz:
            # 粉尘持仓全平：低于该合约 minSz 的平仓单必被交易所拒单，直接清零账本
            # 视为已清理 —— 否则长周期反转清理会在其上永久失败重试，持续阻断两个
            # 仓位新方向开仓（人工强制换向时表现为区间仓停摆）。
            # 阈值必须取合约 minSz 而非硬编码 0.1：XRP 的 minSz=0.01，真实可平的
            # 0.03 张若按 0.1 判定会被误清账本，交易所仍持仓而系统认为空仓（幽灵仓）
            self.pos_mgr.note_external_close(
                inst_id, bucket, direction, held,
                '粉尘清零(低于最小步长无法下单平仓)')
            return True
        amt = self._q(inst_id, min(held, float(amount)) if amount is not None else held)
        if amt <= 0:
            return False
        if cancel_orders:
            self.pos_mgr.cancel_bucket_orders(inst_id, bucket, reason or '仓位平仓')
        ok = self._close_amount(inst_id, direction, amt, reason, run_id,
                                short_period, long_period, price, bucket=bucket)
        if ok:
            self.pos_mgr.note_external_close(inst_id, bucket, direction, amt, reason)
        return ok

    # =================================================================
    # 智能减仓（基于整仓净已实现盈亏 realizedPnl）
    # =================================================================

    def _get_position_realized_pnl(self, inst_id: str, direction: str) -> Optional[float]:
        """查询该合约整仓的净已实现盈亏（realizedPnl，含手续费/资金费）。

        按交易所隔离约定：多头取全仓(cross)持仓、空头取逐仓(isolated)持仓。
        净持仓模式下两个篮子共享同一仓位，此值为整仓混合口径（不区分篮子）。
        查询失败返回 None（上层需降级处理，避免拿错误数字做决策）。
        """
        by_mode = self.trade_executor.try_get_positions_by_mode(inst_id)
        if by_mode is None:
            return None
        key = 'cross' if direction == 'long' else 'isolated'
        return sum(float(p.get('realizedPnl', 0) or 0)
                   for p in by_mode.get(key, []))

    def _calc_keep_contracts(self, inst_id: str, price: float, leverage: float,
                             target_usd: float) -> Optional[tuple]:
        """计算智能减仓的保留张数：目标保证金×杠杆折算，向下取整到 lotSz。

        折算结果不足最小下单量(minSz)时按 minSz 保留 —— 否则残留仓位
        将来平仓时订单低于 minSz 会被交易所拒单，永远平不掉。
        杠杆超过合约最大杠杆(max_lever)时自动 clamp（与开仓 _resolve_size 一致）。
        规格/价格缺失返回 None。成功返回 (keep, min_sz)。
        """
        spec = self.spec_cache.get_spec(inst_id)
        price = float(price or 0)
        if not spec or price <= 0:
            return None
        try:
            lev = float(leverage or 0)
        except (TypeError, ValueError):
            lev = 0.0
        max_lev = float(spec.get('max_lever') or 0)
        if max_lev > 0 and lev > max_lev:
            lev = max_lev
        keep = self.spec_cache.usd_to_contracts(
            inst_id, target_usd, price, leverage=lev, enforce_min_sz=False)
        min_sz = float(spec.get('min_sz') or 0)
        if keep < min_sz:
            keep = min_sz
        return keep, min_sz

    def _close_bucket_smart(self, inst_id: str, bucket: str, direction: str,
                            reason: str = '', run_id: str = '', short_period: str = '',
                            long_period: str = '', price: float = 0.0,
                            leverage: float = 0.0, scene: str = 'reversal') -> bool:
        """智能减仓止损：平仓前查整仓净已实现盈亏(realizedPnl)。

        - realizedPnl ≥ 0（未亏损）→ 按原逻辑全量平仓；
        - realizedPnl < 0（亏损）  → 放弃全平，市价减仓至目标保证金
          （默认 0.5U）的残留仓，保留持仓记录避免亏损了结计入胜负统计。

        仅白名单场景（scenes）且配置启用时生效，其余情况完全退化为
        _close_bucket 原逻辑；降级原则：realizedPnl 查询失败时按原逻辑全平
        （保持存量行为），规格/价格缺失无法计算保留量时跳过减仓下轮重试
        （亏损状态绝不因技术故障被动全平）。
        """
        rid = f"[{run_id}] " if run_id else ''
        bucket_cn = '趋势仓' if bucket == BUCKET_TREND else '区间仓'
        cfg = ((self._load_config().get('global_settings', {}) or {})
               .get('smart_reduce', {}) or {})
        scenes = cfg.get('scenes') or ['reversal', 'night_force']
        if not cfg.get('enabled', False) or scene not in scenes:
            return self._close_bucket(inst_id, bucket, direction, reason, run_id,
                                      short_period, long_period, price)

        held, avg_px = self.pos_mgr.get_position(inst_id, bucket, direction)
        if not held or held < self.POS_DUST:
            return False
        _lot, min_lot = self._steps(inst_id)
        if held < min_lot:
            # 粉尘持仓（低于该合约 minSz）：平仓单必被交易所拒单，直接清零账本
            # 视为已清理，避免反转清理永久失败而阻断两仓位新方向开仓；
            # 残留价值不足最小下单量，止损意义可忽略，粉尘清零不影响智能减仓统计。
            # 阈值按合约取而非硬编码 0.1：否则 XRP 真实可交易的 0.0x 张仓位会被误清
            self.pos_mgr.note_external_close(
                inst_id, bucket, direction, held,
                '粉尘清零(低于最小步长无法下单平仓)')
            return True

        rpnl = self._get_position_realized_pnl(inst_id, direction)
        if rpnl is None:
            task_log.warning(
                f"{rid}{inst_id} | 【智能减仓】整仓净已实现盈亏查询失败，"
                f"降级为全量平仓（保持存量行为）")
            return self._close_bucket(inst_id, bucket, direction, reason, run_id,
                                      short_period, long_period, price)
        if rpnl >= 0:
            task_log.info(
                f"{rid}{inst_id} | 【智能减仓】整仓净已实现盈亏{rpnl:+.4f}≥0（未亏损），"
                f"{bucket_cn}按原逻辑全量平仓")
            ok = self._close_bucket(inst_id, bucket, direction, reason, run_id,
                                    short_period, long_period, price)
            if ok:
                self._remove_residual(inst_id, bucket, direction)
            return ok

        target_usd = float(cfg.get('target_margin_usd', 0.5) or 0.5)
        res = self._calc_keep_contracts(inst_id, price, leverage, target_usd)
        if res is None:
            task_log.error(
                f"{rid}{inst_id} | 【智能减仓】合约规格/价格缺失，无法计算保留张数，"
                f"本轮跳过减仓（保留旧方向记录下轮重试）")
            return False
        keep, min_sz = res
        reduce_amt = self._q(inst_id, held - keep)
        if held <= keep + 1e-9 or reduce_amt <= 0:
            # 仓位已接近目标：持仓≤保留量，或减仓单不足最小下单量(minSz)会被拒
            task_log.info(
                f"{rid}{inst_id} | 【智能减仓】{bucket_cn}持仓{fmt_qty(held)}张已达/低于"
                f"保留目标（{fmt_qty(keep)}张，minSz={min_sz}），跳过减仓视为已达标")
            return True

        smart_reason = f"{reason}·智能减仓"
        task_log.info(
            f"{rid}{inst_id} | 【智能减仓】整仓净已实现盈亏{rpnl:+.4f}为亏损 → "
            f"{bucket_cn}放弃全平，市价减仓{fmt_qty(reduce_amt)}张，"
            f"保留{fmt_qty(keep)}张（目标保证金{target_usd}U）")
        ok = self._close_bucket(inst_id, bucket, direction, smart_reason, run_id,
                                short_period, long_period, price, amount=reduce_amt)
        if ok:
            self._record_residual(
                inst_id, bucket, direction, reduce_amt, keep, avg_px, rpnl,
                smart_reason, run_id, scene, target_usd)
            task_log.info(
                f"{rid}{inst_id} | 【智能减仓】减仓成功 | {bucket_cn}残留"
                f"{fmt_qty(keep)}张（账本均价{avg_px:.6g}），等待人工/后续转正处理")
        return ok

    # ---- 残留仓记录持久化（MySQL kv_store，供残留查询接口与人工操作使用） ----

    @staticmethod
    def _residual_key(inst_id: str, bucket: str, direction: str) -> str:
        return f"{inst_id}|{bucket}|{direction}"

    def _load_residuals(self) -> Dict:
        if session_scope is None or config_store_repo is None:
            return {}
        try:
            with session_scope() as session:
                return config_store_repo.load_json_config(
                    session, config_store_repo.KEY_SMART_REDUCE_RESIDUALS) or {}
        except Exception as e:
            task_log.warning(f"加载智能减仓残留记录失败: {e}")
            return {}

    def _save_residuals(self, residuals: Dict):
        if session_scope is None or config_store_repo is None:
            return
        try:
            with session_scope() as session:
                config_store_repo.save_json_config(
                    session, config_store_repo.KEY_SMART_REDUCE_RESIDUALS, residuals)
        except Exception as e:
            task_log.warning(f"保存智能减仓残留记录失败: {e}")

    def _record_residual(self, inst_id: str, bucket: str, direction: str,
                         reduce_amt: float, keep: float, avg_px: float,
                         realized_pnl: float, reason: str, run_id: str,
                         scene: str, target_usd: float):
        """减仓成功后登记残留仓快照（同 key 覆盖，多次减仓只留最新状态）"""
        residuals = self._load_residuals()
        residuals[self._residual_key(inst_id, bucket, direction)] = {
            'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'inst_id': inst_id,
            'bucket': bucket,
            'direction': direction,
            'reduce_amount': round(float(reduce_amt), 4),
            'keep_amount': round(float(keep), 4),
            'avg_px': round(float(avg_px), 8),
            'realized_pnl_at_reduce': round(float(realized_pnl), 6),
            'target_margin_usd': round(float(target_usd), 4),
            'scene': scene,
            'reason': reason,
            'run_id': run_id or '',
        }
        self._save_residuals(residuals)

    def _remove_residual(self, inst_id: str, bucket: str = None, direction: str = None):
        """删除残留记录：指定 bucket/direction 时删单条，否则删该合约全部

        （人工强平/转正后全平 时调用，避免残留记录与账本脱节）"""
        residuals = self._load_residuals()
        if not residuals:
            return
        if bucket and direction:
            keys = [self._residual_key(inst_id, bucket, direction)]
        else:
            keys = [k for k in residuals if k.startswith(f"{inst_id}|")]
        changed = False
        for k in keys:
            if k in residuals:
                del residuals[k]
                changed = True
        if changed:
            self._save_residuals(residuals)

    def force_close_manual(self, inst_id: str, close_type: str = 'all') -> List[str]:
        """人工强制平仓：先撤两个仓位的全部挂单并清空账本，再市价平掉真实持仓。

        挂单必须先撤，否则强平后残留的限价单仍会成交把仓位开回来；
        账本必须先清空，否则残留的 held 会让下一轮对账把已平掉的持仓
        重新算到某个仓位名下。

        close_type: 'all' 全平 | 'long' 平全仓多头 | 'short' 平逐仓空头
        """
        # 与调度轮互斥：等当前轮的“对账→止盈止损→挂单”完整结束后才强平，
        # 强平的“清账本→撤单→平仓”也不会被调度轮中途打断
        with self._trade_lock:
            msgs = []
            dirs = ('long', 'short') if close_type == 'all' else (close_type,)
            for d in dirs:
                for bucket in (BUCKET_TREND, BUCKET_RANGE):
                    n = self.pos_mgr.clear_bucket(inst_id, bucket, '人工强平', d)
                    if n:
                        label = '趋势仓' if bucket == BUCKET_TREND else '区间仓'
                        msgs.append(f'{label}撤单x{n}')
                self.tp_engine.clear_state(inst_id, d == 'long')
            if 'long' in dirs:
                ok = self._close_cross_positions(inst_id, '人工强平')
                msgs.append('平全仓多头' + ('已执行' if ok else '失败'))
            if 'short' in dirs:
                ok = self._close_isolated_positions(inst_id, '人工强平')
                msgs.append('平逐仓空头' + ('已执行' if ok else '失败'))
            # 人工强平后残留仓已不存在，同步清除智能减仓残留记录
            self._remove_residual(inst_id)

            # 人工强平后进入冷却期：禁止自动开新仓，避免下一轮(60s内)把
            # 刚平掉的仓位重新开回来；冷却时长可配，0=不冷却
            pause_min = float((self._load_config().get('global_settings', {})
                               .get('manual_close_pause_minutes', 30)) or 0)
            if pause_min > 0:
                self._manual_pause[inst_id] = time.time() + pause_min * 60
                self._save_manual_pause()
                self._pause_logged.add(inst_id)  # 本轮已提示，调度轮内不重复打
                msgs.append(f'冷却{pause_min:.0f}分钟内暂停自动开仓')
                task_log.info(
                    f"{inst_id} | 【人工强平】进入冷却期{pause_min:.0f}分钟，"
                    f"期间禁止自动开新仓")
            return msgs

    def _send_trade_email(self, inst_id: str, short_period: str, long_period: str,
                          trade_direction: str, execution_time: str,
                          price: float, success: bool, error_msg: str = ''):
        """发送交易操作结果邮件通知（成功/失败都发送）"""
        try:
            self.message_notifier.send_trade_operation_email(
                symbol=inst_id,
                short_period=short_period or '-',
                long_period=long_period or '-',
                trade_direction=trade_direction,
                execution_time=execution_time,
                price=float(price) if price else 0.0,
                success=success,
                error_msg=error_msg
            )
        except Exception as e:
            task_log.warning(f"发送交易邮件通知失败: {e}")

    # =================================================================
    # 持仓查询辅助
    # =================================================================

    def _read_positions_by_mode(self, inst_id: str) -> Optional[Dict]:
        """一次性读取全仓/逐仓净持仓；查询失败返回 None（区别于真实空仓 0.0）。

        返回 {'cross': 全仓净持仓, 'isolated': 逐仓净持仓}（正=多/负=空）；
        断网/API错误时返回 None，供上层跳过本轮交易，避免把“查询失败”
        误当成“空仓 0”而重复开仓超额。
        """
        by_mode = self.trade_executor.try_get_positions_by_mode(inst_id)
        if by_mode is None:
            return None
        cross = sum(float(p.get('pos', 0)) for p in by_mode.get('cross', []))
        isolated = sum(float(p.get('pos', 0)) for p in by_mode.get('isolated', []))
        return {'cross': cross, 'isolated': isolated}

    def _refresh_positions(self, inst_id: str, ctx: Dict) -> bool:
        """平仓动作后重新读取交易所持仓，同步 ctx 与本地账本对账基准。

        后续挂单量的 reduce-only 封顶依赖真实持仓，若沿用平仓前的旧值
        会挂出超量平仓单被交易所拒单。查询失败返回 False（沿用旧值）。
        """
        pos = self._read_positions_by_mode(inst_id)
        if pos is None:
            return False
        ctx['cross_pos'] = pos['cross']
        ctx['isolated_pos'] = pos['isolated']
        self.pos_mgr.reconcile(inst_id, pos['cross'], pos['isolated'],
                               price=float(ctx.get('price') or 0))
        return True

    # =================================================================
    # 执行类静默失效告警（下单被拒 / 结果未知孤儿单 / 止盈止损平仓失败）
    # 阈值来自 global_settings.risk_alerts，热加载、按账号隔离；缺省见下。
    # 所有方法整体 fail-safe：任何异常只记日志，绝不影响交易主流程、绝不抛出。
    # =================================================================

    _RISK_ALERT_DEFAULTS = {
        'enabled': True,
        'place_fail_rounds': 3,     # 单币连续 N 轮下单被拒 → 升级告警
        'sl_tp_fail_rounds': 2,     # 止盈/止损连续 N 轮平仓失败 → 升级告警
        # P2-a 账本与真实持仓大幅背离（缩减≈强平/爆仓，吸收≈人工加仓）
        'ledger_divergence_enabled': True,
        'shrink_alert_frac': 0.5,   # 缩减后仅保留 ≤ 此比例（真实/账本）→ critical（默认丢一半）
        'absorb_alert_frac': 1.0,   # 吸收量 ≥ 账本原合计的此倍数 → warning（默认翻倍）
        # P2-b 兜底委托不在位（该挂的交易所侧保险连续挂不上/被撤且重挂仍失败）
        'algo_fail_rounds': 3,      # 连续 N 轮"该挂在位却缺失" → 升级告警
        # P2-c 持久化降级（MySQL 记账层连续不可用，重启即丢冷却/计时/方向状态）
        'persist_fail_rounds': 3,   # DB 连接连续失败 ≥ 此数 → 降级叫醒告警
        # P2-d 链路健康聚合（整批币种 OKX 读接口成片超时/失败=链路级故障）
        'link_alert_rounds': 3,     # 连续 N 轮全批读失败 → 聚合系统告警（区别单币 _note_cycle_result）
    }

    def _risk_alerts_cfg(self) -> Dict:
        """读取风控告警配置（热加载），缺省字段用 _RISK_ALERT_DEFAULTS 兜底。"""
        try:
            ra = ((self._load_config().get('global_settings', {}) or {})
                  .get('risk_alerts', {}) or {})
        except Exception:
            ra = {}
        out = dict(self._RISK_ALERT_DEFAULTS)
        for k, v in ra.items():
            if v is not None:
                out[k] = v
        return out

    def _safe_risk_alert(self, inst_id: str, title: str,
                         rows: List[tuple], level: str = 'critical'):
        """发送风控告警邮件，异常吞掉只记日志（发信失败不得影响交易）。"""
        try:
            self.message_notifier.send_risk_alert(
                inst_id, title, detail_rows=rows, level=level)
        except Exception as e:
            task_log.error(f"{inst_id} | 风控告警邮件发送失败（{title}）: {e}")

    def _handle_place_failures(self, inst_id: str, run_id: str):
        """排空本轮下单失败记录并分级处理：
        - need_verify（结果未知、该单可能已落地）→ 立即升级"请人工核实"邮件（P0-3），
          不核实就重发=重复开仓，故不等阈值、首轮即发；
        - 普通拒单（51008/价格过滤/最小量）→ 计入连续轮数，达阈值发"下单持续被拒"
          告警（P0-1）；某轮无拒单即复位计数。
        """
        try:
            fails = self.pos_mgr.drain_place_failures()
        except Exception as e:
            task_log.warning(f"{inst_id} | 排空下单失败记录异常: {e}")
            return
        if not fails:
            self._place_fail_streak.pop(inst_id, None)   # 本轮干净 → 复位连续计数
            return
        cfg = self._risk_alerts_cfg()
        if not cfg.get('enabled', True):
            return
        rid = f"[{run_id}] " if run_id else ''
        verify = [f for f in fails if f.get('need_verify')]
        rejected = [f for f in fails if not f.get('need_verify')]

        def _brief(f):
            b_cn = '趋势仓' if f['bucket'] == BUCKET_TREND else '区间仓'
            s_cn = '开仓' if f['slot'] == 'entry' else '平仓'
            return f"{b_cn}·{s_cn} {f['amount']:g}张@{f['price']:.6g}"

        # P0-3：结果未知的孤儿单 —— 立即升级人工核实（不重发=可能重复开仓）
        if verify:
            first = verify[0]
            self._safe_risk_alert(
                inst_id, "下单结果未知，疑似孤儿单", level='critical',
                rows=[('异常单数', f'{len(verify)} 笔'),
                      ('最新一笔', _brief(first)),
                      ('交易所返回', str(first['error'])[:200]),
                      ('处置建议', '请先在交易所核对该挂单/成交是否已存在，确认为未落地后再重发。'
                               '系统下轮会自动重试挂单，若该单其实已成交可能造成重复开仓，务必人工确认。')])
            task_log.error(
                f"{rid}{inst_id} | 【下单结果未知】{len(verify)}笔需人工核实："
                f"{_brief(first)}")

        # P0-1：普通拒单连续计数（need_verify 不计入，避免与核实信重复）
        if rejected:
            streak = self._place_fail_streak.get(inst_id, 0) + 1
            self._place_fail_streak[inst_id] = streak
            thr = max(1, int(cfg.get('place_fail_rounds', 3)))
            if streak >= thr and (streak - thr) % thr == 0:   # 每 thr 轮重复提醒一次
                first = rejected[0]
                errs = '、'.join(sorted({str(f['error'])[:60] for f in rejected}))[:300]
                self._safe_risk_alert(
                    inst_id, f"下单连续被拒（已{streak}轮）", level='critical',
                    rows=[('连续拒单轮数', f'{streak} 轮（阈值{thr}）'),
                          ('本轮拒单笔数', f'{len(rejected)} 笔'),
                          ('最新篮子/单型', _brief(first)),
                          ('错误摘要', errs),
                          ('影响', '系统可能一单未挂而交易停摆（保证金不足51008/价格过滤/'
                                   '低于最小量等），但心跳日志照常不体现，请及时检查余额与下单参数。')])
                task_log.error(
                    f"{rid}{inst_id} | 【下单连续被拒】已{streak}轮，阈值{thr}，"
                    f"最新：{_brief(first)}｜{first['error']}")
        else:
            # 本轮只有 need_verify、无普通拒单 → 不推进"连续被拒"计数
            self._place_fail_streak.pop(inst_id, None)

    def _handle_ledger_divergence(self, inst_id: str, run_id: str):
        """账本与真实持仓大幅背离（P2-a）：在动作前那次 reconcile 之后排空背离事件。
        - 缩减（真实 << 账本）：非本程序动作导致的持仓凭空消失，几乎只有强平/爆仓/
          账户被人工平仓才会出现 → 保留比例 ≤ shrink_alert_frac 时叫醒级告警；
        - 吸收（真实 >> 账本）：人工同向加仓/入账遗漏 → 倍数 ≥ absorb_alert_frac 时提示级。
        缩减后对账已把账本封顶到真实，程序随即失去对原仓位的覆盖，故须"告警 + 留痕"。
        """
        try:
            events = self.pos_mgr.drain_divergences()
        except Exception as e:
            task_log.warning(f"{inst_id} | 排空账本背离记录异常: {e}")
            return
        if not events:
            return
        cfg = self._risk_alerts_cfg()
        if not cfg.get('enabled', True) or not cfg.get('ledger_divergence_enabled', True):
            return
        rid = f"[{run_id}] " if run_id else ''
        try:
            shrink_thr = float(cfg.get('shrink_alert_frac', 0.5))
            absorb_thr = float(cfg.get('absorb_alert_frac', 1.0))
        except Exception:
            shrink_thr, absorb_thr = 0.5, 1.0
        for ev in events:
            try:
                dcn = '多头' if ev.get('direction') == 'long' else '空头'
                if ev.get('kind') == 'shrink':
                    scale = float(ev.get('scale', 1) or 0)
                    if scale > shrink_thr:
                        continue
                    lost = max(0.0, (1 - scale) * 100)
                    self._safe_risk_alert(
                        inst_id, f'账本大幅背离·{dcn}持仓被系统外削减', level='critical',
                        rows=[('背离类型', '缩减（真实持仓 < 本地账本）'),
                              ('方向', dcn),
                              ('保留比例', f'仅约 {scale * 100:.1f}%（凭空消失约 {lost:.1f}%）'),
                              ('账本→真实', f"{ev.get('total', 0):g}张 → {ev.get('real', 0):g}张"),
                              ('篮子明细', str(ev.get('detail', ''))[:300]),
                              ('影响', '本轮任何程序平仓动作之前，账本就远高于交易所真实持仓，'
                                       '通常意味着已发生强平/爆仓或账户被人工平仓（非本程序所为）；'
                                       '系统对原仓位的止盈止损/兜底覆盖即刻失效，请立即核对持仓与保证金。')])
                    task_log.error(
                        f"{rid}{inst_id} | 【账本背离·缩减】{dcn} 账本{ev.get('total', 0):g}→"
                        f"真实{ev.get('real', 0):g}张(保留{scale * 100:.1f}%)：{ev.get('detail')}")
                elif ev.get('kind') == 'absorb':
                    ratio = float(ev.get('ratio', 0) or 0)
                    # total<=0 多见于重启/接管人工仓（从零吸收），不作背离告警避免误报
                    if float(ev.get('total', 0) or 0) <= 0 or ratio < absorb_thr:
                        continue
                    self._safe_risk_alert(
                        inst_id, f'账本大幅背离·{dcn}持仓被大幅吸收', level='warning',
                        rows=[('背离类型', '吸收（真实持仓 > 本地账本）'),
                              ('方向', dcn),
                              ('吸收倍数', f'超出账本约 {ratio * 100:.0f}%'),
                              ('账本→真实', f"{ev.get('total', 0):g}张 → {ev.get('real', 0):g}张"),
                              ('篮子明细', str(ev.get('detail', ''))[:300]),
                              ('影响', '交易所侧出现大量账本外的同向持仓被吸收进册，通常为人工加仓'
                                       '或上一轮入账遗漏；系统会按策略接管这部分，如非本人操作请核查。')])
                    task_log.warning(
                        f"{rid}{inst_id} | 【账本背离·吸收】{dcn} 超出账本{ratio * 100:.0f}%："
                        f"{ev.get('detail')}")
            except Exception as e:
                # 背离事件在动作前处理，任何格式化/取值异常都不得外溢中断该币本轮交易
                task_log.warning(f"{inst_id} | 账本背离事件处理异常（不影响交易）: {e}")

    def _check_persistence_health(self, run_id: str):
        """持久化层降级告警（P2-c）：读全局 DB 连接健康度（database.db_health），
        连续连接类失败 ≥ persist_fail_rounds → 叫醒级告警。DB 挂掉时实盘仍按内存态
        交易，但止盈止损冷却/反向持仓计时/长周期方向记录无法落库，期间一旦重启即丢
        状态、按冷启动默认重新判定 → 可能误平/漏平。恢复后发缓解并复位。
        仅在每轮结束调一次，全局 latch 去重；fail-safe，异常不影响交易。
        """
        try:
            if db_health is None:
                return
            h = db_health() or {}
            consec = int(h.get('consecutive_failures', 0) or 0)
            cfg = self._risk_alerts_cfg()
            thr = max(1, int(cfg.get('persist_fail_rounds', 3)))
            rid = f"[{run_id}] " if run_id else ''
            if consec >= thr:
                if not self._persist_alerted:
                    self._persist_alerted = True
                    task_log.error(
                        f"{rid}【持久化降级】MySQL 连接已连续失败 {consec} 次"
                        f"（阈值{thr}），冷却/计时/方向记录无法落库")
                    if cfg.get('enabled', True):
                        last_ok = float(h.get('last_ok_ts', 0) or 0)
                        self._safe_risk_alert(
                            'SYSTEM', f'持久化层降级（DB 连续失败 {consec} 次）',
                            level='critical',
                            rows=[('连续失败次数', f'{consec}（阈值{thr}）'),
                                  ('最近错误', str(h.get('last_error', ''))[:200]),
                                  ('最近成功写入',
                                   time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_ok))
                                   if last_ok else '—'),
                                  ('影响', '实盘仍按内存态交易，但止盈止损冷却/反向持仓计时/'
                                           '长周期方向记录等状态无法持久化；此期间若重启或崩溃，'
                                           '这些计时会丢失并按冷启动默认重新判定，可能触发误平/漏平。'
                                           '请尽快检查 MySQL 服务与网络。')])
            elif self._persist_alerted:
                self._persist_alerted = False
                task_log.info(f"{rid}【持久化恢复】MySQL 连接恢复正常，降级告警解除")
                if cfg.get('enabled', True):
                    self._safe_risk_alert(
                        'SYSTEM', '持久化层已恢复', level='recover',
                        rows=[('状态', 'MySQL 连接恢复正常，冷却/计时/方向记录重新落库')])
        except Exception as e:
            task_log.warning(f"持久化健康度检测异常（不影响交易）: {e}")

    def _note_link_health(self, run_id: str, total: int, ok_cnt: int, link_fail_cnt: int):
        """链路健康聚合告警（P2-d）：整批币种在同一轮内均因 OKX 读接口失败/超时
        （分析取数失败、持仓查询失败）而"保持不动作"，且连续 link_alert_rounds 轮
        → 发一封聚合系统邮件（区别于 _note_cycle_result 的单币连续失败、避免多币各发
        一封）。恢复后清 latch 发缓解。只在多币种（total≥2）时判定，单币无法区分
        "链路挂了"还是"这枚币自身问题"，交回 _note_cycle_result。fail-safe。
        """
        try:
            cfg = self._risk_alerts_cfg()
            thr = max(1, int(cfg.get('link_alert_rounds', 3)))
            # 本轮是否"链路成片异常"：多币种、无一成功、且每个币种都是读失败
            bad = (total >= 2 and ok_cnt == 0 and link_fail_cnt == total)
            rid = f"[{run_id}] " if run_id else ''
            if bad:
                self._link_bad_rounds += 1
                task_log.warning(
                    f"{rid}【链路健康】全批 {total} 币种 OKX 读失败、无一成功"
                    f"（连续{self._link_bad_rounds}轮）")
                if (self._link_bad_rounds >= thr and not self._link_alerted
                        and cfg.get('enabled', True)):
                    self._link_alerted = True
                    task_log.error(
                        f"{rid}【链路成片异常】连续{self._link_bad_rounds}轮全批读失败，"
                        f"升级聚合告警")
                    try:
                        self.message_notifier.send_system_alert(
                            symbol='OKX链路',
                            title=f'OKX 链路成片异常（连续{self._link_bad_rounds}轮全批读失败）',
                            detail=(f'实盘调度连续 {self._link_bad_rounds} 轮内全部 {total} 个币种'
                                    f'均因 OKX 只读接口失败/超时而"保持不动作"（既未分析成功也未取到持仓），'
                                    f'系统在此期间不做任何开平仓决策。多为网络抖动、OKX 限频或接口故障成片所致。'
                                    f'若持续，请检查出口网络 / OKX 状态 / 限频配置；'
                                    f'持仓在链路恢复前仅剩交易所侧兜底委托保护。'))
                    except Exception as e:
                        task_log.warning(f"{rid} 链路聚合告警邮件发送失败: {e}")
            else:
                if self._link_alerted:
                    self._link_alerted = False
                    task_log.info(f"{rid}【链路恢复】读取恢复，成片异常告警解除")
                    if cfg.get('enabled', True):
                        try:
                            self.message_notifier.send_system_alert(
                                symbol='OKX链路', title='OKX 链路已恢复',
                                detail='实盘调度读取恢复正常，链路成片异常告警解除。')
                        except Exception:
                            pass
                self._link_bad_rounds = 0
        except Exception as e:
            task_log.warning(f"链路健康聚合异常（不影响交易）: {e}")

    def _alert_sl_tp_fail(self, inst_id: str, kind_cn: str, side_cn: str,
                          reason: str, held: float, avg_px: float, price: float,
                          pnl_pct: float, run_id: str):
        """止盈/止损触发但平仓未成功（P0-2）：连续计数达阈值升级为"名义止损失效、
        持仓裸露"叫醒级告警。通用交易失败邮件已由 _close_amount 发过，此处是专项
        升级 + 连续失败留痕；平仓成功时由调用方复位 _sltp_fail_streak。"""
        try:
            cfg = self._risk_alerts_cfg()
            rid = f"[{run_id}] " if run_id else ''
            streak = self._sltp_fail_streak.get(inst_id, 0) + 1
            self._sltp_fail_streak[inst_id] = streak
            task_log.error(
                f"{rid}{inst_id} | 【{kind_cn}·平仓失败】{side_cn} | 已连续{streak}轮 | "
                f"原因：{reason} | 平仓委托未成功，下轮重试")
            if not cfg.get('enabled', True):
                return
            thr = max(1, int(cfg.get('sl_tp_fail_rounds', 2)))
            if streak >= thr and (streak - thr) % thr == 0:
                self._safe_risk_alert(
                    inst_id, f"{kind_cn}触发但平仓未成功（已{streak}轮）",
                    level='critical',
                    rows=[('触发类型', f'{kind_cn}（{reason}）'),
                          ('裸露仓位', f'{side_cn} {fmt_qty(held)}张 @均价{avg_px:.6g}'),
                          ('当前价格', f'{price:.6g}'),
                          ('浮动盈亏', fmt_pct(pnl_pct)),
                          ('连续失败', f'{streak} 轮（阈值{thr}）'),
                          ('风险', '止损/止盈名义已触发但仓位未实际平掉，等于此刻没有保护；'
                                   '系统每轮自动重试，若持续失败请人工介入平仓并检查余额/接口。')])
        except Exception as e:
            task_log.warning(f"{inst_id} | 止盈止损失败告警异常: {e}")

    # =================================================================
    # 止盈止损检查
    # =================================================================

    def _check_tp_sl(self, inst_id: str, current_price: float, analysis: Dict,
                     tp_cfg: Dict, sl_cfg: Dict, run_id: str = '',
                     short_period: str = '', long_period: str = '',
                     observe_only: bool = False) -> Dict:
        """趋势跟踪仓位的止盈止损检查（区间波动仓位自带对侧边界止盈，不参与）。

        盈亏一律基于**本地账本均价**计算 —— 净持仓模式下交易所返回的 avgPx 是
        两个仓位混合后的均价，无法用于单仓位盈亏判定。

        优先级：止损 → 止盈引擎（时间兜底 → 主止盈六选一）。全平触发后本轮不开新仓，
        分批止盈（close_partial）不阻断开仓。全平成功后启动止盈/止损冷却
        （tp_cooldown_periods 个短周期），冷却期内两个仓位禁止自动开新仓。
        observe_only=True（观察模式）时只评估与发邮件提醒，不执行平仓、不清引擎状态、
        不启动冷却。返回 {'triggered': bool, 'reason': str, 'cooldown_started': bool,
        'details': [触发+执行结果文本], 'monitor': [未触发持仓的监控状态文本]}。
        """
        rid = f"[{run_id}] " if run_id else ''
        result = {'triggered': False, 'reason': '', 'cooldown_started': False,
                  'details': [], 'monitor': []}
        a = analysis or {}
        atr_value = float(a.get('atr_value', 0) or 0)

        for direction in ('long', 'short'):
            is_long = direction == 'long'
            held, avg_px = self.pos_mgr.get_position(inst_id, BUCKET_TREND, direction)
            if held <= 0.01 or avg_px <= 0:
                # 无持仓 → 清理止盈引擎运行时状态（峰值/入场时间/阶梯档位）
                self.tp_engine.clear_state(inst_id, is_long)
                continue

            pnl_pct = ((current_price - avg_px) if is_long
                       else (avg_px - current_price)) / avg_px * 100
            side_cn = '趋势多头' if is_long else '趋势空头'

            # 1) 止损（最优先）
            sl_hit, sl_reason = self._eval_sl(
                pnl_pct, current_price, avg_px, atr_value, sl_cfg or {})
            if sl_hit:
                if observe_only:
                    task_log.warning(
                        f"{rid}{inst_id} | 【止损·观察】{side_cn} | 原因：{sl_reason} | "
                        f"均价{avg_px:.6g} 现价{current_price:.6g} "
                        f"盈亏{fmt_pct(pnl_pct)} | 观察模式未执行平仓")
                    self._send_watch_sl_tp(inst_id, '止损', side_cn, sl_reason,
                                           held, avg_px, current_price, pnl_pct)
                    result['details'].append(
                        f"{side_cn}止损触发({sl_reason}，盈亏{fmt_pct(pnl_pct)})观察未执行")
                    result['triggered'] = True
                    result['reason'] = sl_reason
                    continue
                task_log.warning(
                    f"{rid}{inst_id} | 【止损】{side_cn} | 全平{fmt_qty(held)}张 | "
                    f"原因：{sl_reason} | 均价{avg_px:.6g} 现价{current_price:.6g} "
                    f"盈亏{fmt_pct(pnl_pct)}")
                ok = self._close_bucket(inst_id, BUCKET_TREND, direction, sl_reason,
                                        run_id, short_period, long_period, current_price)
                if not ok:
                    self._alert_sl_tp_fail(inst_id, '止损', side_cn, sl_reason,
                                           held, avg_px, current_price, pnl_pct, run_id)
                else:
                    self._sltp_fail_streak.pop(inst_id, None)
                    if self._start_tp_cooldown(inst_id, sl_reason, short_period, run_id):
                        result['cooldown_started'] = True
                result['details'].append(
                    f"{side_cn}止损触发({sl_reason}，盈亏{fmt_pct(pnl_pct)})"
                    f"→平{fmt_qty(held)}张{'成功' if ok else '失败'}")
                self.tp_engine.clear_state(inst_id, is_long)
                result['triggered'] = True
                result['reason'] = sl_reason
                continue

            # 2) 止盈引擎（时间兜底 → 主止盈）
            ctx = {
                'inst_id': inst_id,
                'is_long': is_long,
                'pos_size': held,
                'avg_px': avg_px,
                'current_price': current_price,
                'pnl_pct': pnl_pct,
                'atr_value': atr_value,
                'boll_upper': a.get('boll_upper', 0),
                'boll_middle': a.get('boll_middle', 0),
                'boll_lower': a.get('boll_lower', 0),
                'macd_hist_series': a.get('macd_hist_series', []),
                'close_series': a.get('close_series', []),
                'short_period': short_period,
                'sl_cfg': sl_cfg or {},
            }
            decision = self.tp_engine.evaluate(tp_cfg or {}, ctx)
            action = decision.get('action', 'none')
            reason = decision.get('reason', '')
            if action == 'close_all':
                if observe_only:
                    task_log.warning(
                        f"{rid}{inst_id} | 【止盈·观察】{side_cn} | 原因：{reason} | "
                        f"均价{avg_px:.6g} 现价{current_price:.6g} "
                        f"盈亏{fmt_pct(pnl_pct)} | 观察模式未执行平仓")
                    self._send_watch_sl_tp(inst_id, '止盈', side_cn, reason,
                                           held, avg_px, current_price, pnl_pct)
                    result['details'].append(
                        f"{side_cn}止盈触发({reason}，盈亏{fmt_pct(pnl_pct)})观察未执行")
                    result['triggered'] = True
                    result['reason'] = reason
                    continue
                task_log.warning(
                    f"{rid}{inst_id} | 【止盈】{side_cn} | 全平{fmt_qty(held)}张 | "
                    f"原因：{reason} | 均价{avg_px:.6g} 现价{current_price:.6g} "
                    f"盈亏{fmt_pct(pnl_pct)}")
                ok = self._close_bucket(inst_id, BUCKET_TREND, direction, reason,
                                        run_id, short_period, long_period, current_price)
                if not ok:
                    self._alert_sl_tp_fail(inst_id, '止盈', side_cn, reason,
                                           held, avg_px, current_price, pnl_pct, run_id)
                else:
                    self._sltp_fail_streak.pop(inst_id, None)
                    if self._start_tp_cooldown(inst_id, reason, short_period, run_id):
                        result['cooldown_started'] = True
                result['details'].append(
                    f"{side_cn}止盈触发({reason}，盈亏{fmt_pct(pnl_pct)})"
                    f"→平{fmt_qty(held)}张{'成功' if ok else '失败'}")
                self.tp_engine.clear_state(inst_id, is_long)
                result['triggered'] = True
                result['reason'] = reason
            elif action == 'close_partial':
                if observe_only:
                    ratio = float(decision.get('close_ratio', 0) or 0)
                    task_log.warning(
                        f"{rid}{inst_id} | 【分批止盈·观察】{side_cn} | 建议平"
                        f"{ratio * 100:.0f}% | 原因：{reason} | 均价{avg_px:.6g} "
                        f"现价{current_price:.6g} 盈亏{fmt_pct(pnl_pct)} | 观察模式未执行")
                    self._send_watch_sl_tp(inst_id, '分批止盈', side_cn, reason,
                                           held, avg_px, current_price, pnl_pct)
                    result['details'].append(
                        f"{side_cn}分批止盈({reason})建议平{ratio * 100:.0f}%观察未执行")
                    continue
                ratio = float(decision.get('close_ratio', 0) or 0)
                amt = self._q(inst_id, held * ratio)
                task_log.warning(
                    f"{rid}{inst_id} | 【分批止盈】{side_cn} | 平{fmt_qty(amt)}张/"
                    f"{fmt_qty(held)}张 | 原因：{reason} | 均价{avg_px:.6g} "
                    f"现价{current_price:.6g} 盈亏{fmt_pct(pnl_pct)}")
                # 分批止盈不撤本仓位挂单（保留未成交的趋势开/平仓单），也不阻断开仓
                ok = self._close_bucket(inst_id, BUCKET_TREND, direction, reason,
                                        run_id, short_period, long_period, current_price,
                                        amount=amt, cancel_orders=False)
                result['details'].append(
                    f"{side_cn}分批止盈({reason})→平{fmt_qty(amt)}张/{fmt_qty(held)}张"
                    f"{'成功' if ok else '失败'}")
                # 末档分批即全平（剩余不足该合约最小下单量）：等同全平，同样启动冷却，
                # 否则分批清仓后仍会在窗口恒开条件下立即追价重开
                if ok and (held - amt) < self._steps(inst_id)[1] and \
                        self._start_tp_cooldown(inst_id, reason, short_period, run_id):
                    result['cooldown_started'] = True
            else:
                # 未触发 → 记录持仓监控状态（供本轮摘要展示止盈止损监控中的仓位）
                result['monitor'].append(
                    f"{side_cn}{fmt_qty(held)}张@均价{avg_px:.6g} "
                    f"盈亏{fmt_pct(pnl_pct)} 监控中")

        return result

    def _send_watch_sl_tp(self, inst_id: str, kind_cn: str, side_cn: str, reason: str,
                          held: float, avg_px: float, current_price: float,
                          pnl_pct: float):
        """观察模式下止盈/止损触发提醒邮件（不执行平仓，冷却+指纹去重防刷屏）"""
        try:
            self.message_notifier.send_watch_mode_alert(
                inst_id, 'sl_tp', f'{kind_cn}信号 - {side_cn}',
                detail_rows=[('触发原因', reason),
                             ('持仓量', f'{fmt_qty(held)}张'),
                             ('持仓均价', f'{avg_px:.6g}'),
                             ('当前价格', f'{current_price:.6g}'),
                             ('浮动盈亏', fmt_pct(pnl_pct))],
                hint='观察模式下系统未执行平仓，请人工决定是否处理；'
                     '已挂的交易所兜底止盈止损委托仍按原样生效。')
        except Exception as e:
            task_log.warning(f"{inst_id} | 观察模式止盈止损邮件发送失败: {e}")

    def _eval_sl(self, pnl_pct: float, current_price: float, avg_px: float,
                 atr_value: float, sl_cfg: Dict) -> tuple:
        """评估止损是否触发（止盈已移交 TakeProfitEngine）。返回 (triggered, reason)"""
        if sl_cfg.get('enabled', False) and pnl_pct < 0:
            sl_type = sl_cfg.get('type', 'fixed')
            if sl_type == 'fixed':
                fixed_pct = float(sl_cfg.get('fixed_pct', 3.0))
                if abs(pnl_pct) >= fixed_pct:
                    return True, f'固定止损 {abs(pnl_pct):.2f}%≥{fixed_pct}%'
            elif sl_type == 'atr':
                atr_multiple = float(sl_cfg.get('atr_multiple', 1.5))
                if atr_value > 0:
                    loss_distance = abs(current_price - avg_px)
                    if loss_distance >= atr_multiple * atr_value:
                        return True, f'ATR止损 {loss_distance:.4f}≥{atr_multiple}ATR({atr_value:.4f})'

        return False, ''

    # =================================================================
    # 双仓位调度（趋势跟踪 + 区间波动）
    # =================================================================
    
    @staticmethod
    def _fill_label(fill: Dict) -> str:
        """限价成交事件 → 交易方向标签（邮件/日志用）"""
        is_long = fill.get('dir') == 'long'
        if fill.get('slot') == 'entry':
            return '开多' if is_long else '开空'
        return '平多' if is_long else '平空'
    
    def _notify_fills(self, inst_id: str, bucket_cn: str, fills, ctx: Dict):
        """限价单成交补发邮件通知（交易日志已由挂单管理器记录）"""
        for fx in fills or []:
            self._send_trade_email(
                inst_id, ctx['short_period'], ctx['long_period'],
                f"{self._fill_label(fx)}({bucket_cn}限价成交)",
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                fx.get('price', 0), True)
    
    def _in_night_window(self, cfg: Dict) -> bool:
        """是否处于东八区睡眠时段（长周期反转需强平而非仅发提醒邮件）。
    
        显式按 UTC+8 折算，不依赖服务器本地时区设置。
        """
        nc = (cfg.get('global_settings', {}) or {}).get('night_force_close', {}) or {}
        if not nc.get('enabled', True):
            return False
        start = int(nc.get('start_hour', 0) or 0)
        end = int(nc.get('end_hour', 6) or 0)
        if start == end:
            return False
        hour = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=8)).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end   # 跨零点区间
    
    def _notify_long_reversal(self, inst_id: str, ctx: Dict, prev_long: str,
                              long_direction: str, forced_trend: bool,
                              closed_range: bool):
        """长周期趋势方向反转提醒（睡眠时段附带强平说明）"""
        dir_cn = {'long': '看多', 'short': '看空'}
        title = (f"长周期趋势方向从{dir_cn.get(prev_long, prev_long)}"
                 f"转为{dir_cn.get(long_direction, long_direction)}")
        notes = ['趋势仓位已强制平仓(睡眠时段)' if forced_trend else '趋势仓位保持不动']
        if closed_range:
            notes.append('区间仓位已强平并换方向重挂')
        task_log.warning(
            f"{ctx['rid']}{inst_id} | 【信号反转】长周期 "
            f"{dir_cn.get(prev_long, prev_long)}→{dir_cn.get(long_direction, long_direction)} | "
            f"趋势仓={'已强平(睡眠时段)' if forced_trend else '保持不动'} | "
            f"区间仓={'已强平并换向重挂' if closed_range else '无动作'}")
        self._send_trade_email(
            inst_id, ctx['short_period'], ctx['long_period'],
            f"{title}（{'，'.join(notes)}）",
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            ctx['price'], True)
    
    def _resolve_size(self, inst_id: str, pcfg: Dict, ctx: Dict, label: str) -> float:
        """仓位下单量解析：张数/金额双模式统一出口。

        - size_mode='contracts'（默认，存量行为）：取 contracts 张数，并按合约 lotSz
          向下取整；低于 minSz 返回 0 并告警（下单必被交易所拒，不如不挂）
        - size_mode='usd'：notional_usd 为占用保证金预算，按本轮现价 + 杠杆折算成张数，
          公式：张数 = floor(金额 × 杠杆 ÷ 每张面值 ÷ lotSz) × lotSz；
          向下取整到 lotSz 步长（实际占用保证金不超预算）；低于 minSz
          或规格/价格缺失返回 0（挂单管理器对 0 张自然不挂单）。
          杠杆优先取仓位配置 pcfg.leverage，缺省回退币种级 ctx.leverage，
          再缺省按 1x（无杠杆）。
          若配置杠杆超过合约支持的最大杠杆（max_lever），自动降到上限并告警。
        """
        mode = str(pcfg.get('size_mode', 'contracts') or 'contracts').lower()
        if mode != 'usd':
            # 张数模式也必须按合约步长校验：各合约 lotSz/minSz 差异极大
            # （XRP=0.01 / NEAR=0.1 / POL=1），直接拿配置值下单会被交易所拒
            # （POL 配 0.5 张 → 每轮 All operations failed，2026-09-01 实盘现象）
            raw = float(pcfg.get('contracts', 0) or 0)
            fixed = self.spec_cache.quantize(inst_id, raw)
            key = f"{inst_id}:{label}:lot"
            if fixed <= 0 < raw:
                if key not in self._size_warned:
                    self._size_warned.add(key)
                    lot, min_sz = self.spec_cache.steps(inst_id)
                    task_log.warning(
                        f"{ctx.get('rid', '')}{inst_id} | 【{label}张数模式】配置{raw}张"
                        f"低于该合约最小下单量{min_sz:g}张（步长{lot:g}张），"
                        f"下单必被交易所拒，本仓位暂不挂单：请把张数改为"
                        f"≥{min_sz:g}且为{lot:g}的整数倍，或改用金额模式(usd)")
            else:
                self._size_warned.discard(key)
            return fixed
        usd = float(pcfg.get('notional_usd', 0) or 0)
        if usd <= 0:
            return 0.0
        price = float(ctx.get('price', 0) or 0)
        leverage = float(pcfg.get('leverage') or ctx.get('leverage') or 0)
        # 自动 clamp 杠杆到合约最大杠杆上限，避免下单被交易所拒
        spec = self.spec_cache.get_spec(inst_id)
        max_lev = float(spec.get('max_lever') or 0) if spec else 0
        if max_lev > 0 and leverage > max_lev:
            key = f"{inst_id}:{label}:leverage_clamp"
            if key not in self._size_warned:
                self._size_warned.add(key)
                task_log.warning(
                    f"{ctx.get('rid', '')}{inst_id} | 【{label}】配置杠杆{leverage}x"
                    f"超过该合约最大杠杆{max_lev}x，自动降到{max_lev}x")
            leverage = max_lev
        contracts = self.spec_cache.usd_to_contracts(
            inst_id, usd, price, leverage=leverage)
        key = f"{inst_id}:{label}"
        if contracts > 0:
            self._size_warned.discard(key)
        elif key not in self._size_warned:
            # 去重告警：换算失败每轮都会命中，只在首轮提示避免刷屏
            self._size_warned.add(key)
            per = self.spec_cache.contract_usd_value(inst_id, price)
            task_log.warning(
                f"{ctx.get('rid', '')}{inst_id} | 【{label}金额模式】{usd} USDT保证金 无法折算"
                f"有效张数（现价={price}，杠杆={leverage or 1}x，每张≈{per:.6g} USDT）："
                f"规格/价格缺失或保证金×杠杆不足最小下单量(minSz)，本仓位暂不挂单")
        return contracts

    def _run_trend_position(self, inst_id: str, tcfg: Dict, ctx: Dict) -> Dict:
        """趋势跟踪仓位一轮调度：构建 plan → 交由双仓位挂单管理器执行。"""
        rid = ctx['rid']
        mode = str(tcfg.get('period_mode', 'dual') or 'dual').lower()
        strat_cn = '单周期Pro3双向' if mode == 'single' else '双周期Pro3单向'
        ctx['trend_strategy'] = strat_cn
        if not tcfg.get('enabled', True):
            n = self.pos_mgr.cancel_bucket_orders(inst_id, BUCKET_TREND, '趋势仓位已停用')
            bk = self.pos_mgr.get_book(inst_id, BUCKET_TREND)
            held_sum = round(bk['held']['long'] + bk['held']['short'], 4)
            task_log.info(
                f"{rid}{inst_id} | 【仓位停用】趋势仓 | 撤销挂单{n}笔"
                + (f"，仍持仓{held_sum}张待人工处理" if held_sum > 0.01 else ''))
            # 停用期间窗口开合不参与上升沿记录，复位避免重新启用后漏报
            self._window_open_prev.pop(inst_id, None)
            return {'actions': [], 'fills': []}
    
        a = ctx['analysis']
        entry_px = float(a.get('reversal_open' if ctx['entry_price_type'] == 'open'
                               else 'reversal_close', 0) or 0) or ctx['price']
        exit_px = float(a.get('reversal_open' if ctx['exit_price_type'] == 'open'
                              else 'reversal_close', 0) or 0) or ctx['price']
        plan = {
            'period_mode': mode,
            'entry_px': entry_px,
            'exit_px': exit_px,
            'contracts': self._resolve_size(inst_id, tcfg, ctx, '趋势仓'),
            'leverage': ctx['leverage'],
            'allow_entry': ctx['trend_allow_entry'],
            'max_position': ctx.get('max_position', 0),
            'max_total_position': ctx.get('max_total_position', 0),
            'dir_source': ctx.get('dir_source', '信号驱动'),
        }
        if mode == 'single':
            # 单周期双向：只看短周期方向，翻转即平旧开新，无窗口概念
            plan['target_dir'] = ctx['short_dir']
            self._window_open_prev.pop(inst_id, None)
        else:
            # 双周期单向：只做长周期方向，三时段窗口确认
            desired = ctx['long_dir']
            opp = 'short' if desired == 'long' else 'long'
            plan['target_dir'] = desired
            plan['open_window'] = (ctx['short_dir'] == desired
                                   and ctx['short_prev'] == desired
                                   and ctx['short_prev_prev'] == opp)
            plan['close_window'] = (ctx['short_dir'] == opp
                                    and ctx['short_prev'] == opp
                                    and ctx['short_prev_prev'] == desired)
            # 开仓窗口上升沿提醒：进入窗口只发一封邮件（方向+挂单参考价），
            # 窗口持续期间不重复，窗口结束后复位；发送失败也不重试刷屏
            ow = bool(plan['open_window'])
            if ow and not self._window_open_prev.get(inst_id, False):
                dir_cn = '开多' if desired == 'long' else '开空'
                task_log.info(
                    f"{rid}{inst_id} | 【开仓窗口】{dir_cn} | 参考价={entry_px:.6g}"
                    f" | 计划挂单{plan['contracts']:g}张，已发送提醒邮件")
                self.message_notifier.send_entry_window_alert(
                    inst_id, ctx['short_period'], ctx['long_period'],
                    dir_cn, entry_px, contracts=plan['contracts'])
            self._window_open_prev[inst_id] = ow
    
        res = self.pos_mgr.process_trend(inst_id, plan,
                                         ctx['cross_pos'], ctx['isolated_pos'])
        ctx['trend_plan'] = plan
        # 静默原则：无动作不打状态日志（持仓/方向已浓缩进本轮心跳行）；
        # 挂单/成交/撤单等事件已由挂单管理器按场景模板输出
        self._notify_fills(inst_id, '趋势跟踪', res.get('fills'), ctx)
        return res
    
    def _sync_trend_algo(self, inst_id: str, tcfg: Dict, ctx: Dict):
        """同步趋势仓位的交易所侧止盈止损兜底委托（本地评估的“保险”那一层）。
    
        触发价基于本地账本均价推导；只有固定目标止盈与固定/ATR 止损能表达为
        静态触发价，其余四类止盈仍由 tp_engine 本地评估。
        """
        backup_on = bool(tcfg.get('exchange_algo_backup', True)) and tcfg.get('enabled', True)
        atr_value = float((ctx['analysis'] or {}).get('atr_value', 0) or 0)
        run_id = ctx.get('run_id', '')
        for direction in ('long', 'short'):
            held, avg_px = self.pos_mgr.get_position(inst_id, BUCKET_TREND, direction)
            if not backup_on or held <= 0.01 or avg_px <= 0:
                # 开关关闭 / 无持仓 → 清除已挂兜底委托，并复位"保险缺失"计数（此时无保险可言缺失）
                self.pos_mgr.sync_exchange_algo(inst_id, BUCKET_TREND, direction)
                self._algo_fail_streak.pop(f"{inst_id}|{direction}", None)
                continue
            levels = self.tp_engine.exchange_backup_levels(
                tcfg.get('take_profit') or {}, tcfg.get('stop_loss') or {},
                {'is_long': direction == 'long', 'avg_px': avg_px,
                 'atr_value': atr_value, 'sl_cfg': tcfg.get('stop_loss') or {}})
            sl_px = levels.get('sl_trigger_px')
            tp_px = levels.get('tp_trigger_px')
            act = self.pos_mgr.sync_exchange_algo(
                inst_id, BUCKET_TREND, direction,
                sl_trigger_px=sl_px, tp_trigger_px=tp_px, amount=held)
            if act and levels.get('note'):
                task_log.info(
                    f"{ctx['rid']}{inst_id} | 【兜底委托】{levels['note']}")
            # P2-b 兜底委托不在位：确有持仓且要求了静态触发价，但 sync 后交易所侧仍不在位
            self._note_algo_backup(inst_id, direction, held, avg_px,
                                   sl_px, tp_px, run_id)

    def _note_algo_backup(self, inst_id: str, direction: str, held: float,
                          avg_px: float, sl_px, tp_px, run_id: str):
        """兜底委托"该挂在位却缺失"连续计数并升级告警（P2-b）。
        交易所侧止盈止损是本地宕机/断网时的最后防线，sync_exchange_algo 挂失败或被撤
        重挂又失败时仅 warning，静默期一长就等于裸奔。这里按 inst|direction 计连续轮数，
        达阈值（每阈值轮重复提醒）升级叫醒级；恢复在位即复位。全程 fail-safe。
        """
        key = f"{inst_id}|{direction}"
        try:
            wants = (float(sl_px or 0) > 0) or (float(tp_px or 0) > 0)
            if not wants:
                # 无可表达的静态触发价（仅动态止盈，由 tp_engine 本地评估）→ 不判缺失
                self._algo_fail_streak.pop(key, None)
                return
            if self.pos_mgr.algo_in_place(inst_id, BUCKET_TREND, direction):
                if self._algo_fail_streak.pop(key, None):
                    task_log.info(
                        f"{inst_id} | 兜底委托恢复在位"
                        f"（趋势仓{'多头' if direction == 'long' else '空头'}）")
                return
            cfg = self._risk_alerts_cfg()
            streak = self._algo_fail_streak.get(key, 0) + 1
            self._algo_fail_streak[key] = streak
            dcn = '多头' if direction == 'long' else '空头'
            rid = f"[{run_id}] " if run_id else ''
            task_log.warning(
                f"{rid}{inst_id} | 【兜底委托不在位】趋势仓{dcn} {held:g}张，"
                f"交易所侧保险未挂上（已连续{streak}轮）")
            if not cfg.get('enabled', True):
                return
            thr = max(1, int(cfg.get('algo_fail_rounds', 3)))
            if streak >= thr and (streak - thr) % thr == 0:
                sl_disp = f'{float(sl_px):.6g}' if float(sl_px or 0) > 0 else '—'
                tp_disp = f'{float(tp_px):.6g}' if float(tp_px or 0) > 0 else '—'
                self._safe_risk_alert(
                    inst_id, f'兜底委托连续{streak}轮挂不上（趋势仓{dcn}）',
                    level='critical',
                    rows=[('篮子/方向', f'趋势跟踪·{dcn}'),
                          ('持仓', f'{held:g}张 @ 均价{avg_px:.6g}'),
                          ('期望触发价', f'止损 {sl_disp} / 止盈 {tp_disp}'),
                          ('连续缺失轮数', f'{streak} 轮（阈值{thr}）'),
                          ('影响', '交易所侧止盈止损兜底委托是程序宕机/断网时的最后一道防线；'
                                   '连续多轮挂不上意味着此刻若本地失联，该仓位在交易所侧将无任何'
                                   '自动平仓保护。请检查保证金是否足额、交易权限是否正常、OKX 策略'
                                   '委托接口是否异常。')])
        except Exception as e:
            task_log.warning(f"{inst_id} | 兜底委托在位检测异常（不影响交易）: {e}")
    
    def _run_range_position(self, inst_id: str, rcfg: Dict, ctx: Dict) -> Dict:
        """区间波动仓位一轮调度：BOLL 边界限价开平、无限循环刷区间。"""
        rid = ctx['rid']
        if not rcfg.get('enabled', True):
            n = self.pos_mgr.cancel_bucket_orders(inst_id, BUCKET_RANGE, '区间仓位已停用')
            bk = self.pos_mgr.get_book(inst_id, BUCKET_RANGE)
            held_sum = round(bk['held']['long'] + bk['held']['short'], 4)
            task_log.info(
                f"{rid}{inst_id} | 【仓位停用】区间仓 | 撤销挂单{n}笔"
                + (f"，仍持仓{held_sum}张待人工处理" if held_sum > 0.01 else ''))
            return {'actions': [], 'fills': []}
    
        a = ctx['analysis']
        upper = float(a.get('boll_upper', 0) or 0)
        lower = float(a.get('boll_lower', 0) or 0)
        if upper <= 0 or lower <= 0:
            task_log.warning(
                f"{rid}{inst_id} | 【区间跳过】BOLL边界不可用（上={upper} 下={lower}），"
                f"本轮不挂单")
            return {'actions': [], 'fills': []}
    
        desired = ctx['long_dir']
        plan = {
            'target_dir': desired,
            'entry_px': lower if desired == 'long' else upper,
            'exit_px': upper if desired == 'long' else lower,
            'contracts': self._resolve_size(inst_id, rcfg, ctx, '区间仓'),
            'leverage': ctx['leverage'],
            'amend_min_pct': float(rcfg.get('boll_amend_min_pct', 0.001) or 0),
            'allow_entry': ctx.get('range_allow_entry', True),
            'max_position': ctx.get('max_position', 0),
            'max_total_position': ctx.get('max_total_position', 0),
            'dir_source': ctx.get('dir_source', '信号驱动'),
        }
        res = self.pos_mgr.process_range(inst_id, plan,
                                         ctx['cross_pos'], ctx['isolated_pos'])
        # 静默原则同趋势仓：状态信息进心跳行，事件由挂单管理器场景化输出
        self._notify_fills(inst_id, '区间波动', res.get('fills'), ctx)
        return res

    # =================================================================
    # 观察模式（trade_enabled=false）：只分析 + 邮件提醒，对交易所零操作
    # =================================================================

    def _watch_edge(self, inst_id: str, name: str, state: bool) -> bool:
        """观察模式事件上升沿：状态由 False→True 返回 True（每段只触发一次）"""
        key = f"{inst_id}|{name}"
        prev = self._watch_edges.get(key, False)
        self._watch_edges[key] = state
        return state and not prev

    def _watch_trend_alerts(self, inst_id: str, tcfg: Dict, ctx: Dict) -> Dict:
        """观察模式趋势仓提醒：开仓窗口/平仓信号/单周期翻转只发邮件不下单"""
        rid = ctx['rid']
        actions = []
        if not tcfg.get('enabled', True):
            return {'actions': actions, 'fills': []}
        mode = str(tcfg.get('period_mode', 'dual') or 'dual').lower()
        a = ctx['analysis']
        entry_px = float(a.get('reversal_open' if ctx['entry_price_type'] == 'open'
                               else 'reversal_close', 0) or 0) or ctx['price']

        if mode == 'single':
            # 单周期双向：短周期方向翻转 → 提醒平旧仓开新仓
            target = ctx['short_dir']
            prev_target = self._watch_single_prev.get(inst_id)
            self._watch_single_prev[inst_id] = target
            if prev_target and prev_target != target:
                held, _ = self.pos_mgr.get_position(inst_id, BUCKET_TREND, prev_target)
                old_cn = '多头' if prev_target == 'long' else '空头'
                new_cn = '开多' if target == 'long' else '开空'
                task_log.info(
                    f"{rid}{inst_id} | 【观察】单周期翻转 | "
                    f"{fmt_dir(prev_target)}→{fmt_dir(target)} | 建议平{old_cn}并{new_cn}")
                actions.append(f'观察:翻转建议{new_cn}')
                if held > 0.01:
                    try:
                        self.message_notifier.send_watch_mode_alert(
                            inst_id, 'exit_signal',
                            f'信号翻转：建议平{old_cn}并{new_cn}',
                            detail_rows=[('持仓模式', '单周期双向'),
                                         ('方向变化',
                                          f'{fmt_dir(prev_target)}→{fmt_dir(target)}'),
                                         ('持有仓位', f'{old_cn}{fmt_qty(held)}张'),
                                         ('参考价位', f'{entry_px:.6g}')])
                    except Exception as e:
                        task_log.warning(
                            f"{rid}{inst_id} | 观察模式翻转邮件发送失败: {e}")
            return {'actions': actions, 'fills': []}

        # 双周期单向：三时段窗口确认，只做长周期方向
        desired = ctx['long_dir']
        opp = 'short' if desired == 'long' else 'long'
        open_window = (ctx['short_dir'] == desired and ctx['short_prev'] == desired
                       and ctx['short_prev_prev'] == opp)
        close_window = (ctx['short_dir'] == opp and ctx['short_prev'] == opp
                        and ctx['short_prev_prev'] == desired)
        if self._watch_edge(inst_id, 'trend_open_window', open_window):
            dir_cn = '开多' if desired == 'long' else '开空'
            task_log.info(
                f"{rid}{inst_id} | 【观察】开仓窗口 | {dir_cn} | 参考价={entry_px:.6g}")
            actions.append(f'观察:{dir_cn}窗口')
            try:
                self.message_notifier.send_watch_mode_alert(
                    inst_id, 'entry_window', f'建议{dir_cn}（开仓窗口）',
                    detail_rows=[('交易周期',
                                  f"短周期 {ctx['short_period']} / 长周期 {ctx['long_period']}"),
                                 ('交易方向', dir_cn),
                                 ('挂单参考价', f'{entry_px:.6g}')],
                    hint='双周期共振开仓窗口已确认，观察模式下系统未挂单，'
                         '请人工决定是否跟踪。')
            except Exception as e:
                task_log.warning(f"{rid}{inst_id} | 观察模式开仓窗口邮件发送失败: {e}")
        if self._watch_edge(inst_id, 'trend_close_window', close_window):
            held, _ = self.pos_mgr.get_position(inst_id, BUCKET_TREND, desired)
            if held > 0.01:
                dir_cn = '平多' if desired == 'long' else '平空'
                task_log.info(
                    f"{rid}{inst_id} | 【观察】平仓窗口 | {dir_cn}{fmt_qty(held)}张")
                actions.append(f'观察:{dir_cn}窗口')
                try:
                    self.message_notifier.send_watch_mode_alert(
                        inst_id, 'exit_signal', f'平仓信号（平仓窗口）建议{dir_cn}',
                        detail_rows=[('持有仓位', f'{fmt_dir(desired)}{fmt_qty(held)}张'),
                                     ('平仓窗口', f'短周期连续{fmt_dir(opp)}')],
                        hint='观察模式下系统未执行平仓，请人工决定是否跟踪。')
                except Exception as e:
                    task_log.warning(f"{rid}{inst_id} | 观察模式平仓窗口邮件发送失败: {e}")
        return {'actions': actions, 'fills': []}

    def _watch_range_alerts(self, inst_id: str, rcfg: Dict, ctx: Dict) -> Dict:
        """观察模式区间仓提醒：价格触及 BOLL 入场边界时发入场机会邮件"""
        rid = ctx['rid']
        actions = []
        if not rcfg.get('enabled', True):
            return {'actions': actions, 'fills': []}
        a = ctx['analysis']
        upper = float(a.get('boll_upper', 0) or 0)
        lower = float(a.get('boll_lower', 0) or 0)
        if upper <= 0 or lower <= 0:
            return {'actions': actions, 'fills': []}
        desired = ctx['long_dir']
        boundary = lower if desired == 'long' else upper
        touch = (ctx['price'] <= lower) if desired == 'long' else (ctx['price'] >= upper)
        if self._watch_edge(inst_id, 'range_boundary', touch):
            dir_cn = '开多' if desired == 'long' else '开空'
            task_log.info(
                f"{rid}{inst_id} | 【观察】区间仓BOLL边界 | {dir_cn} | "
                f"边界价={boundary:.6g} 现价={ctx['price']:.6g}")
            actions.append(f'观察:区间{dir_cn}机会')
            try:
                self.message_notifier.send_watch_mode_alert(
                    inst_id, 'range_entry', f'区间仓BOLL边界入场机会（{dir_cn}）',
                    detail_rows=[('入场边界',
                                  f"{boundary:.6g}（{'下轨' if desired == 'long' else '上轨'}）"),
                                 ('当前价格', f"{ctx['price']:.6g}"),
                                 ('对侧止盈边界',
                                  f"{(upper if desired == 'long' else lower):.6g}")])
            except Exception as e:
                task_log.warning(f"{rid}{inst_id} | 观察模式区间仓邮件发送失败: {e}")
        return {'actions': actions, 'fills': []}
    
    # =================================================================
    # 核心交易逻辑
    # =================================================================

    def _resolve_directions(self, inst_id: str, manual_direction: str,
                            trend_mode: str, dirs: Dict) -> Dict:
        """方向决议：人工方向锁定 + 长周期上一时段稳定化（2026-09-01 语义修正）。

        方向锁定（配置 manual_direction=long/short 或页面 manual_override_cache）
        语义：只允许持有该方向仓位、绝不反向开仓 —— 锁定只作用于长周期方向；
        双周期单向模式短周期三时段一律保留真实信号，开仓仍须通过真实三时段
        共振窗口，反向信号窗口期不开仓也不反向。旧实现把 short_prev/
        short_prev_prev 伪造成“刚刚反转”，使 _run_trend_position 的 open_window
        恒为 True，空头信号窗口期仍在高位追涨补多（2026-09-01 实盘事故根因），
        已废弃。单周期双向模式无三时段窗口概念，方向即持仓方向，短周期一并锁定。

        Args:
            dirs: {'short', 'short_prev', 'short_prev_prev', 'long_raw', 'long_prev'}

        Returns:
            同名短周期三键 + 'long'（最终长周期方向）/ 'long_prev' /
            'locked_dir'（无锁定为 None）/ 'lock_src' / 'dir_source'（方向来源文案）
        """
        out = {
            'short': dirs.get('short'),
            'short_prev': dirs.get('short_prev'),
            'short_prev_prev': dirs.get('short_prev_prev'),
            'long_prev': dirs.get('long_prev'),
        }
        locked_dir = manual_direction if manual_direction in ('long', 'short') else None
        lock_src = '配置锁定'
        if not locked_dir:
            override = str(self.manual_override_cache.get(inst_id) or '').lower()
            if override in ('long', 'short'):
                locked_dir = override
                lock_src = '页面锁定'
        if locked_dir:
            out['long'] = locked_dir
            out['long_prev'] = locked_dir
            out['locked_dir'] = locked_dir
            out['lock_src'] = lock_src
            out['dir_source'] = f'{lock_src}({locked_dir})'
            if trend_mode == 'single':
                out['short'] = locked_dir
                out['short_prev'] = locked_dir
                out['short_prev_prev'] = locked_dir
        else:
            out['locked_dir'] = None
            out['lock_src'] = ''
            out['dir_source'] = '信号驱动'
            # 长周期使用上一时段方向（避免震荡期频繁反转）
            out['long'] = dirs.get('long_prev') or dirs.get('long_raw')
        return out

    def analyze_and_trade_real(self, inst_id: str, short_period: str, long_period: str,
                                trend_cfg: Dict = None, range_cfg: Dict = None,
                                manual_direction: str = 'auto', run_id: str = '',
                                leverage: int = 0, signal_algo: str = None,
                                entry_price_type: str = None, exit_price_type: str = None,
                                verbose_lifecycle: bool = True,
                                trade_enabled: bool = True) -> Dict:
        """实盘分析并调度两个相互独立的仓位（趋势跟踪 + 区间波动）

        Args:
            inst_id: 合约ID (e.g. NEAR-USDT-SWAP)
            short_period: 短周期 (e.g. 1m) — 趋势仓开平时机；单周期模式下的方向来源
            long_period: 长周期 (e.g. 15m) — 单向模式下的持仓方向
            trend_cfg: 趋势跟踪仓位配置（trend_position）
            range_cfg: 区间波动仓位配置（range_position）
            manual_direction: 人工方向覆盖 (auto/long/short)
            run_id: 调度轮次ID，用于日志关联
            leverage: 杠杆倍数（0=沿用交易所当前设置）
            signal_algo: 短周期信号算法 'diff'(默认) / 'hybrid'
            entry_price_type: 开仓限价取值 'close'(默认) / 'open'
            exit_price_type: 平仓限价取值 'open'(默认) / 'close'
            verbose_lifecycle: 是否输出挂单生命周期详细日志
            trade_enabled: 是否开启自动交易（False=观察模式：只分析+发邮件提醒，
                不执行任何下单/撤单/平仓，供人工在线过滤信号）

        执行顺序：趋势分析 → 持仓对账 → 长周期反转处理 → 反向持仓风控 →
        趋势仓止盈止损 → 趋势仓调度 → 交易所兜底委托同步 → 区间仓调度。
        """
        rid = f"[{run_id}] " if run_id else ''
        tcfg = trend_cfg or {}
        rcfg = range_cfg or {}
        watch = not trade_enabled
        trend_mode = str(tcfg.get('period_mode', 'dual') or 'dual').lower()
        # 整轮“分析→对账→反转清理→止盈止损→挂单”与人工强平(Flask线程)互斥，
        # 避免强平在调度中途横插造成本地账本与交易所状态不一致
        self._trade_lock.acquire()
        try:
            # 1. 双周期趋势分析（BOLL 参数取自区间波动仓位配置）
            adapter = self._get_strategy_adapter()
            analysis = adapter.analyze(
                inst_id, short_period, long_period,
                boll_period=int(rcfg.get('boll_period', 20) or 20),
                boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
                signal_algo=signal_algo,
                entry_price_type=entry_price_type,
                exit_price_type=exit_price_type)
            if not analysis:
                task_log.warning(f"{rid}{inst_id} | 【分析失败】无法获取数据")
                return {'success': False, 'error': '双周期趋势分析失败', 'link_error': True}

            short_direction = analysis['direction']       # 'long' or 'short'
            # 长周期方向可能为 None（K线不足/计算异常），不能默认看多
            long_direction_raw = analysis.get('long_direction')
            current_price = analysis.get('last_price', 0) or analysis.get('latest_trade_price', 0) or 0
            # 价格兵底：分析未返回有效价格时，拉取最新市场价，避免开仓/通知价格为 0
            if not current_price:
                current_price = self.trade_executor.get_last_price(inst_id)

            # 短周期前序方向（用于信号稳定性确认）
            short_prev_dir = analysis.get('short_prev_direction')        # 上一时段
            short_prev_prev_dir = analysis.get('short_prev_prev_direction')  # 上上时段
            # 长周期上一时段方向（避免震荡期频繁反转）
            long_prev_dir = analysis.get('long_prev_direction')

            # 2. 人工方向锁定（原“人工方向覆盖”，2026-09-01 语义修正）：
            #    锁定只作用于长周期方向，双周期模式短周期三时段保留真实信号，
            #    开仓仍需真实共振窗口（详见 _resolve_directions 文档）
            _d = self._resolve_directions(
                inst_id, manual_direction, trend_mode,
                {'short': short_direction, 'short_prev': short_prev_dir,
                 'short_prev_prev': short_prev_prev_dir,
                 'long_raw': long_direction_raw, 'long_prev': long_prev_dir})
            short_direction = _d['short']
            short_prev_dir = _d['short_prev']
            short_prev_prev_dir = _d['short_prev_prev']
            long_direction = _d['long']
            long_prev_dir = _d['long_prev']
            dir_source = _d['dir_source']
            if _d['locked_dir']:
                task_log.info(
                    f"{rid}{inst_id} | 方向锁定 | {_d['lock_src']}="
                    f"{fmt_dir(_d['locked_dir'])} | "
                    + ('单周期模式方向即持仓方向' if trend_mode == 'single'
                       else f"短周期保留真实信号（现={fmt_dir(short_direction)} "
                            f"上={fmt_dir(short_prev_dir)} "
                            f"上上={fmt_dir(short_prev_prev_dir)}），"
                            f"开仓仍需真实共振窗口"))

            # 长周期方向缺失（分析数据异常）→ 跳过本轮，不做任何交易决策
            if long_direction not in ('long', 'short'):
                task_log.warning(
                    f"{rid}{inst_id} | 【跳过本轮】长周期方向缺失（分析数据异常），"
                    f"暂不交易")
                return {'success': False, 'error': '长周期方向缺失'}

            # 3. 获取持仓（查询失败返回 None → 跳过本轮交易，
            #    避免把“查询失败”误当成“空仓 0”而重复开仓超额）
            pos_by_mode = self._read_positions_by_mode(inst_id)
            if pos_by_mode is None:
                task_log.warning(
                    f"{rid}{inst_id} | 【跳过本轮】持仓查询失败（网络/API），暂不交易，"
                    f"避免误判持仓量导致重复开仓")
                return {'success': False, 'error': '持仓查询失败，跳过本轮', 'link_error': True}

            # 3b. 观察模式切换（trade_enabled=false：只分析+发邮件，对交易所零操作）
            #     进入首轮：撤销全部未成交挂单，避免遗留挂单在不知情时成交
            if watch:
                if inst_id not in self._watch_active:
                    self._watch_active.add(inst_id)
                    n_cancel = (self.pos_mgr.cancel_bucket_orders(
                                    inst_id, BUCKET_TREND, '进入观察模式')
                                + self.pos_mgr.cancel_bucket_orders(
                                    inst_id, BUCKET_RANGE, '进入观察模式'))
                    task_log.warning(
                        f"{rid}{inst_id} | 【观察模式】已开启 | 撤销未成交挂单{n_cancel}笔，"
                        f"期间只分析提醒不交易")
                    try:
                        self.message_notifier.send_watch_mode_alert(
                            inst_id, 'watch_enter', '观察模式已开启',
                            detail_rows=[('撤销挂单', f'{n_cancel}笔'),
                                         ('持仓处理', '保持不动，已挂的交易所兜底委托原样保留')],
                            hint='观察模式下系统只分析与邮件提醒，不执行任何下单/撤单/平仓；'
                                 '重新开启自动交易后恢复正常调度。')
                    except Exception as e:
                        task_log.warning(f"{rid}{inst_id} | 观察模式开启邮件发送失败: {e}")
            elif inst_id in self._watch_active:
                self._watch_active.discard(inst_id)
                for ek in [k for k in self._watch_edges if k.startswith(inst_id + '|')]:
                    self._watch_edges.pop(ek, None)
                self._watch_rev_notified.pop(inst_id, None)
                self._watch_single_prev.pop(inst_id, None)
                task_log.info(
                    f"{rid}{inst_id} | 【观察模式】已关闭，恢复自动交易"
                    f"（若期间发生长周期反转，本轮将自动补执行反转清理）")

            # 4. 本轮日志上下文 + 本地账本与交易所持仓对账
            #    净持仓模式下两个仓位共享同一模式的净头寸，本地账本是归属的唯一依据
            self.pos_mgr.set_context(run_id, verbose_lifecycle)
            # 对账前账本快照：供 reconcile 后检测交易所侧手动平仓（系统外持仓减少）
            pre_books = {b: dict(self.pos_mgr.get_book(inst_id, b)['held'])
                         for b in (BUCKET_TREND, BUCKET_RANGE)}
            # 4a. 兜底委托触发检测：断网/停机期间交易所侧止损止盈可能已触发，
            #     必须先定向从趋势仓账本扣减，否则 reconcile 会按比例摊到两个篮子
            for ev in self.pos_mgr.poll_algo_triggers(inst_id, BUCKET_TREND):
                task_log.info(f"{rid}{inst_id} | 【兜底委托】{ev}")
            # 4b. 成交入账前置：必须在 reconcile 之前 —— 上一轮限价平仓成交若未先入账，
            #     reconcile 的“账本>真实”缩减分支会把它误判为系统外平仓（手动平仓），
            #     错误触发人工平仓冷却；入账后 reconcile 的缩减即纯“系统外”变化。
            #     同时使刚成交的开仓单本轮就能进入止盈止损检查，而不是等到下一轮(60s+)
            #     挂单 TTL：存续超过 order_ttl_periods 个短周期（默认4，15m→60分钟）的
            #     限价单主动撤销后同轮重挂；趋势仓窗口撤销通常先于 TTL，TTL 主要约束
            #     区间仓持久挂单，防止漏撤/崩溃残留时订单在交易所侧永久存续。
            #     撤单不动账本，不影响后续手动平仓检测的快照比对。
            _ttl_periods = int((self._load_config().get('global_settings', {}) or {})
                               .get('order_ttl_periods', 4) or 0)
            ttl_seconds = (_ttl_periods * self._period_seconds(short_period)
                           if _ttl_periods > 0 else 0)
            early_fills = self.pos_mgr.poll_fills(inst_id, ttl_seconds=ttl_seconds)\
                .get('fills') or []
            self.pos_mgr.reconcile(inst_id, pos_by_mode['cross'], pos_by_mode['isolated'],
                                   price=current_price)
            # 4b-2. 账本大幅背离告警（P2-a）：紧接动作前对账排空背离事件（本轮任何程序
            #       平仓之前，缩减≈强平/爆仓/账户被人工平仓、吸收≈人工加仓），及时叫醒并留痕。
            #       平仓动作后 _refresh_positions 的再对账属程序自身行为，已在 set_context 复位，
            #       不会重复计入。fail-safe，异常不影响主流程。
            self._handle_ledger_divergence(inst_id, run_id)
            # 4c. 手动平仓检测：对账后账本缩减（系统外持仓减少）= 交易所侧手动平仓/强平，
            #     复用人工强平冷却禁止立即重新开仓（检测点在本轮任何程序平仓动作之前，
            #     程序自身平仓均已即时入账，不会误判）
            self._detect_external_close(inst_id, pre_books, run_id)

            # 5. 方向记录：先只计算是否反转；记录的更新推迟到步骤6反转清理结束
            #    之后 —— 清理失败保留旧方向，下轮 long_dir_changed 重新成立自动重试
            prev_dirs = self.last_directions.get(inst_id, {})
            prev_long = prev_dirs.get('long')
            long_dir_changed = bool(prev_long and prev_long != long_direction)

            cfg_all = self._load_config()
            _rc = cfg_all.get('global_settings', {}).get('risk_control', {})
            max_pos_cap = float(_rc.get('max_position_per_currency', 0) or 0)
            max_total_cap = float(_rc.get('max_total_position', 0) or 0)
            # USD 口径单币种上限：按本轮现价折算成张数后与张数口径取更严者。
            # 折算向下取整到 lotSz 步长（宁严勿松）；规格/价格缺失时无法折算，
            # 仅保留张数口径并告警，避免风控静默失效。
            # 注：风控上限按纯面值口径折算（leverage=1），不随杠杆缩放，
            # 保证同一上限在不同杠杆设置下拦截力度一致
            max_pos_usd = float(_rc.get('max_position_per_currency_usd', 0) or 0)
            if max_pos_usd > 0:
                cap_c = self.spec_cache.usd_to_contracts(
                    inst_id, max_pos_usd, current_price,
                    leverage=1, enforce_min_sz=False)
                if cap_c > 0:
                    max_pos_cap = cap_c if max_pos_cap <= 0 else min(max_pos_cap, cap_c)
                else:
                    task_log.warning(
                        f"{rid}{inst_id} | 【风控】单币种USD上限{max_pos_usd}无法折算张数"
                        f"（规格/价格缺失），本轮仅按张数口径{max_pos_cap}拦截")

            ctx = {
                'rid': rid,
                'run_id': run_id,
                'analysis': analysis,
                'short_period': short_period,
                'long_period': long_period,
                'short_dir': short_direction,
                'long_dir': long_direction,
                'short_prev': short_prev_dir,
                'short_prev_prev': short_prev_prev_dir,
                'price': current_price,
                'cross_pos': pos_by_mode['cross'],
                'isolated_pos': pos_by_mode['isolated'],
                'leverage': leverage,
                'entry_price_type': entry_price_type or 'close',
                'exit_price_type': exit_price_type or 'open',
                'trend_allow_entry': True,
                'range_allow_entry': True,
                # 方向来源（配置锁定/页面锁定/信号驱动）：随挂单原因写入
                # trade_operations.log，便于事后区分人工锁定与信号驱动的开平仓
                'dir_source': dir_source,
                'max_position': max_pos_cap,
                'max_total_position': max_total_cap,
            }

            # 步骤4b检出的成交按篮子补发邮件通知
            if early_fills:
                self._notify_fills(inst_id, '趋势跟踪', [
                    f for f in early_fills if f.get('bucket') == BUCKET_TREND], ctx)
                self._notify_fills(inst_id, '区间波动', [
                    f for f in early_fills if f.get('bucket') == BUCKET_RANGE], ctx)

            # 5a. 插针保护：与上一轮现价环比，命中即设暂停到期（并发改配置热生效）。
            #     本轮命中会在下面 5d 闸门即时拦截开仓；开关默认关闭，未开启零副作用。
            self._note_spike_guard(inst_id, current_price, run_id)

            # 5b. 人工强平冷却：冷却期内两个仓位都禁止开新仓（平仓/撤单照常）
            pause_remaining = self._manual_pause_remaining(inst_id)
            if pause_remaining > 0:
                ctx['trend_allow_entry'] = False
                ctx['range_allow_entry'] = False
                # 冷却期内每轮都会走到这里：只在进入冷却后首轮提示，避免刷屏
                if inst_id not in self._pause_logged:
                    self._pause_logged.add(inst_id)
                    task_log.info(
                        f"{rid}{inst_id} | 【人工强平冷却】剩余{pause_remaining / 60:.0f}分钟，"
                        f"期间禁止开新仓")

            # 5c. 止盈/止损冷却：全平成功后 N 个短周期内两仓禁止开新仓，
            #     防止止盈平仓后立即在极端价位追价重开。
            #     观察模式对交易所零操作无需拦截；结构化流水每轮记录剩余周期数，
            #     task_log 仅进入冷却后首轮提示一次（防刷屏）
            if not watch:
                tp_cd_remaining = self._tp_cooldown_remaining(inst_id)
                if tp_cd_remaining > 0:
                    ctx['trend_allow_entry'] = False
                    ctx['range_allow_entry'] = False
                    bar_secs = max(1, self._period_seconds(short_period))
                    cd_left = int((tp_cd_remaining + bar_secs - 1) // bar_secs)
                    ctx['tp_cooldown_remaining'] = cd_left
                    trade_log.info(
                        f"{rid}止盈止损冷却 | 拦截开仓 | {inst_id} | 趋势仓/区间仓均禁止 | "
                        f"tp_cooldown_remaining={cd_left} (短周期{short_period})")
                    if inst_id not in self._tp_cd_logged:
                        self._tp_cd_logged.add(inst_id)
                        task_log.info(
                            f"{rid}{inst_id} | 【止盈止损冷却】剩余约{cd_left}个短周期"
                            f"({tp_cd_remaining / 60:.0f}分钟)，期间禁止开新仓")

            # 5d. 插针临时禁开仓：spike_guard 判定插针后暂停期内，两个仓位禁止开新仓
            #     （平仓/撤单照常）。观察模式对交易所零操作无需拦截；每轮写结构化流水，
            #     task_log 仅进入暂停后首轮提示一次（防刷屏）。
            if not watch:
                spike_remaining = self._spike_pause_remaining(inst_id)
                if spike_remaining > 0:
                    ctx['trend_allow_entry'] = False
                    ctx['range_allow_entry'] = False
                    ctx['spike_pause_remaining'] = int(spike_remaining)
                    trade_log.info(
                        f"{rid}插针保护 | 拦截开仓 | {inst_id} | 趋势仓/区间仓均禁止 | "
                        f"spike_pause_remaining={spike_remaining / 60:.1f}分钟")
                    if inst_id not in self._spike_logged:
                        self._spike_logged.add(inst_id)
                        task_log.warning(
                            f"{rid}{inst_id} | 【插针保护】暂停自动开新仓中，"
                            f"剩余{spike_remaining / 60:.1f}分钟")

            # 6. 长周期方向反转处理
            #    区间仓：旧方向持仓直接强平（未成交挂单由 process_range 撤单换向）
            #    趋势仓：只发提醒邮件；仅单向(dual)模式且处于东八区睡眠时段才强平
            #    强平入口统一走 _close_bucket_smart：启用智能减仓时，整仓净已实现
            #    盈亏(realizedPnl)为亏损则只减仓至目标保证金残留仓，转正才全平
            #    只有清理全部成功才更新方向记录；失败保留旧方向下轮自动重试
            if long_dir_changed and watch:
                # 观察模式：不执行反转清理、不更新方向记录（冻结），重新开启交易后
                # long_dir_changed 重新成立自动补执行；提醒邮件按新方向上升沿去重
                if self._watch_rev_notified.get(inst_id) != long_direction:
                    self._watch_rev_notified[inst_id] = long_direction
                    task_log.warning(
                        f"{rid}{inst_id} | 【信号反转·观察】长周期 "
                        f"{fmt_dir(prev_long)}→{fmt_dir(long_direction)} | 观察模式未执行清理")
                    try:
                        self.message_notifier.send_watch_mode_alert(
                            inst_id, 'reversal', '长周期方向反转',
                            detail_rows=[('方向变化',
                                          f'{fmt_dir(prev_long)}→{fmt_dir(long_direction)}'),
                                         ('当前价格', f'{current_price:.6g}')],
                            hint='观察模式下未执行反转清理（旧方向持仓/挂单保持原样），'
                                 '重新开启自动交易后系统将自动补执行。')
                    except Exception as e:
                        task_log.warning(f"{rid}{inst_id} | 观察模式反转邮件发送失败: {e}")
            elif long_dir_changed:
                reason = f"长周期反转({prev_long}→{long_direction})"
                closed_range = False
                forced_trend = False
                cleanup_ok = True
                # 门槛取粉尘阈值(0.01张)而非硬编码 0.05：低于 minSz 的旧方向持仓也必须
                # 进清理入口（内部会粉尘清零账本），否则它会永久留在账本上阻断新方向开仓
                if self.pos_mgr.get_position(
                        inst_id, BUCKET_RANGE, prev_long)[0] >= self.POS_DUST:
                    closed_range = self._close_bucket_smart(
                        inst_id, BUCKET_RANGE, prev_long, reason, run_id,
                        short_period, long_period, current_price,
                        leverage=leverage, scene='reversal')
                    cleanup_ok = cleanup_ok and closed_range
                if (trend_mode == 'dual'
                        and self._in_night_window(cfg_all)
                        and self.pos_mgr.get_position(
                            inst_id, BUCKET_TREND, prev_long)[0] >= self.POS_DUST):
                    forced_trend = self._close_bucket_smart(
                        inst_id, BUCKET_TREND, prev_long, reason + '睡眠时段强平',
                        run_id, short_period, long_period, current_price,
                        leverage=leverage, scene='night_force')
                    if forced_trend:
                        self.tp_engine.clear_state(inst_id, prev_long == 'long')
                    cleanup_ok = cleanup_ok and forced_trend
                self._notify_long_reversal(inst_id, ctx, prev_long, long_direction,
                                           forced_trend, closed_range)
                if closed_range or forced_trend:
                    self._refresh_positions(inst_id, ctx)
                if cleanup_ok:
                    self.last_directions[inst_id] = {
                        'short': short_direction, 'long': long_direction}
                else:
                    # 保留旧方向记录 → 下轮 long_dir_changed 重新成立，自动重试清理；
                    # 本轮禁止开新仓，避免新旧方向仓位并存
                    self.last_directions[inst_id] = {
                        'short': short_direction, 'long': prev_long}
                    ctx['trend_allow_entry'] = False
                    ctx['range_allow_entry'] = False
                    task_log.error(
                        f"{rid}{inst_id} | 【信号反转·清理失败】保留旧方向记录"
                        f"({fmt_dir(prev_long)})下轮自动重试，本轮禁止开新仓")
            else:
                self.last_directions[inst_id] = {
                    'short': short_direction, 'long': long_direction}

            # 7. 反向持仓风控（账本内程序旧方向残留仓 + 账本外人工反向单）：
            #    仅“趋势双周期单向 + 区间单向”时开启。
            #    趋势仓单周期模式本身就是双向持仓，反向持仓属正常状态，必须关闭风控
            if trend_mode == 'single' and tcfg.get('enabled', True):
                guard_text = '趋势仓为双向模式，风控不适用'
                if inst_id in self._reverse_guard:
                    self._reverse_guard.pop(inst_id, None)
                    self._save_reverse_guard()
                    task_log.info(
                        f"{rid}{inst_id} | 反向持仓风控 | 趋势仓为双向模式，已解除预警")
            else:
                guard_res = self._check_reverse_position_guard(
                    inst_id, long_direction, ctx['cross_pos'], ctx['isolated_pos'],
                    short_period, long_period, current_price, long_dir_changed, run_id,
                    observe_only=watch, leverage=leverage)
                guard_text = guard_res.get('text') or '无反向持仓'
                if guard_res.get('closed'):
                    self._refresh_positions(inst_id, ctx)

            # 8. 止盈止损检查（只针对趋势跟踪仓位，区间仓自带对侧边界止盈）
            #    全平触发 → 本轮趋势仓不开新仓，但区间仓照常运行
            tp_sl = self._check_tp_sl(
                inst_id, current_price, analysis,
                tcfg.get('take_profit') or {}, tcfg.get('stop_loss') or {},
                run_id, short_period, long_period, observe_only=watch)
            if tp_sl.get('triggered'):
                ctx['trend_allow_entry'] = False
                self._refresh_positions(inst_id, ctx)
            if tp_sl.get('cooldown_started'):
                # 本轮刚启动冷却：立即覆盖本轮剩余步骤。趋势仓已由 triggered 拦截，
                # 区间仓不在 triggered 拦截范围内（止盈只针对趋势仓），此处补上，
                # 否则同一轮区间仓仍会在 BOLL 边界挂出开仓单；后续轮由步骤5c接管
                ctx['trend_allow_entry'] = False
                ctx['range_allow_entry'] = False

            # 9. 两个相互独立的仓位依次调度（观察模式：只发事件提醒邮件，不下单）
            if watch:
                trend_res = self._watch_trend_alerts(inst_id, tcfg, ctx)
                range_res = self._watch_range_alerts(inst_id, rcfg, ctx)
            else:
                trend_res = self._run_trend_position(inst_id, tcfg, ctx)
                self._sync_trend_algo(inst_id, tcfg, ctx)
                range_res = self._run_range_position(inst_id, rcfg, ctx)

            # 9b. 本轮下单失败分级处理（P0-1 连续被拒升级 / P0-3 结果未知孤儿单核实）：
            #     必须在挂单阶段结束后、心跳输出前排空；fail-safe，异常不影响主流程
            self._handle_place_failures(inst_id, run_id)

            # 10. 本轮单行心跳：方向 + 持仓 + 动作，一行看清执行状态
            #     事件细节（挂单/成交/撤单/风控拦截等）已按场景模板单独输出
            hb = (("[观察] " if watch else '')
                  + f"短={fmt_dir(short_direction)}({short_period}) "
                  f"长={fmt_dir(long_direction)}({long_period})"
                  + (' [长周期本轮反转]' if long_dir_changed else '')
                  + f" | 趋势仓[{self._fmt_book(inst_id, BUCKET_TREND, current_price)}]"
                  + f" 区间仓[{self._fmt_book(inst_id, BUCKET_RANGE, current_price)}]"
                  + f" | 净持仓:全仓{ctx['cross_pos']:.1f}张/逐仓{ctx['isolated_pos']:.1f}张")
            if tp_sl.get('details'):
                hb += f" | 止盈止损:{'；'.join(tp_sl['details'])}"
            if guard_text and guard_text not in ('无反向持仓', '风控未启用',
                                                 '趋势仓为双向模式，风控不适用'):
                hb += f" | 风控:{guard_text}"
            all_acts = ((trend_res.get('actions') or [])
                        + (range_res.get('actions') or []))
            hb += f" | {'动作:' + '、'.join(all_acts) if all_acts else '无动作'}"
            task_log.info(f"{rid}{inst_id} | 心跳 | {hb}")

            return {
                'success': True,
                'inst_id': inst_id,
                'watch_mode': watch,
                'short_direction': short_direction,
                'long_direction': long_direction,
                'short_prev_direction': short_prev_dir,
                'short_prev_prev_direction': short_prev_prev_dir,
                'long_prev_direction': long_prev_dir,
                'cross_position': ctx['cross_pos'],
                'isolated_position': ctx['isolated_pos'],
                'tp_sl_triggered': bool(tp_sl.get('triggered')),
                'tp_sl_reason': tp_sl.get('reason', ''),
                'tp_cooldown_remaining': ctx.get('tp_cooldown_remaining', 0),
                'trend_actions': trend_res.get('actions') or [],
                'range_actions': range_res.get('actions') or [],
                'trade_executed': bool((trend_res.get('fills') or [])
                                       or (range_res.get('fills') or [])),
            }

        except Exception as e:
            task_log.error(f"{rid}{inst_id} | 分析交易异常: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            self._trade_lock.release()

    # =================================================================
    # 批量执行与主循环
    # =================================================================

    def get_real_trading_currencies_from_config(self) -> List[Dict]:
        """从配置文件获取交易币种和参数（每次调用重新读取）"""
        config = self._load_config()
        gs = config.get('global_settings', {})

        # 检查全局启用状态
        if not gs.get('enabled', True):
            task_log.info("策略已禁用，跳过执行")
            return []

        # 检查紧急停止
        rc = gs.get('risk_control', {})
        if rc.get('emergency_stop', False):
            task_log.warning("⚠️ 紧急停止已启用，跳过执行")
            return []

        items = []
        for c in config.get('currencies', []):
            inst_id = c.get('instId')
            if not inst_id:
                continue
            items.append({
                'inst_id': inst_id,
                'short_period': c.get('short_period', '1m'),
                'long_period': c.get('long_period', '15m'),
                'manual_direction': c.get('manual_direction', 'auto'),
                'signal_algo': c.get('signal_algo', 'diff'),
                'entry_price_type': c.get('entry_price_type', 'close'),
                'exit_price_type': c.get('exit_price_type', 'open'),
                'leverage': int(c.get('leverage', 0) or 0),
                'verbose_lifecycle': bool(c.get('verbose_lifecycle', True)),
                # 是否开启自动交易（缺失视为开启；false=观察模式只发邮件不交易）
                'trade_enabled': bool(c.get('trade_enabled', True)),
                # 两个相互独立的仓位配置（各自独立张数，不按本金比例）
                'trend_position': c.get('trend_position', {}) or {},
                'range_position': c.get('range_position', {}) or {},
            })

        return items

    def _note_cycle_result(self, inst_id: str, result: Dict):
        """记录单币种本轮执行结果：连续失败达阈值升级为告警邮件

        分析失败/持仓查询失败每轮只记 warning 就跳过，若 OKX API 或网络持续故障，
        系统会“安静地”停止交易而持仓继续裸露；连续 N 轮失败后发一封告警邮件并
        重置计数（故障持续则每 N 轮再提醒一次，另受邮件冷却约束不会刷屏）。
        """
        if not inst_id:
            return
        if result and result.get('success'):
            if self._consec_fail.pop(inst_id, None):
                task_log.info(f"{inst_id} | 执行恢复正常，连续失败计数已清零")
            return
        threshold = int((self._load_config().get('global_settings', {})
                         .get('consecutive_failure_alert_rounds', 5)) or 0)
        if threshold <= 0:
            return
        cnt = self._consec_fail.get(inst_id, 0) + 1
        self._consec_fail[inst_id] = cnt
        if cnt < threshold:
            return
        err = (result or {}).get('error') or '未知错误'
        task_log.error(
            f"{inst_id} | 连续{cnt}轮执行失败（最近错误：{err}），升级发送告警邮件")
        try:
            self.message_notifier.send_system_alert(
                symbol=inst_id,
                title=f'连续{cnt}轮执行失败',
                detail=(f'实盘调度器对 {inst_id} 已连续 {cnt} 轮执行失败，'
                        f'最近一次错误：{err}。'
                        f'期间未能正常分析/交易，若当前有持仓仅剩交易所侧兜底委托保护，'
                        f'请尽快检查网络/OKX API/日志。'))
        except Exception as e:
            task_log.warning(f"{inst_id} | 告警邮件发送失败: {e}")
        # 发送后重置：故障持续则每 N 轮再告警一次
        self._consec_fail[inst_id] = 0

    def run_real_trading_batch(self):
        """批量执行实盘交易"""
        run_id = generate_run_id()
        start_time = time.time()
        try:
            self._run_count += 1
            currency_list = self.get_real_trading_currencies_from_config()
            if not currency_list:
                task_log.info(
                    f"[{run_id}] ══ 第{self._run_count}轮跳过 | 无币种或策略已禁用 ══")
                return
            task_log.info(
                f"[{run_id}] ══ 第{self._run_count}轮开始 | {len(currency_list)}币种 ══")

            ok_cnt = 0
            link_fail_cnt = 0
            for item in currency_list:
                if self.stop_event.is_set():
                    break
                try:
                    inst_id = item['inst_id']
                    manual_dir = item.get('manual_direction', 'auto')

                    # 更新人工方向缓存
                    if manual_dir in ['long', 'short']:
                        self.manual_direction_config[inst_id] = manual_dir
                    elif inst_id in self.manual_direction_config:
                        del self.manual_direction_config[inst_id]

                    res = self.analyze_and_trade_real(
                        inst_id=inst_id,
                        short_period=item['short_period'],
                        long_period=item['long_period'],
                        trend_cfg=item.get('trend_position'),
                        range_cfg=item.get('range_position'),
                        manual_direction=manual_dir,
                        run_id=run_id,
                        leverage=item.get('leverage', 0),
                        signal_algo=item.get('signal_algo'),
                        entry_price_type=item.get('entry_price_type'),
                        exit_price_type=item.get('exit_price_type'),
                        verbose_lifecycle=item.get('verbose_lifecycle', True),
                        trade_enabled=item.get('trade_enabled', True),
                    )
                    self._note_cycle_result(inst_id, res)
                    if res and res.get('success'):
                        ok_cnt += 1
                    elif res and res.get('link_error'):
                        link_fail_cnt += 1
                except Exception as e:
                    task_log.error(f"[{run_id}] {item.get('inst_id')} | 【执行异常】{e}")
                    self._note_cycle_result(item.get('inst_id'),
                                            {'success': False, 'error': str(e)})

            elapsed = round(time.time() - start_time, 1)
            # 本轮全部币种处理完毕后才写盘：确保磁盘方向 = “已完整执行”的方向；
            # 若中途崩溃/断网，重启后仍按旧方向比对，能重新触发长周期反转清理。
            self._save_directions()
            # P2-c 持久化降级 / P2-d 链路健康聚合：每轮一次的全局旁路检查，读全局状态
            # + latch 去重，绝不新增 OKX 调用、绝不影响交易主流程。
            self._check_persistence_health(run_id)
            self._note_link_health(run_id, len(currency_list), ok_cnt, link_fail_cnt)
            task_log.info(
                f"[{run_id}] ══ 本轮结束 | 成功{ok_cnt}/{len(currency_list)} | "
                f"耗时{elapsed}s ══")

        except Exception as e:
            task_log.error(f"[{run_id}] ══ 轮次异常 | {e} ══")
            return

    def start_real_scheduler(self):
        """主循环"""
        if self.running:
            return

        self.running = True
        self.stop_event.clear()

        while self.running and not self.stop_event.is_set():
            try:
                current_time = time.time()

                # 每轮重新读取配置获取执行间隔
                config = self._load_config()
                interval = int(config.get('global_settings', {}).get(
                    'execution_interval_seconds', self._default_interval
                ))
                # 邮件冷却时长同随配置热更新
                self._sync_email_cooldown(config)

                if (current_time - self.last_execution.get('batch', 0)) >= interval:
                    self.last_execution['batch'] = current_time
                    self.run_real_trading_batch()
                    if self.stop_event.is_set():
                        break

                if not self.stop_event.is_set():
                    self.stop_event.wait(10)

            except KeyboardInterrupt:
                break
            except Exception as e:
                # 主循环异常必须留痕，否则持续故障（如配置损坏）只会静默空转
                task_log.error(f"[主循环] 异常: {type(e).__name__}: {e}")
                if not self.stop_event.is_set():
                    self.stop_event.wait(30)

        self.running = False

    def stop(self):
        if self.running:
            self.running = False
            self.stop_event.set()

    # =================================================================
    # 人工干预
    # =================================================================

    def set_manual_override(self, inst_id: str, direction: str):
        if direction is None or direction == 'auto':
            self.manual_override_cache.pop(inst_id, None)
            task_log.info(f"[人工干预] 已取消 {inst_id} 的方向干预")
        else:
            self.manual_override_cache[inst_id] = direction
            task_log.info(f"[人工干预] 设置 {inst_id} 方向为: {direction}")

    def get_manual_override(self, inst_id: str) -> Optional[str]:
        return self.manual_override_cache.get(inst_id)

    def clear_all_overrides(self):
        count = len(self.manual_override_cache)
        self.manual_override_cache.clear()
        task_log.info(f"[人工干预] 已清除所有 {count} 个干预设置")


def main():
    trader = TrendRangeTrader(manual_direction_config={})
    trader.start_real_scheduler()


if __name__ == "__main__":
    main()
