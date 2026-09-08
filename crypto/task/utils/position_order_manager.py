#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双仓位限价挂单管理器
=====================

为定时任务提供两个**互相独立**的仓位账本与限价挂单生命周期管理：

┌──────────────┬────────────────────────────┬──────────────────────────────┐
│ 篮子         │ 趋势跟踪 trend             │ 区间波动 range               │
├──────────────┼────────────────────────────┼──────────────────────────────┤
│ 信号来源     │ 趋势策略（Pro3）           │ BOLL 边界                    │
│ 方向         │ single=双向 / dual=单向    │ 单向（长周期方向）           │
│ 开仓价       │ 反转bar 的 open/close      │ 长多→下轨 / 长空→上轨        │
│ 平仓价       │ 反转bar 的 open/close      │ 对侧边界（长多→上轨）        │
│ 追价         │ 不追价（宁愿错过）         │ 随边界移动 amend 追价        │
│ 循环         │ 一次信号一次挂单           │ 平仓成交→立即重挂开仓（循环）│
└──────────────┴────────────────────────────┴──────────────────────────────┘

交易所层面隔离（本地记账方案）：
- 账户为净持仓模式，多头统一走全仓(cross)、空头统一走逐仓(isolated)。
- 两篮子方向相同时共享同一个净头寸，因此各自持仓张数(held)与**加权入场均价
  (avg_px)** 全部由本地账本记录 —— 交易所返回的 avgPx 是两篮子混合均价，
  不能用于单个篮子的盈亏/止盈计算。
- 平仓(reduce-only)挂单总量按「真实同向持仓 − 其他篮子已挂平仓量」封顶，
  避免两篮子同时挂单导致计划平仓量超过净持仓。
- 每轮开始调用 reconcile() 用真实持仓给账本封顶，防止手动平仓/强平/爆仓
  造成账本长期虚高。

状态持久化：MySQL pos_book/pos_slot/pos_algo/pos_lev 四表（迁移批次5，
原 position_order_state.json；下单成功立即落库，缩小孤儿单窗口）

作者：AI Assistant
创建时间：2026-07-28
"""

import time
import threading
from typing import Dict, Optional

try:
    from .logger import get_task_logger, get_trade_logger
    from .trade_journal import record_fill as journal_fill
except ImportError:
    from logger import get_task_logger, get_trade_logger
    from trade_journal import record_fill as journal_fill

# 状态持久化已迁移 MySQL（pos_* 四表）：DB 不可用时仅告警不中断交易，
# 与 JSON 版“文件读写失败仅 warning”的容错行为一致
try:
    from crypto.database import session_scope
    from crypto import trader_state_repo as state_repo
except ImportError:
    session_scope = None
    state_repo = None

task_log = get_task_logger()
trade_log = get_trade_logger()


# 篮子标识
BUCKET_TREND = 'trend'
BUCKET_RANGE = 'range'
BUCKETS = (BUCKET_TREND, BUCKET_RANGE)

# 每个篮子的槽位（一个开仓槽 + 一个平仓槽）
SLOT_ENTRY = 'entry'
SLOT_EXIT = 'exit'
SLOTS = (SLOT_ENTRY, SLOT_EXIT)

# 槽位状态机：IDLE(未挂) → PENDING(已挂未成交) → FILLED(已成交) / EXPIRED(已撤销)
ST_IDLE = 'IDLE'
ST_PENDING = 'PENDING'
ST_FILLED = 'FILLED'
ST_EXPIRED = 'EXPIRED'

# 中文标签（生命周期日志可读性）
BUCKET_LABEL = {BUCKET_TREND: '趋势跟踪', BUCKET_RANGE: '区间波动'}
SLOT_LABEL = {SLOT_ENTRY: '开仓', SLOT_EXIT: '平仓'}


class DualPositionOrderManager:
    """趋势跟踪 + 区间波动 双仓位挂单与账本管理器"""

    # PENDING 槽位保护窗口（秒）：刚下的单可能尚未在交易所侧完成传播，
    # 该窗口内“查无此单”按瞬时故障处理；超窗才认定是跨账号残留/已归档，
    # 可安全复位（账本状态表跨账号共享，切换账号后旧 ord_id 必然查无此单）
    NOT_FOUND_GRACE_SEC = 120.0

    def __init__(self, trade_executor, state_file: str = None, spec_cache=None):
        """
        Args:
            trade_executor: TradeExecutor 实例（复用下单/查单/撤单/改单能力）
            state_file: 已废弃（保留签名兼容旧调用方）——状态已迁移 MySQL
            spec_cache: InstrumentSpecCache 实例，提供各合约 lotSz/minSz；
                缺省时全部按 0.1 张步长兜底（存量行为，仅供无规格的测试场景）
        """
        self.executor = trade_executor
        self.spec_cache = spec_cache
        self._lock = threading.RLock()
        self.state_file = state_file  # 仅兼容保留，不再读写文件
        self.state = self._load_state()
        self._run_id = ''
        self._verbose = True
        # 杠杆回核时间戳（内存态）：{inst_id:mode -> 上次回交易所核对的秒级时间}。
        # lev_set 缓存只能证明“本进程设过”，人工在 App 改杠杆、交易所侧重置
        # 或账户杠杆模式变更都会使缓存失真，盲信缓存会一直沿用错误杠杆
        # 开仓（2026-09-01 排查结论），故需周期性回交易所核对
        self._lev_verified = {}

    # =================================================================
    # 下单量步长（按合约规格，绝不硬编码）
    # =================================================================

    # 无规格来源时的兜底步长（张）与“视为无持仓”的粉尘阈值（张）
    FALLBACK_LOT_SZ = 0.1
    POS_DUST = 0.01

    def _steps(self, inst_id: str) -> tuple:
        """该合约的 (lotSz 步长, minSz 最小下单量)。

        各合约步长差异极大（XRP=0.01 / NEAR=0.1 / POL=1），历史实现把 0.1 张
        当成通用最小步长，导致：XRP 折算出的 0.03 张被 round(…,1) 抹成 0，
        区间仓永远不挂单（表现为“仓位B纹丝不动”）；POL 配置的 0.5 张低于其
        minSz=1，每轮下单必被交易所拒（2026-09-01 排查结论）。
        """
        if self.spec_cache is not None:
            try:
                return self.spec_cache.steps(inst_id)
            except Exception as e:
                task_log.warning(f"[双仓位] {inst_id} 读取合约步长失败，按兜底0.1张: {e}")
        return self.FALLBACK_LOT_SZ, self.FALLBACK_LOT_SZ

    def _q(self, inst_id: str, amount: float) -> float:
        """下单量向下取整到该合约 lotSz 步长；低于 minSz 返回 0（不可下单）。"""
        amount = float(amount or 0)
        if amount <= 0:
            return 0.0
        if self.spec_cache is not None:
            try:
                return self.spec_cache.quantize(inst_id, amount)
            except Exception:
                pass
        lot, _ = self._steps(inst_id)
        out = round(int(amount / lot + 1e-9) * lot, 4)
        return out if out >= lot - 1e-9 else 0.0

    def _in_pos(self, held: float) -> bool:
        """账本是否算“在场”：≥0.01 张即在场（低于此为对账浮点残渣）。

        阈值取 0.01 而非 minSz —— 把可交易的真实小仓（如 XRP 最小 0.01 张）
        误判为空仓会触发重复开仓，比把不可交易的粉尘当成持仓危险得多；
        粉尘无法下单平仓的死循环由平仓路径的“粉尘清零”出口处理。
        """
        return float(held or 0) + 1e-9 >= self.POS_DUST

    # =================================================================
    # 状态持久化与结构初始化（MySQL：迁移批次5）
    # =================================================================

    def _load_state(self) -> Dict:
        try:
            with session_scope() as session:
                return state_repo.load_position_state(session)
        except Exception as e:
            task_log.warning(f"[双仓位] 加载状态失败: {e}")
        return {}

    def _save(self, inst_id: str = None):
        """立即落库。传 inst_id 时只写该合约（单次 ≤9 行，交易链路常态），
        不传时整树重写（仅迁移/兜底场景）。"""
        try:
            with session_scope() as session:
                if inst_id:
                    state_repo.save_inst_position(
                        session, inst_id, self.state.get(inst_id) or {})
                else:
                    state_repo.save_position_state(session, self.state)
        except Exception as e:
            task_log.warning(f"[双仓位] 保存状态失败: {e}")

    @staticmethod
    def _new_slot() -> Dict:
        return {'state': ST_IDLE, 'ord_id': None, 'price': 0.0, 'amount': 0.0,
                'placed_ts': 0.0, 'acc_filled': 0.0, 'dir': None}

    def _new_bucket(self) -> Dict:
        """篮子账本。held/avg_px 按方向分开记账 —— 单周期双向模式下方向翻转期间
        旧向持仓（平仓单未成交）与新向持仓可能同时存在，单一方向字段无法表达。"""
        return {
            'held': {'long': 0.0, 'short': 0.0},    # 各方向持仓张数（本地账本）
            'avg_px': {'long': 0.0, 'short': 0.0},  # 各方向加权入场均价（本地账本）
            'slots': {s: self._new_slot() for s in SLOTS},
            'prev_open_confirmed': False,
            'prev_close_confirmed': False,
            'last_desired': None,   # 上一轮期望方向（用于识别新反转信号）
            # 交易所侧兜底委托 {'long': {...}, 'short': {...}}
            'algo': {'long': None, 'short': None},
        }

    def _inst(self, inst_id: str) -> Dict:
        """获取/初始化某合约的状态（兼容旧结构缺字段）"""
        s = self.state.setdefault(inst_id, {})
        for b in BUCKETS:
            if not isinstance(s.get(b), dict):
                s[b] = self._new_bucket()
            bk = s[b]
            for fld in ('held', 'avg_px'):
                if not isinstance(bk.get(fld), dict):
                    bk[fld] = {'long': 0.0, 'short': 0.0}
                for d in ('long', 'short'):
                    bk[fld].setdefault(d, 0.0)
            if not isinstance(bk.get('algo'), dict):
                bk['algo'] = {'long': None, 'short': None}
            for d in ('long', 'short'):
                bk['algo'].setdefault(d, None)
            bk.setdefault('prev_open_confirmed', False)
            bk.setdefault('prev_close_confirmed', False)
            bk.setdefault('last_desired', None)
            if not isinstance(bk.get('slots'), dict):
                bk['slots'] = {sl: self._new_slot() for sl in SLOTS}
            for sl in SLOTS:
                if not isinstance(bk['slots'].get(sl), dict):
                    bk['slots'][sl] = self._new_slot()
                else:
                    for k, v in self._new_slot().items():
                        bk['slots'][sl].setdefault(k, v)
        s.setdefault('lev_set', None)
        return s

    # =================================================================
    # 日志
    # =================================================================

    def set_context(self, run_id: str = '', verbose: bool = True):
        """设置本轮日志上下文（调度轮次ID + 生命周期日志详细开关）"""
        self._run_id = run_id or ''
        self._verbose = bool(verbose)

    def _rid(self) -> str:
        """轮次ID前缀（场景日志统一格式）"""
        return f"[{self._run_id}] " if self._run_id else ''

    def _life(self, msg: str, key: bool = False):
        """挂单生命周期场景日志（key=True 的关键事件不受 verbose 开关约束）。

        msg 统一场景模板：'{inst_id} | 【动作】仓位 | 数量 @ 价格 | 原因：xxx'
        """
        if not (self._verbose or key):
            return
        task_log.info(f"{self._rid()}{msg}")

    @staticmethod
    def _tag(bucket: str, slot: str) -> str:
        return f"{BUCKET_LABEL.get(bucket, bucket)}·{SLOT_LABEL.get(slot, slot)}"

    # =================================================================
    # 对外：账本查询与外部平仓同步
    # =================================================================

    def get_book(self, inst_id: str, bucket: str) -> Dict:
        """读取篮子账本 {'held': {long,short}, 'avg_px': {long,short}}"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            return {'held': {d: round(float(bk['held'].get(d, 0) or 0), 4)
                             for d in ('long', 'short')},
                    'avg_px': {d: float(bk['avg_px'].get(d, 0) or 0)
                               for d in ('long', 'short')}}

    def get_position(self, inst_id: str, bucket: str, direction: str) -> tuple:
        """读取某篮子某方向的 (持仓张数, 加权均价)"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            return (round(float(bk['held'].get(direction, 0) or 0), 4),
                    float(bk['avg_px'].get(direction, 0) or 0))

    def pending_snapshot(self, inst_id: str, bucket: str) -> Dict:
        """槽位状态快照（供日志展示）"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            now = time.time()
            snap = {}
            for sl in SLOTS:
                d = bk['slots'][sl]
                age = int(now - float(d.get('placed_ts', 0) or 0)) if (
                    d.get('state') == ST_PENDING and d.get('placed_ts')) else 0
                snap[sl] = {'state': d.get('state'), 'price': float(d.get('price', 0) or 0),
                            'amount': float(d.get('amount', 0) or 0), 'age_sec': age,
                            'dir': d.get('dir')}
            return snap

    def note_external_close(self, inst_id: str, bucket: str, direction: str,
                            amount: float, reason: str = ''):
        """外部平仓（止盈/止损/强平）后扣减账本持仓，归零则清空该方向均价。"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            before = float(bk['held'].get(direction, 0) or 0)
            bk['held'][direction] = round(
                max(0.0, before - max(0.0, float(amount or 0))), 4)
            if not self._in_pos(bk['held'][direction]):
                bk['held'][direction] = 0.0
                bk['avg_px'][direction] = 0.0
            self._life(
                f"{inst_id} | 【账本扣减】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{'多头' if direction == 'long' else '空头'}持仓 {before}→{bk['held'][direction]}张 | "
                f"原因：外部平仓({reason})", key=True)
            self._save(inst_id)

    def clear_bucket(self, inst_id: str, bucket: str, reason: str = '',
                     direction: str = None) -> int:
        """撤销该篮子挂单 + 清空账本（长周期反转强平 / 手动重置用）。

        direction 为 None 时清空双向账本，否则只清指定方向。返回撤单数。
        """
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            n = 0
            for sl in SLOTS:
                if direction and bk['slots'][sl].get('dir') not in (None, direction):
                    continue
                if self._cancel_slot(inst_id, bucket, sl, reason or '清空篮子'):
                    n += 1
            for d in (('long', 'short') if not direction else (direction,)):
                bk['held'][d] = 0.0
                bk['avg_px'][d] = 0.0
                if self._cancel_algo(inst_id, bucket, d, reason or '清空篮子'):
                    n += 1
            bk['prev_open_confirmed'] = False
            bk['prev_close_confirmed'] = False
            bk['last_desired'] = None
            self._save(inst_id)
            return n

    def cancel_bucket_orders(self, inst_id: str, bucket: str, reason: str = '') -> int:
        """仅撤销该篮子未成交挂单，保留持仓账本。返回撤单数。"""
        with self._lock:
            n = 0
            for sl in SLOTS:
                if self._cancel_slot(inst_id, bucket, sl, reason or '撤销挂单'):
                    n += 1
            self._save(inst_id)
            return n

    # =================================================================
    # 每轮对账：用真实持仓给本地账本封顶
    # =================================================================

    def reconcile(self, inst_id: str, cross_pos: float, isolated_pos: float,
                  price: float = 0.0):
        """按方向把真实持仓与两篮子账本对账（双向拉平）。

        多头真实持仓 = 全仓净持仓(正)，空头真实持仓 = 逐仓净持仓(负)的绝对值。
        - 账本 > 真实：按比例缩减 —— 对应手动平仓、强平、爆仓等系统外减仓；
        - 真实 > 账本：把多出部分**吸收进账本** —— 对应人工同向加仓/入账。
          人工仓位并入策略管理（该加仓加仓、该平仓平仓），而不是单向拒单卡死。
          归属优先级：已持同向仓的篮子（按比例摊） > last_desired 同向的篮子
          （趋势仓优先） > 趋势仓；人工部分入场价未知，用本轮现价近似参与均价。
          price 缺省 0 时保持原均价不动（宁缺勿错）。

        必须在本轮检查成交之前调用（此时账本与传入持仓反映同一时刻状态，
        否则会把本轮新成交误判为账本漂移）。
        """
        with self._lock:
            s = self._inst(inst_id)
            real = {'long': max(0.0, float(cross_pos or 0)),
                    'short': abs(min(0.0, float(isolated_pos or 0)))}
            for d in ('long', 'short'):
                names = [b for b in BUCKETS
                         if float(s[b]['held'].get(d, 0) or 0) > 0]
                total = round(sum(float(s[b]['held'].get(d, 0) or 0) for b in names), 4)
                if total <= 0:
                    continue
                if total - real[d] <= 0.01:
                    continue
                scale = (real[d] / total) if total > 0 else 0.0
                detail = []
                for b in names:
                    old = float(s[b]['held'].get(d, 0) or 0)
                    s[b]['held'][d] = round(old * scale, 4)
                    if not self._in_pos(s[b]['held'][d]):
                        s[b]['held'][d] = 0.0
                        s[b]['avg_px'][d] = 0.0
                    detail.append(f"{BUCKET_LABEL.get(b, b)} {old}→{s[b]['held'][d]}")
                task_log.info(
                    f"[双仓位] {inst_id} 账本对账 | {d} 方向真实持仓{real[d]:.1f}张 < "
                    f"账本合计{total}张（系统外平仓/强平），按比例{scale:.3f}缩减：" +
                    ' '.join(detail))
            # 账本外真实超额（人工同向加仓/入账遗漏）：吸收进账本继续管理，
            # 使后续开平仓调度照常运作（补到目标量/信号平仓都覆盖人工部分）
            for d in ('long', 'short'):
                total = round(sum(float(s[b]['held'].get(d, 0) or 0)
                                  for b in BUCKETS), 4)
                excess = round(real[d] - total, 4)
                if excess <= 0.05:
                    continue
                adds = {}
                held_names = [b for b in BUCKETS
                              if float(s[b]['held'].get(d, 0) or 0) > 0]
                if held_names:
                    for b in held_names:
                        adds[b] = round(
                            excess * float(s[b]['held'].get(d, 0) or 0) / total, 4)
                else:
                    tb = BUCKET_TREND if s[BUCKET_TREND].get('last_desired') == d \
                        else (BUCKET_RANGE
                              if s[BUCKET_RANGE].get('last_desired') == d
                              else BUCKET_TREND)
                    adds[tb] = excess
                detail = []
                for b, add in adds.items():
                    if add <= 0:
                        continue
                    old = float(s[b]['held'].get(d, 0) or 0)
                    new_held = round(old + add, 4)
                    if float(price or 0) > 0:
                        base = float(s[b]['avg_px'].get(d, 0) or 0) or float(price)
                        s[b]['avg_px'][d] = round(
                            (base * old + float(price) * add) / new_held, 8)
                    s[b]['held'][d] = new_held
                    detail.append(f"{BUCKET_LABEL.get(b, b)} {old}→{new_held}")
                task_log.info(
                    f"[双仓位] {inst_id} 账本吸收 | {d} 方向真实持仓{real[d]:.1f}张 > "
                    f"账本合计{total}张（人工加仓/入账），吸收{excess:.1f}张入册：" +
                    ' '.join(detail))
            self._save(inst_id)

    # =================================================================
    # 内部：槽位生命周期
    # =================================================================

    def _reconcile_slot(self, inst_id: str, bucket: str, slot: str,
                        fills: list) -> Optional[str]:
        """检查槽位挂单成交状态并入账。返回动作描述或 None。

        - filled          → 入账（entry 增持并加权均价 / exit 减持），槽位置 FILLED
        - canceled        → 部分成交入账，槽位置 EXPIRED（本周期不补挂）
        - partially_filled→ 记录进度，状态不变（终态统一入账）
        - 查询失败        → 保持 PENDING（不能把查询失败当成撤单/成交）
        """
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id'):
            return None

        status, info = self.executor.probe_order(inst_id, pt['ord_id'])
        if status == 'not_found':
            # 当前账号查无此单（跨账号残留/已归档）：不存在成交尾巴，直接复位，
            # 否则槽位永远 PENDING 卡死后续挂单；刚下单未传播的用保护窗口防误判
            age = time.time() - float(pt.get('placed_ts', 0) or 0)
            if age > self.NOT_FOUND_GRACE_SEC:
                px_c, amt_c = float(pt.get('price', 0) or 0), float(pt.get('amount', 0) or 0)
                bk['slots'][slot] = self._new_slot()
                self._life(
                    f"{inst_id} | 【订单不存在】{self._tag(bucket, slot)} "
                    f"{amt_c}张@{px_c:.6g} 当前账号无此订单（跨账号残留/已归档），"
                    f"槽位已复位", key=True)
                return f'{bucket}.{slot}复位(订单不存在)'
            status = 'error'
        if status == 'error':
            # 持续失败期间按槽位去重：只在首次失败打一行，避免每轮轮询刷屏
            if not pt.get('qfail_logged'):
                pt['qfail_logged'] = True
                self._life(
                    f"{inst_id} | 【查询失败】{self._tag(bucket, slot)} 订单状态查询失败，"
                    f"保持挂单（持续失败期间不再重复记录）", key=True)
            return None
        pt.pop('qfail_logged', None)

        st = info.get('state', '')
        acc = self._filled_size(info)
        fill_px = self._fill_price(info) or float(pt.get('price', 0) or 0)

        if st == 'filled':
            filled = acc or float(pt.get('amount', 0) or 0)
            self._credit(inst_id, bucket, slot, filled, fill_px)
            self._journal_credit(inst_id, bucket, slot, pt, filled, fill_px)
            pt['state'] = ST_FILLED
            pt['acc_filled'] = filled
            _d = pt.get('dir') or 'long'
            _held, _avg = self.get_position(inst_id, bucket, _d)
            trade_log.info(
                f"[双仓位] {inst_id} {BUCKET_LABEL.get(bucket, bucket)}"
                f"{SLOT_LABEL.get(slot, slot)}限价单成交 {filled}张 @ {fill_px:.6g}"
                f"（篮子{'多头' if _d == 'long' else '空头'}持仓={_held}张 均价={_avg:.6g}）")
            _act = (('开多' if _d == 'long' else '开空') if slot == SLOT_ENTRY
                    else ('平多' if _d == 'long' else '平空'))
            self._life(
                f"{inst_id} | 【{_act}·成交】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{filled}张 @ {fill_px:.6g} | 账本持仓={_held}张 均价={_avg:.6g}", key=True)
            if fills is not None:
                fills.append({'bucket': bucket, 'slot': slot, 'amount': filled,
                              'price': fill_px, 'dir': pt.get('dir')})
            # 开仓成交 → 释放平仓槽以便立即挂平仓单；平仓成交 → 释放开仓槽（区间循环）
            other = SLOT_EXIT if slot == SLOT_ENTRY else SLOT_ENTRY
            if bk['slots'][other]['state'] in (ST_FILLED, ST_EXPIRED):
                bk['slots'][other] = self._new_slot()
            return f'{bucket}.{slot}成交'

        if st in ('canceled', 'mmp_canceled'):
            if acc > 0:
                self._credit(inst_id, bucket, slot, acc, fill_px)
                self._journal_credit(inst_id, bucket, slot, pt, acc, fill_px)
            px_c, amt_c = float(pt.get('price', 0) or 0), float(pt.get('amount', 0) or 0)
            pt['state'] = ST_EXPIRED
            pt['ord_id'] = None
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} 挂单被外部撤销({st})"
                + (f"，已成交{acc}张已入账" if acc > 0 else '') + "，本轮不补挂")
            self._life(
                f"{inst_id} | 【撤单·外部】{self._tag(bucket, slot)} 被外部撤销({st}) "
                f"{amt_c}张@{px_c:.6g}" + (f"，已成交{acc}张" if acc > 0 else ''), key=True)
            return f'{bucket}.{slot}外部已撤'

        if st == 'partially_filled' and acc > float(pt.get('acc_filled', 0) or 0):
            pt['acc_filled'] = acc
            remaining = round(max(0.0, float(pt.get('amount', 0) or 0) - acc), 4)
            self._life(
                f"{inst_id} | 【部分成交】{self._tag(bucket, slot)} 累计{acc}张 @ "
                f"{fill_px:.6g} | 剩余{remaining}张", key=True)
        return None

    def _credit(self, inst_id: str, bucket: str, slot: str, amount: float, fill_px: float):
        """成交入账：开仓槽增持并更新加权均价，平仓槽减持（归零清均价）。

        入账方向取自槽位的 dir 字段 —— 双向模式下同一篮子可能同时持有多空。
        """
        if amount <= 0:
            return
        bk = self._inst(inst_id)[bucket]
        d = bk['slots'][slot].get('dir')
        if d not in ('long', 'short'):
            task_log.warning(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} 成交入账缺少方向标识，跳过")
            return
        held = float(bk['held'].get(d, 0) or 0)
        avg = float(bk['avg_px'].get(d, 0) or 0)
        if slot == SLOT_ENTRY:
            new_held = round(held + amount, 4)
            if new_held > 0:
                bk['avg_px'][d] = round(
                    (avg * held + float(fill_px) * amount) / new_held, 8)
            bk['held'][d] = new_held
        else:
            bk['held'][d] = round(max(0.0, held - amount), 4)
            if not self._in_pos(bk['held'][d]):
                bk['held'][d] = 0.0
                bk['avg_px'][d] = 0.0

    def _journal_credit(self, inst_id: str, bucket: str, slot: str,
                        pt: Dict, amount: float, fill_px: float):
        """限价成交写入结构化流水（实盘 vs 策略对比引擎的数据源）"""
        d = pt.get('dir')
        if amount <= 0 or d not in ('long', 'short'):
            return
        journal_fill(
            inst_id, bucket, d,
            'open' if slot == SLOT_ENTRY else 'close',
            fill_px, amount, reason='signal',
            ord_id=pt.get('ord_id'), run_id=self._run_id)

    @staticmethod
    def _filled_size(info: Dict) -> float:
        try:
            return float((info or {}).get('accFillSz', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fill_price(info: Dict) -> float:
        """成交均价（限价单通常等于委托价，可能更优）"""
        try:
            return float((info or {}).get('avgPx', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _cancel_slot(self, inst_id: str, bucket: str, slot: str, reason: str) -> bool:
        """撤销槽位挂单并复位（部分成交先入账）。返回是否发生撤单。

        查询与撤单之间存在竞态：查询后订单可能恰好完全成交（撤单返回
        “已完成无法撤销”）。因此撤单失败或首次查询失败时重查一次终态，
        把漏掉的成交尾巴入账；重查仍不确定则保持 PENDING 留给下一轮
        _reconcile_slot，绝不盲目复位丢账。
        """
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id'):
            bk['slots'][slot] = self._new_slot()
            return False
        px_c, amt_c = float(pt.get('price', 0) or 0), float(pt.get('amount', 0) or 0)
        age = time.time() - float(pt.get('placed_ts', 0) or 0)
        status, info = self.executor.probe_order(inst_id, pt['ord_id'])
        if status == 'not_found':
            if age > self.NOT_FOUND_GRACE_SEC:
                # 当前账号查无此单（跨账号残留/已归档）：无可撤订单也无成交尾巴，
                # 直接复位槽位解除对新方向挂单的阻塞，而不是无限重试撤单
                bk['slots'][slot] = self._new_slot()
                self._life(
                    f"{inst_id} | 【撤单·订单不存在】{self._tag(bucket, slot)} "
                    f"{amt_c}张@{px_c:.6g} | 原因：{reason}，当前账号无此订单，"
                    f"槽位已复位", key=True)
                return True
            status, info = 'error', {}
        ok = self.executor.cancel_normal_order(inst_id, pt['ord_id'])
        if not ok or not info:
            # 撤单失败（可能已完全成交）或首查失败 → 重查一次终态
            st2, info2 = self.executor.probe_order(inst_id, pt['ord_id'])
            if st2 == 'ok' and info2:
                info = info2
            elif st2 == 'not_found' and age > self.NOT_FOUND_GRACE_SEC:
                bk['slots'][slot] = self._new_slot()
                self._life(
                    f"{inst_id} | 【撤单·订单不存在】{self._tag(bucket, slot)} "
                    f"{amt_c}张@{px_c:.6g} | 原因：{reason}，当前账号无此订单，"
                    f"槽位已复位", key=True)
                return True
            elif not info:
                # 两次查询都失败且撤单结果未知 → 保持 PENDING 待下轮确认
                self._life(
                    f"{inst_id} | 【撤单·未确认】{self._tag(bucket, slot)} 原因：{reason}，"
                    f"订单状态查询失败，保持挂单待下轮确认", key=True)
                return False
        if (info or {}).get('state') == 'filled':
            # 撤单前一瞬间已完全成交 → 全量入账置 FILLED（与 _reconcile_slot 一致）
            filled = self._filled_size(info) or amt_c
            fill_px = self._fill_price(info) or px_c
            self._credit(inst_id, bucket, slot, filled, fill_px)
            self._journal_credit(inst_id, bucket, slot, pt, filled, fill_px)
            pt['state'] = ST_FILLED
            pt['acc_filled'] = filled
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} 撤单({reason})时"
                f"发现已完全成交 {filled}张@{fill_px:.6g}，已入账")
            self._life(
                f"{inst_id} | 【成交·撤单竞态】{self._tag(bucket, slot)} 撤单前已完全成交 "
                f"{filled}张 @ {fill_px:.6g}", key=True)
            return False
        partial = self._filled_size(info)
        if partial > 0:
            self._credit(inst_id, bucket, slot, partial,
                         self._fill_price(info) or px_c)
            self._journal_credit(inst_id, bucket, slot, pt, partial,
                                 self._fill_price(info) or px_c)
        trade_log.info(
            f"[双仓位] {inst_id} {self._tag(bucket, slot)} 撤单({reason}) "
            f"{amt_c}张@{px_c:.6g}" + (f"，已成交{partial}张已入账" if partial > 0 else '')
            + ('' if ok else '（撤单失败）'))
        self._life(
            f"{inst_id} | 【撤单】{self._tag(bucket, slot)} {amt_c}张@{px_c:.6g} | "
            f"原因：{reason}" + ('' if ok else ' | 撤单失败'), key=True)
        bk['slots'][slot] = self._new_slot()
        return ok

    def _amend_slot_price(self, inst_id: str, bucket: str, slot: str,
                          new_px: float, min_pct: float) -> Optional[str]:
        """挂单价追随目标价改单（BOLL 边界移动用）。返回动作描述或 None。"""
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id') or new_px <= 0:
            return None
        old_px = float(pt.get('price', 0) or 0)
        new_r = round(new_px, 6)
        if old_px <= 0 or new_r == round(old_px, 6):
            return None
        if min_pct > 0 and abs(new_r - old_px) / old_px < min_pct:
            return None
        if self.executor.amend_order(inst_id, pt['ord_id'], new_price=new_r):
            pt['price'] = new_r
            pct = abs(new_r - old_px) / old_px * 100
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} BOLL边界移动改价 "
                f"{old_px:.6g}→{new_r:.6g}")
            self._life(
                f"{inst_id} | 【改单】{self._tag(bucket, slot)} {old_px:.6g}→{new_r:.6g} | "
                f"原因：BOLL边界移动(幅度{pct:.3f}%)")
            return f'{bucket}.{slot}改价{old_px:.6g}→{new_r:.6g}'
        task_log.warning(
            f"[双仓位] {inst_id} {self._tag(bucket, slot)} 改价失败（保留原价{old_px:.6g}）")
        return None

    def _amend_slot_size(self, inst_id: str, bucket: str, slot: str,
                         new_amt: float, reason: str = '跟随篮子持仓变化',
                         cancel_reason: str = '篮子持仓归零，平仓挂单撤销') -> Optional[str]:
        """挂单量同步（平仓单跟随篮子持仓 / 开仓单跟随配置目标量变化）。
        返回动作描述或 None。new_amt<=0 时撤销挂单（目标量为 0 不应留单）。"""
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id'):
            return None
        cur = float(pt.get('amount', 0) or 0)
        lot, _ = self._steps(inst_id)
        target = self._q(inst_id, new_amt)
        if target <= 0:
            self._cancel_slot(inst_id, bucket, slot, cancel_reason)
            return f'{bucket}.{slot}撤单(目标量为0)'
        # 差值不足一个步长视为无变化；直接比较浮点差会因二进制表示误差把
        # 恰好一步的差值（0.3-0.2=0.09999…）误判为无变化，故按步长比例取整
        if abs(target - cur) < lot - 1e-9:
            return None
        if self.executor.amend_order(inst_id, pt['ord_id'], new_size=target):
            pt['amount'] = target
            self._life(
                f"{inst_id} | 【改量】{self._tag(bucket, slot)} {cur}张→{target}张 | "
                f"原因：{reason}")
            return f'{bucket}.{slot}改量{cur}→{target}'
        task_log.warning(
            f"[双仓位] {inst_id} {self._tag(bucket, slot)} 改量失败（保留{cur}张）")
        return None

    # =================================================================
    # 内部：下单
    # =================================================================

    @staticmethod
    def _mode_of(direction: str) -> str:
        """方向 → 保证金模式（约定：多头全仓、空头逐仓）"""
        return 'cross' if direction == 'long' else 'isolated'

    # 挂单管理器杠杆缓存命中后的交易所回核周期（秒）：
    # 既避免每轮多余查询，又保证最长一天内一定能发现并纠正失真
    LEV_VERIFY_INTERVAL = 86400

    def _set_leverage_if_needed(self, inst_id: str, s: Dict, leverage, mode: str):
        """按配置设置交易所侧杠杆；缓存命中也周期性回交易所核对实际值。

        原实现盲信本地缓存 lev_set：一旦交易所侧实际杠杆与缓存不一致
        （人工在 App 改过、交易所重置、账户杠杆模式变更），系统永不纠正，
        后续开仓一直沿用错误杠杆（2026-09-01 排查结论）。
        """
        if not leverage:
            return
        # 按保证金模式分键缓存：多头(cross)与空头(isolated)各自记录，
        # 避免多空交替时单键缓存反复失效导致每轮重复调 set_leverage
        lev = s.get('lev_set')
        if not isinstance(lev, dict):
            lev = {}
            s['lev_set'] = lev
        vkey = f'{inst_id}:{mode}'
        if lev.get(mode) != leverage:
            if self.executor.set_leverage(inst_id, leverage, mode, None):
                lev[mode] = leverage
                self._lev_verified[vkey] = time.time()
            return
        # 缓存命中：超过回核周期回交易所核对一次（进程重启后首次开仓即核对）
        if time.time() - self._lev_verified.get(vkey, 0) < self.LEV_VERIFY_INTERVAL:
            return
        actual = self.executor.get_leverage_setting(inst_id, mode)
        if actual is None:
            # 查询失败（网络/API）：保守信任缓存，不因校验失败阻断交易
            return
        self._lev_verified[vkey] = time.time()
        if abs(actual - float(leverage)) < 1e-6:
            return
        task_log.warning(
            f"{self._rid()}{inst_id} | 【杠杆校正】{mode}模式交易所侧实际杠杆="
            f"{actual:g}x ≠ 配置{leverage}x，已重新设置（可能被人工在App改过）")
        if self.executor.set_leverage(inst_id, leverage, mode, None):
            lev[mode] = leverage
        else:
            # 重设失败：清除缓存使下轮重试，不让错误杠杆静默建仓
            lev.pop(mode, None)

    def _place_entry(self, inst_id: str, bucket: str, direction: str,
                     amount: float, price: float, leverage=None,
                     max_position: float = 0, max_total: float = 0,
                     cross_pos: float = None, isolated_pos: float = None,
                     reason: str = '') -> bool:
        """挂开仓限价单（净持仓模式不传 posSide，靠 cross/isolated 区分多空）"""
        s = self._inst(inst_id)
        bk = s[bucket]
        mode = self._mode_of(direction)
        side = 'buy' if direction == 'long' else 'sell'
        amount = self._q(inst_id, amount)
        if amount <= 0 or price <= 0:
            return False
        # 人工同向加仓已由每轮 reconcile 吸收进账本并纳入正常调度，
        # 不再以“账本欠账”为由拒单（否则人工开仓后系统永久不开不平）
        led = {'long': 0.0, 'short': 0.0}
        for b in BUCKETS:
            for d in ('long', 'short'):
                led[d] += float(s[b]['held'].get(d, 0) or 0)
        real = {'long': max(0.0, float(cross_pos or 0)) if cross_pos is not None else None,
                'short': abs(min(0.0, float(isolated_pos or 0)))
                if isolated_pos is not None else None}
        # 账本外真实持仓超额（双向合计）同样计入上限占用，防止上限被绕过
        extra_unbooked = sum(
            max(0.0, real[d] - led[d]) for d in ('long', 'short')
            if real[d] is not None)
        # 单币种仓位上限：本次挂单 + 两篮子双向账本 + 已挂未成交开仓单合计
        # 超限拒单 —— 风控配置必须在交易链路强制执行，而非仅前端展示
        if max_position and max_position > 0:
            occupied = extra_unbooked
            for b in BUCKETS:
                for d in ('long', 'short'):
                    occupied += float(s[b]['held'].get(d, 0) or 0)
                pt = s[b]['slots'][SLOT_ENTRY]
                if pt.get('state') == ST_PENDING:
                    occupied += float(pt.get('amount', 0) or 0)
            if round(occupied + amount, 4) > float(max_position):
                task_log.warning(
                    f"{self._rid()}{inst_id} | 【风控拦截】{self._tag(bucket, SLOT_ENTRY)}拒单 | "
                    f"本次{amount}张 + 已占用{occupied:.1f}张 > 单币种上限"
                    f"{float(max_position):.0f}张")
                return False
        # 全账户总仓位上限：跨全部币种的账本持仓 + 未成交开仓挂单合计
        # （本币种账本外的真实持仓超额一并计入）
        if max_total and max_total > 0:
            total_occ = extra_unbooked
            for st in self.state.values():
                if not isinstance(st, dict):
                    continue
                for b in BUCKETS:
                    bx = st.get(b) or {}
                    for d in ('long', 'short'):
                        total_occ += float((bx.get('held') or {}).get(d, 0) or 0)
                    px = (bx.get('slots') or {}).get(SLOT_ENTRY) or {}
                    if px.get('state') == ST_PENDING:
                        total_occ += float(px.get('amount', 0) or 0)
            if round(total_occ + amount, 4) > float(max_total):
                task_log.warning(
                    f"{self._rid()}{inst_id} | 【风控拦截】{self._tag(bucket, SLOT_ENTRY)}拒单 | "
                    f"本次{amount}张 + 全账户已占用{total_occ:.1f}张 > 总仓位上限"
                    f"{float(max_total):.0f}张")
                return False
        self._set_leverage_if_needed(inst_id, s, leverage, mode)
        res = self.executor.execute_trade(
            inst_id=inst_id, side=side, amount=amount, price=round(price, 6),
            order_type='limit', trading_mode=mode, pos_side=None)
        if res and res.get('success'):
            bk['slots'][SLOT_ENTRY] = {
                'state': ST_PENDING, 'ord_id': res.get('order_id'),
                'price': round(price, 6), 'amount': amount,
                'placed_ts': time.time(), 'acc_filled': 0.0, 'dir': direction}
            self._save(inst_id)  # 立即落库：缩小“下单成功但未存盘即崩溃”的孤儿单窗口
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, SLOT_ENTRY)} 限价"
                f"{'开多' if direction == 'long' else '开空'} {amount}张 @ "
                f"{price:.6g} ({mode}) | 原因：{reason or '-'}")
            _act = '开多' if direction == 'long' else '开空'
            self._life(
                f"{inst_id} | 【{_act}·挂单】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{amount}张 @限价{price:.6g} | 原因：{reason or '-'}", key=True)
            return True
        err = res.get('error') if res else '无返回'
        task_log.warning(f"[双仓位] {inst_id} {self._tag(bucket, SLOT_ENTRY)} 挂单失败: {err}")
        return False

    def _place_exit(self, inst_id: str, bucket: str, direction: str,
                    amount: float, price: float, reason: str = '') -> bool:
        """挂平仓限价单（reduce-only）。direction 为**持仓方向**。"""
        bk = self._inst(inst_id)[bucket]
        mode = self._mode_of(direction)
        side = 'sell' if direction == 'long' else 'buy'
        amount = self._q(inst_id, amount)
        if amount <= 0 or price <= 0:
            return False
        res = self.executor.execute_reduce_only_order(
            inst_id=inst_id, side=side, amount=amount, trading_mode=mode,
            order_type='limit', price=round(price, 6))
        if res and res.get('success'):
            bk['slots'][SLOT_EXIT] = {
                'state': ST_PENDING, 'ord_id': res.get('order_id'),
                'price': round(price, 6), 'amount': amount,
                'placed_ts': time.time(), 'acc_filled': 0.0, 'dir': direction}
            self._save(inst_id)
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, SLOT_EXIT)} 限价"
                f"{'平多' if direction == 'long' else '平空'} {amount}张 @ "
                f"{price:.6g} ({mode}) | 原因：{reason or '-'}")
            _act = '平多' if direction == 'long' else '平空'
            self._life(
                f"{inst_id} | 【{_act}·挂单】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{amount}张 @限价{price:.6g} | 原因：{reason or '-'}", key=True)
            return True
        err = res.get('error') if res else '无返回'
        task_log.warning(f"[双仓位] {inst_id} {self._tag(bucket, SLOT_EXIT)} 挂单失败: {err}")
        return False

    def _exit_room(self, inst_id: str, bucket: str, direction: str,
                   cross_pos: float, isolated_pos: float) -> float:
        """本篮子可挂平仓量上限 = 真实同向持仓 − 全部篮子已挂平仓量。

        净持仓模式下两篮子共享头寸，reduce-only 之外再加一层软件封顶，
        避免同向两篮子同时挂平仓单导致计划平仓量超过真实持仓。
        """
        s = self._inst(inst_id)
        real = max(0.0, float(cross_pos or 0)) if direction == 'long' \
            else abs(min(0.0, float(isolated_pos or 0)))
        pending = 0.0
        for b in BUCKETS:
            pt = s[b]['slots'][SLOT_EXIT]
            if pt.get('state') == ST_PENDING and pt.get('dir') == direction:
                if b == bucket:
                    continue  # 本篮子自己的挂单不计入（供下单/改量目标量使用）
                pending += float(pt.get('amount', 0) or 0)
        # 只做浮点收敛，不在此按步长取整 —— 步长取整由调用方 _q() 统一处理，
        # 这里若沿用 1 位小数会把小步长合约（XRP=0.01）的可平量直接抹成 0
        return round(max(0.0, real - pending), 4)

    def _reset_slot(self, bk: Dict, slot: str):
        """将终态（FILLED/EXPIRED）槽位复位为 IDLE，以便下一轮重新挂单"""
        if bk['slots'][slot].get('state') != ST_PENDING:
            bk['slots'][slot] = self._new_slot()

    # =================================================================
    # 对外：趋势跟踪仓位调度
    # =================================================================

    def process_trend(self, inst_id: str, plan: Dict,
                      cross_pos: float = 0.0, isolated_pos: float = 0.0) -> Dict:
        """趋势跟踪篮子一轮调度。

        plan 字段：
            period_mode : 'single'（单周期→双向）/ 'dual'（双周期→单向）
            target_dir  : 期望持仓方向 'long'/'short'， None 表示无信号
            entry_px    : 开仓限价（反转 bar 的 open/close，由配置决定）
            exit_px     : 平仓限价
            contracts   : 目标持仓张数
            leverage    : 杠杆
            allow_entry : 是否允许开新仓（凌晨强平/风控可关闭）
            open_window / close_window : dual 模式三时段窗口标识

        Returns: {'actions': [...], 'fills': [...]}
        """
        with self._lock:
            acts, fills = [], []
            bk = self._inst(inst_id)[BUCKET_TREND]
            for sl in SLOTS:
                a = self._reconcile_slot(inst_id, BUCKET_TREND, sl, fills)
                if a:
                    acts.append(a)

            desired = plan.get('target_dir')
            desired = desired if desired in ('long', 'short') else None
            args = (inst_id, bk, plan, desired, cross_pos, isolated_pos)
            if (plan.get('period_mode') or 'dual').lower() == 'single':
                acts += self._trend_single(*args)
            else:
                acts += self._trend_dual(*args)
            self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    def _trend_single(self, inst_id: str, bk: Dict, plan: Dict, desired: Optional[str],
                      cross_pos: float, isolated_pos: float) -> list:
        """单周期双向：短周期方向翻转即平旧开新，一次反转信号只挂一次单（不追价）。"""
        acts = []
        if not desired:
            return acts
        entry_px = float(plan.get('entry_px') or 0)
        exit_px = float(plan.get('exit_px') or 0)
        contracts = self._q(inst_id, plan.get('contracts'))
        allow_entry = bool(plan.get('allow_entry', True))
        # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志，
        # 使 trade_operations.log 能直接区分人工锁定与信号驱动的开平仓
        dsrc = str(plan.get('dir_source') or '信号驱动')
        opp = 'short' if desired == 'long' else 'long'

        # 新反转信号：撤销上一轮未成交挂单并复位槽位，开启新一轮挂单机会
        if bk.get('last_desired') != desired:
            prev = bk.get('last_desired')
            for sl in SLOTS:
                if bk['slots'][sl].get('state') == ST_PENDING:
                    if self._cancel_slot(inst_id, BUCKET_TREND, sl,
                                         f"短周期方向翻转({prev or '无'}→{desired})，旧挂单作废"):
                        acts.append(f'trend.{sl}撤单(方向翻转)')
                # 撤单结果不确定时 _cancel_slot 会保持 PENDING，不能强制复位丢账
                if bk['slots'][sl].get('state') != ST_PENDING:
                    bk['slots'][sl] = self._new_slot()
            bk['last_desired'] = desired

        held_d = float(bk['held'].get(desired, 0) or 0)
        held_o = float(bk['held'].get(opp, 0) or 0)

        # ---- 平仓槽：只服务于“反向持仓” ----
        ex = bk['slots'][SLOT_EXIT]
        if self._in_pos(held_o):
            room = self._exit_room(inst_id, BUCKET_TREND, opp, cross_pos, isolated_pos)
            target = self._q(inst_id, min(held_o, room))
            if ex.get('state') == ST_PENDING:
                if ex.get('dir') != opp:
                    if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, '平仓单方向不符'):
                        acts.append('trend.exit撤单(方向不符)')
                else:
                    a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_EXIT, target)
                    if a:
                        acts.append(a)
            elif ex.get('state') == ST_IDLE and exit_px > 0 and target > 0:
                if self._place_exit(inst_id, BUCKET_TREND, opp, target, exit_px,
                                    reason=f"短周期方向翻转，平{opp}旧仓（方向来源：{dsrc}）"):
                    acts.append(f'trend.exit挂单{target}张@{exit_px:.6g}')
            # FILLED / EXPIRED → 本轮不补挂（不追价，等下次反转信号）
        else:
            if ex.get('state') == ST_PENDING:
                if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, '无反向持仓，平仓单作废'):
                    acts.append('trend.exit撤单(无需平仓)')
            else:
                self._reset_slot(bk, SLOT_EXIT)

        # ---- 开仓槽：仅当目标方向无仓时挂单 ----
        en = bk['slots'][SLOT_ENTRY]
        need_entry = allow_entry and not self._in_pos(held_d) and contracts > 0
        if en.get('state') == ST_PENDING and (not need_entry or en.get('dir') != desired):
            why = '目标方向已持仓' if self._in_pos(held_d) else (
                '禁止开新仓' if not allow_entry else (
                    '折算张数不足最小下单量' if contracts <= 0 else '开仓单方向不符'))
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_ENTRY, why):
                acts.append(f'trend.entry撤单({why})')
        elif need_entry and en.get('state') == ST_PENDING and en.get('dir') == desired:
            # 金额模式：每轮按现价/配置折算张数会变，存量挂单须改量跟上，
            # 否则切换 size_mode 后永远沿用旧张数
            a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_ENTRY, contracts,
                                      reason='配置目标量变化(张数/金额模式折算)')
            if a:
                acts.append(a)
        # FILLED 但持仓已清零（人工平仓/强平）：信号仍有效，复位终态槽位重挂，
        # 否则人工平掉后系统永远不会再开仓（方向未翻转就没有复位机会）
        if need_entry and bk['slots'][SLOT_ENTRY].get('state') == ST_FILLED \
                and not self._in_pos(held_d):
            bk['slots'][SLOT_ENTRY] = self._new_slot()
        if need_entry and bk['slots'][SLOT_ENTRY].get('state') == ST_IDLE and entry_px > 0:
            if self._place_entry(inst_id, BUCKET_TREND, desired, contracts, entry_px,
                                 plan.get('leverage'),
                                 max_position=float(plan.get('max_position') or 0),
                                 max_total=float(plan.get('max_total_position') or 0),
                                 cross_pos=cross_pos, isolated_pos=isolated_pos,
                                 reason=f"短周期方向翻转为{desired}，空仓开新"
                                        f"（方向来源：{dsrc}）"):
                acts.append(f'trend.entry挂单{contracts}张@{entry_px:.6g}')
        return acts

    def _trend_dual(self, inst_id: str, bk: Dict, plan: Dict, desired: Optional[str],
                    cross_pos: float, isolated_pos: float) -> list:
        """双周期单向：只做长周期方向，沿用三时段开/平仓窗口。

        窗口上升沿复位对应槽位（新一轮挂单机会），窗口结束撤销未成交挂单。
        长周期反转后的反向持仓不在此处处理（白天发邮件、凌晨由 trend_range_trader 强平）。
        """
        acts = []
        ow = bool(plan.get('open_window'))
        cw = bool(plan.get('close_window'))
        entry_px = float(plan.get('entry_px') or 0)
        exit_px = float(plan.get('exit_px') or 0)
        contracts = self._q(inst_id, plan.get('contracts'))
        allow_entry = bool(plan.get('allow_entry', True))
        # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志
        dsrc = str(plan.get('dir_source') or '信号驱动')

        if desired and bk.get('last_desired') != desired:
            bk['last_desired'] = desired

        # 窗口上升沿 → 复位终态槽位
        if ow and not bk.get('prev_open_confirmed'):
            self._reset_slot(bk, SLOT_ENTRY)
        if cw and not bk.get('prev_close_confirmed'):
            self._reset_slot(bk, SLOT_EXIT)
        bk['prev_open_confirmed'] = ow
        bk['prev_close_confirmed'] = cw

        held_d = float(bk['held'].get(desired, 0) or 0) if desired else 0.0

        # ---- 开仓窗口 ----
        if ow and desired and allow_entry:
            need = self._q(inst_id, contracts - held_d)
            # 窗口持续期内终态槽位（已成交后被人工平掉/被外部撤销）必须能
            # 复位重挂，否则窗口内剩下的 bar 永远无法开仓；窗口本身由真实
            # 三时段共振判定（方向锁定不会使窗口恒开，2026-09-01 语义修正）
            if need > 0 and entry_px > 0 and \
                    bk['slots'][SLOT_ENTRY].get('state') != ST_PENDING:
                self._reset_slot(bk, SLOT_ENTRY)
                if self._place_entry(inst_id, BUCKET_TREND, desired, need, entry_px,
                                     plan.get('leverage'),
                                     max_position=float(plan.get('max_position') or 0),
                                     max_total=float(plan.get('max_total_position') or 0),
                                     cross_pos=cross_pos, isolated_pos=isolated_pos,
                                     reason=f'双周期共振，开仓窗口确认（方向来源：{dsrc}）'):
                    acts.append(f'trend.entry挂单{need}张@{entry_px:.6g}')
            elif bk['slots'][SLOT_ENTRY].get('state') == ST_PENDING:
                # 窗口内存量挂单同步目标量（金额模式折算张数随价格/配置变化）
                a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_ENTRY, need,
                                          reason='配置目标量变化(张数/金额模式折算)',
                                          cancel_reason='目标量不足最小下单量')
                if a:
                    acts.append(a)
        elif bk['slots'][SLOT_ENTRY].get('state') == ST_PENDING:
            why = '禁止开新仓' if (ow and not allow_entry) else '开仓窗口结束'
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_ENTRY, why):
                acts.append(f'trend.entry撤单({why})')

        # ---- 平仓窗口 ----
        if cw and desired and self._in_pos(held_d) and exit_px > 0:
            room = self._exit_room(inst_id, BUCKET_TREND, desired, cross_pos, isolated_pos)
            target = self._q(inst_id, min(held_d, room))
            ex = bk['slots'][SLOT_EXIT]
            if ex.get('state') == ST_PENDING:
                if ex.get('dir') != desired:
                    if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, '平仓单方向不符'):
                        acts.append('trend.exit撤单(方向不符)')
                else:
                    a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_EXIT, target)
                    if a:
                        acts.append(a)
            elif ex.get('state') != ST_PENDING and target > 0:
                # 终态槽位复位重挂：平仓窗口持续期应一直尝试平到目标量，
                # 避免外部撤单/成交后持仓裸露无人处理（同开仓槽逻辑）
                self._reset_slot(bk, SLOT_EXIT)
                if self._place_exit(inst_id, BUCKET_TREND, desired, target, exit_px,
                                    reason=f'双周期短周期翻转，平仓窗口确认（方向来源：{dsrc}）'):
                    acts.append(f'trend.exit挂单{target}张@{exit_px:.6g}')
        elif bk['slots'][SLOT_EXIT].get('state') == ST_PENDING:
            why = '持仓已清零' if not self._in_pos(held_d) else '平仓窗口结束'
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, why):
                acts.append(f'trend.exit撤单({why})')
        return acts

    # =================================================================
    # 对外：区间波动仓位调度
    # =================================================================

    def process_range(self, inst_id: str, plan: Dict,
                      cross_pos: float = 0.0, isolated_pos: float = 0.0) -> Dict:
        """区间波动（boll_limit）篮子一轮调度——固定双周期单向、无限循环。

        无持仓 → 在开仓边界（长多=下轨 / 长空=上轨）挂限价开仓单，边界漂移时 amend 追价；
        成交 → 立即在对侧边界挂 reduce-only 平仓单；平仓成交 → 下一轮重新挂开仓单。

        plan 字段：target_dir / entry_px / exit_px / contracts / leverage /
                   amend_min_pct / allow_entry
        """
        with self._lock:
            acts, fills = [], []
            bk = self._inst(inst_id)[BUCKET_RANGE]
            for sl in SLOTS:
                a = self._reconcile_slot(inst_id, BUCKET_RANGE, sl, fills)
                if a:
                    acts.append(a)

            desired = plan.get('target_dir')
            desired = desired if desired in ('long', 'short') else None
            entry_px = float(plan.get('entry_px') or 0)
            exit_px = float(plan.get('exit_px') or 0)
            contracts = self._q(inst_id, plan.get('contracts'))
            min_pct = float(plan.get('amend_min_pct') or 0)
            allow_entry = bool(plan.get('allow_entry', True))
            # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志
            dsrc = str(plan.get('dir_source') or '信号驱动')

            # 长周期反转：未成交挂单全部撤销换方向重挂（已持仓的强平由 trend_range_trader 执行）
            if desired and bk.get('last_desired') and bk['last_desired'] != desired:
                prev = bk['last_desired']
                for sl in SLOTS:
                    if bk['slots'][sl].get('state') == ST_PENDING:
                        if self._cancel_slot(inst_id, BUCKET_RANGE, sl,
                                             f"长周期反转({prev}→{desired})，区间挂单撤销重挂"):
                            acts.append(f'range.{sl}撤单(长周期反转)')
                    # 撤单结果不确定时 _cancel_slot 会保持 PENDING，不能强制复位丢账
                    if bk['slots'][sl].get('state') != ST_PENDING:
                        bk['slots'][sl] = self._new_slot()
            if desired:
                bk['last_desired'] = desired
            if not desired:
                return {'actions': acts, 'fills': fills}

            held_d = float(bk['held'].get(desired, 0) or 0)

            # ---- 开仓槽：仅无持仓时有效 ----
            en = bk['slots'][SLOT_ENTRY]
            if self._in_pos(held_d) or not allow_entry:
                if en.get('state') == ST_PENDING:
                    why = '已持仓，转挂平仓单' if self._in_pos(held_d) else '禁止开新仓'
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_ENTRY, why):
                        acts.append(f'range.entry撤单({why})')
                else:
                    self._reset_slot(bk, SLOT_ENTRY)  # 终态槽位复位，便于下一轮重挂
            elif en.get('state') == ST_PENDING:
                if en.get('dir') != desired:
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_ENTRY, '开仓单方向不符'):
                        acts.append('range.entry撤单(方向不符)')
                else:
                    # 追价 + 同步量：金额模式下每轮折算张数随价格/配置变化，
                    # 存量挂单必须改量跟上，否则切换 size_mode 后永远沿用旧张数
                    for a in (self._amend_slot_price(inst_id, BUCKET_RANGE, SLOT_ENTRY,
                                                     entry_px, min_pct),
                              self._amend_slot_size(inst_id, BUCKET_RANGE, SLOT_ENTRY,
                                                    contracts,
                                                    reason='配置目标量变化(张数/金额模式折算)',
                                                    cancel_reason='金额模式折算张数不足最小下单量')):
                        if a:
                            acts.append(a)
            elif contracts > 0 and entry_px > 0:
                # IDLE / FILLED / EXPIRED 均重挂 —— 区间仓位需持续循环刷边界
                if self._place_entry(inst_id, BUCKET_RANGE, desired, contracts, entry_px,
                                    plan.get('leverage'),
                                    max_position=float(plan.get('max_position') or 0),
                                    max_total=float(plan.get('max_total_position') or 0),
                                    cross_pos=cross_pos, isolated_pos=isolated_pos,
                                    reason=f"长周期{desired}，BOLL"
                                           f"{'下轨' if desired == 'long' else '上轨'}限价"
                                           f"（方向来源：{dsrc}）"):
                    acts.append(f'range.entry挂单{contracts}张@{entry_px:.6g}')

            # ---- 平仓槽：仅有持仓时有效 ----
            ex = bk['slots'][SLOT_EXIT]
            if not self._in_pos(held_d):
                if ex.get('state') == ST_PENDING:
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_EXIT, '持仓已清零'):
                        acts.append('range.exit撤单(持仓归零)')
                else:
                    self._reset_slot(bk, SLOT_EXIT)
            else:
                room = self._exit_room(inst_id, BUCKET_RANGE, desired,
                                       cross_pos, isolated_pos)
                target = self._q(inst_id, min(held_d, room))
                if ex.get('state') == ST_PENDING:
                    if ex.get('dir') != desired:
                        if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_EXIT, '平仓单方向不符'):
                            acts.append('range.exit撤单(方向不符)')
                    else:
                        for a in (self._amend_slot_price(inst_id, BUCKET_RANGE, SLOT_EXIT,
                                                         exit_px, min_pct),
                                  self._amend_slot_size(inst_id, BUCKET_RANGE, SLOT_EXIT,
                                                        target)):
                            if a:
                                acts.append(a)
                elif exit_px > 0 and target > 0:
                    if self._place_exit(inst_id, BUCKET_RANGE, desired, target, exit_px,
                                        reason=f'对侧BOLL边界止盈（方向来源：{dsrc}）'):
                        acts.append(f'range.exit挂单{target}张@{exit_px:.6g}')

            self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    # =================================================================
    # 对外：每轮前置轮询（成交入账 / 兜底委托触发检测）
    # =================================================================

    def poll_fills(self, inst_id: str, ttl_seconds: float = 0) -> Dict:
        """提前检测两个篮子全部槽位的成交并入账。

        在止盈止损检查之前调用：上一轮挂出的限价单若已成交，先入账才能
        让刚成交的开仓单本轮就受止损保护，而不是等下一轮(60s+)。
        与后续 process_trend/process_range 内的 _reconcile_slot 幂等（槽位
        进入终态后不会重复入账）。
        ttl_seconds: 挂单存续上限（秒），>0 启用 —— 超龄挂单主动撤销（TTL 到期），
        防止区间仓持久挂单/异常残留单在交易所侧无限期存续；撤销后槽位复位，
        同轮调度按最新价重挂。Returns: {'actions': [...], 'fills': [...]}
        """
        with self._lock:
            acts, fills = [], []
            for bucket in BUCKETS:
                for sl in SLOTS:
                    a = self._reconcile_slot(inst_id, bucket, sl, fills)
                    if a:
                        acts.append(a)
            if float(ttl_seconds or 0) > 0:
                for bucket in BUCKETS:
                    for sl in SLOTS:
                        a = self._expire_ttl(inst_id, bucket, sl, float(ttl_seconds))
                        if a:
                            acts.append(a)
            if acts:
                self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    def _expire_ttl(self, inst_id: str, bucket: str, slot: str,
                    ttl_seconds: float) -> Optional[str]:
        """挂单 TTL 到期撤销：存续超过上限的 PENDING 挂单主动撤销（部分成交先入账）。

        趋势仓挂单通常由窗口结束先撤销（auto 模式窗口仅一个短周期），TTL 仅兜底；
        区间仓挂单是持久单（随 BOLL 边界改单追价），靠 TTL 周期性刷新，
        避免程序漏撤/崩溃残留时订单在交易所侧永久存续。返回动作描述或 None。
        """
        pt = self._inst(inst_id)[bucket]['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('placed_ts'):
            return None
        age = time.time() - float(pt.get('placed_ts', 0) or 0)
        if age < ttl_seconds:
            return None
        if self._cancel_slot(
                inst_id, bucket, slot,
                f"TTL到期(存续{age / 60:.0f}分钟>上限{ttl_seconds / 60:.0f}分钟，同轮重挂)"):
            return f'{bucket}.{slot}TTL撤销'
        return None

    def poll_algo_triggers(self, inst_id: str, bucket: str) -> list:
        """检测交易所侧兜底委托是否已触发，触发则定向扣减该篮子账本。

        必须在 reconcile 之前调用 —— 兜底委托（止损/止盈）只属于趋势篮子，
        若交给 reconcile 会被按比例摊到两个篮子造成串账。
        同时清理已触发/已撤销的失效 algo 记录。返回事件描述列表。
        """
        with self._lock:
            events = []
            bk = self._inst(inst_id)[bucket]
            changed = False
            for d in ('long', 'short'):
                rec = bk['algo'].get(d)
                if not rec or not rec.get('algo_id'):
                    continue
                try:
                    detail = self.executor.get_algo_order_details(rec['algo_id'])
                except Exception as e:
                    task_log.warning(f"[双仓位] {inst_id} 查询兜底委托异常: {e}")
                    continue
                if not detail:
                    continue  # 查询失败保留记录，下轮再查
                st = str(detail.get('state', '') or '').lower()
                if st in ('effective', 'order_failed'):
                    # effective=已触发（市价平仓已执行）；定向扣减本篮子账本，
                    # 避免后续 reconcile 把这笔减仓按比例摊到另一个篮子
                    amt = float(rec.get('amount', 0) or 0)
                    if st == 'effective' and amt > 0:
                        self.note_external_close(
                            inst_id, bucket, d, amt, '交易所兜底委托触发')
                        events.append(
                            f"{'多头' if d == 'long' else '空头'}兜底委托已触发，"
                            f"定向扣减{BUCKET_LABEL.get(bucket, bucket)}账本{amt}张")
                    else:
                        events.append(
                            f"{'多头' if d == 'long' else '空头'}兜底委托下单失败，已清理记录")
                    bk['algo'][d] = None
                    changed = True
                elif st in ('canceled', 'cancelled'):
                    # 被外部撤销 → 清理失效记录，下轮 sync_exchange_algo 会重挂
                    bk['algo'][d] = None
                    changed = True
                    events.append(
                        f"{'多头' if d == 'long' else '空头'}兜底委托被外部撤销，"
                        f"已清理记录待重挂")
            if changed:
                self._save(inst_id)
            return events

    # =================================================================
    # 对外：交易所侧止盈止损兜底委托
    # =================================================================

    @staticmethod
    def _px_eq(a, b) -> bool:
        """触发价是否等效（千分之一以内不重挂，避免频繁摧毁重建）"""
        a = float(a or 0)
        b = float(b or 0)
        if a <= 0 and b <= 0:
            return True
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) / max(a, b) < 0.001

    def _cancel_algo(self, inst_id: str, bucket: str, direction: str,
                     reason: str = '') -> bool:
        """撤销某篮子某方向的交易所兜底委托"""
        bk = self._inst(inst_id)[bucket]
        rec = bk['algo'].get(direction)
        if not rec or not rec.get('algo_id'):
            bk['algo'][direction] = None
            return False
        ok = False
        try:
            ok = bool(self.executor.cancel_algo_order(rec['algo_id'], inst_id))
        except Exception as e:
            task_log.warning(f"[双仓位] {inst_id} 撤销兜底委托异常: {e}")
        bk['algo'][direction] = None
        self._life(
            f"{inst_id} | 【兜底委托·撤销】{BUCKET_LABEL.get(bucket, bucket)}"
            f"{'多头' if direction == 'long' else '空头'} | 原因：{reason or '不再需要'}"
            + ('' if ok else '（撤单未确认）'), key=True)
        return True

    def sync_exchange_algo(self, inst_id: str, bucket: str, direction: str,
                           sl_trigger_px: float = None, tp_trigger_px: float = None,
                           amount: float = None) -> Optional[str]:
        """同步交易所侧止盈止损兜底委托（双保险的“保险”那一层）。

        本地评估为主：六类止盈中只有固定目标止盈和固定止损能表达为交易所委托，
        跟踪/动能/通道/分批/时间止盈必须由 tp_engine 本地评估。本方法只负责把
        可表达的部分挂到交易所，防范程序宕机/网络中断期间的极端行情。

        触发价基于**本地账本均价**计算（由调用方传入），委托量不超过本篮子持仓。
        持仓归零或参数变化时自动撤单重挂。返回动作描述或 None。
        """
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            rec = bk['algo'].get(direction)
            held = float(bk['held'].get(direction, 0) or 0)
            amt = self._q(inst_id, min(float(amount) if amount else held, held))
            has_px = (sl_trigger_px and sl_trigger_px > 0) or \
                     (tp_trigger_px and tp_trigger_px > 0)

            if not self._in_pos(held) or amt <= 0 or not has_px:
                if self._cancel_algo(inst_id, bucket, direction,
                                     '持仓归零' if not self._in_pos(held) else '无有效触发价'):
                    self._save(inst_id)
                    return f'{bucket}.algo撤销'
                return None

            # 量差不足一个下单步长视为未变（重挂也无法表达更精细的量）
            _lot, _ = self._steps(inst_id)
            if rec and rec.get('algo_id') \
                    and abs(float(rec.get('amount', 0) or 0) - amt) < _lot - 1e-9 \
                    and self._px_eq(rec.get('sl'), sl_trigger_px) \
                    and self._px_eq(rec.get('tp'), tp_trigger_px):
                return None  # 参数未变 → 保留现有委托

            if rec and rec.get('algo_id'):
                self._cancel_algo(inst_id, bucket, direction, '触发价/数量变更，重挂')

            side = 'sell' if direction == 'long' else 'buy'
            try:
                res = self.executor.create_tp_sl_order(
                    inst_id=inst_id, side=side, amount=amt,
                    trading_mode=self._mode_of(direction),
                    tp_trigger_px=tp_trigger_px, sl_trigger_px=sl_trigger_px)
            except Exception as e:
                task_log.warning(f"[双仓位] {inst_id} 挂兜底委托异常: {e}")
                return None
            if res and res.get('success'):
                bk['algo'][direction] = {
                    'algo_id': res.get('algo_id'), 'amount': amt,
                    'sl': float(sl_trigger_px or 0), 'tp': float(tp_trigger_px or 0),
                    'ts': time.time()}
                self._save(inst_id)
                parts = []
                if tp_trigger_px:
                    parts.append(f"止盈@{float(tp_trigger_px):.6g}")
                if sl_trigger_px:
                    parts.append(f"止损@{float(sl_trigger_px):.6g}")
                trade_log.info(
                    f"[双仓位] {inst_id} {BUCKET_LABEL.get(bucket, bucket)}"
                    f"{'多头' if direction == 'long' else '空头'} 交易所兜底委托已挂 "
                    f"{amt}张 " + ' '.join(parts))
                self._life(
                    f"{inst_id} | 【兜底委托·挂单】{BUCKET_LABEL.get(bucket, bucket)}"
                    f"{'多头' if direction == 'long' else '空头'} | {amt}张 | "
                    + ' '.join(parts), key=True)
                return f'{bucket}.algo挂单'
            task_log.warning(
                f"[双仓位] {inst_id} 兜底委托挂单失败: "
                f"{(res or {}).get('error', '无返回')}")
            return None
