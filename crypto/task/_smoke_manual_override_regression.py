# -*- coding: utf-8 -*-

"""
人工干预三问题专项回归冒烟测试（内存沙箱，不联网、不下真单、不碰生产 MySQL）
================================================================================
针对用户报告的三个生产问题做定向回归，任何改动挂单管理器/交易器后应重跑：
    python crypto/task/_smoke_manual_override_regression.py

场景分组：
  A. 幽灵挂单（跨账号残留/已归档，订单查无此单）：
     A1 区间仓方向不符触发撤单 → 识别订单不存在并复位，按新方向重挂开仓单
        （回归“开仓单方向不符，订单状态查询失败，保持挂单待下轮确认”死循环）
     A2 保护窗口内（刚下单未传播）查无此单 → 不误复位，保持 PENDING
     A3 _reconcile_slot 对超窗幽灵单直接复位，不再每轮查询失败卡死
     A4 平仓槽(exit)幽灵单同样复位，有持仓时重挂平仓单不丢保护
  B. 人工强制方向持续期开仓恢复（问题1）：
     B1 dual 模式窗口恒开 + entry 槽 FILLED + 仓位被人工平掉 → 复位重挂
     B2 成交入账后持仓未清零 → 不重复挂单（幂等）
     B3 single 模式 FILLED + 空仓 → 复位重挂
  C. 人工加仓/入账吸收（问题3）：
     C1 reconcile 把账本外真实持仓吸收进册（归属：持仓篮子 > last_desired 篮子）
     C2 已持同向仓的篮子按比例摊 + 现价加权均价
     C3 吸收后开仓不再被“欠账”拒单，上限占用照常生效
     C4 吸收的持仓能被正常挂平仓单（该平仓得平仓）
  D. 对账缩减回归（防新逻辑破坏旧行为）：
     D1 真实 < 账本按比例缩减 / 缩减至粉尘时篮子清零均价归零
     D2 0.05 张粉尘差值不动作（缩减与吸收双向死区）
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from utils.position_order_manager import (  # noqa: E402
    DualPositionOrderManager, BUCKET_TREND, BUCKET_RANGE)
import utils.position_order_manager as _pom  # noqa: E402

# ---- 内存沙箱：账本持久化层打桩，不碰生产 MySQL ----
# （生产调度器可能正在并发读写 pos_* 表，绝不能清库/覆写）
import copy as _copy  # noqa: E402

_MEM_STATE = {}


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRepo:
    @staticmethod
    def load_position_state(session):
        return _copy.deepcopy(_MEM_STATE)

    @staticmethod
    def save_position_state(session, data):
        _MEM_STATE.clear()
        _MEM_STATE.update(data or {})

    @staticmethod
    def save_inst_position(session, inst_id, s):
        _MEM_STATE[inst_id] = _copy.deepcopy(s or {})


def _fake_session_scope():
    return _FakeSession()


_pom.session_scope = _fake_session_scope
_pom.state_repo = _FakeRepo

PASSED = []


def ok(name, cond, extra=''):
    assert cond, f'FAIL: {name} {extra}'
    PASSED.append(name)
    print(f'  OK {name}')


# =====================================================================
# Mock 交易执行器：与 TradeExecutor.probe_order 同语义
# =====================================================================
class MockExecutor:
    def __init__(self):
        self.seq = 0
        self.orders = {}            # ord_id -> dict（当前账号真实挂单）
        self.ghost = set()          # 查无此单的 ord_id（跨账号残留/已归档）
        self.cancel_calls = []

    def probe_order(self, inst_id, ord_id):
        if ord_id in self.ghost:
            return 'not_found', {}
        o = self.orders.get(ord_id)
        if not o:
            return 'error', {}
        return 'ok', {'state': o['state'], 'accFillSz': o['accFillSz'],
                      'avgPx': o['avgPx']}

    def get_order_info(self, inst_id, ord_id):
        status, info = self.probe_order(inst_id, ord_id)
        return info if status == 'ok' else None

    def execute_trade(self, inst_id, side, amount, price=None, order_type='limit',
                      trading_mode='cross', pos_side=None):
        self.seq += 1
        oid = f'NEW-{self.seq}'
        self.orders[oid] = {'inst': inst_id, 'side': side, 'amount': float(amount),
                            'price': float(price or 0), 'mode': trading_mode,
                            'state': 'live', 'accFillSz': 0.0, 'avgPx': 0.0}
        return {'success': True, 'order_id': oid}

    def execute_reduce_only_order(self, inst_id, side, amount, trading_mode='cross',
                                  order_type='market', price=None):
        self.seq += 1
        oid = f'NEW-{self.seq}'
        self.orders[oid] = {'inst': inst_id, 'side': side, 'amount': float(amount),
                            'price': float(price or 0), 'mode': trading_mode,
                            'state': 'live', 'accFillSz': 0.0, 'avgPx': 0.0,
                            'reduce': True}
        return {'success': True, 'order_id': oid}

    def cancel_normal_order(self, inst_id, ord_id):
        self.cancel_calls.append(ord_id)
        o = self.orders.get(ord_id)
        if o and o['state'] == 'live':
            o['state'] = 'canceled'
        return True

    def amend_order(self, inst_id, ord_id, new_price=None, new_size=None):
        return ord_id in self.orders

    def set_leverage(self, *a, **kw):
        return True

    def get_algo_order_details(self, algo_id):
        return {}

    def place_algo_oco(self, *a, **kw):
        return None

    def cancel_algo_order(self, *a, **kw):
        return True


INST = 'NEAR-USDT-SWAP'


def new_mgr():
    _MEM_STATE.clear()
    ex = MockExecutor()
    m = DualPositionOrderManager(ex)
    m.set_context('SMOKE', True)
    return m, ex


def slot(m, bucket, name):
    return m.state[INST][bucket]['slots'][name]


def put_pending(m, inst, bucket, name, ord_id, amount, price, d, age=600.0):
    """构造 PENDING 槽位，age 为挂单距今秒数（默认超保护窗口）"""
    bk = m._inst(inst)[bucket]
    bk['slots'][name] = {
        'state': 'PENDING', 'ord_id': ord_id, 'price': price,
        'amount': amount, 'placed_ts': time.time() - age,
        'acc_filled': 0.0, 'dir': d}


print('--- A. 幽灵挂单识别与复位（问题2） ---')

# A1: 区间仓旧挂单(方向不符)在当前账号查无此单 → 撤单路径复位槽位并按新方向重挂
m, ex = new_mgr()
put_pending(m, INST, BUCKET_RANGE, 'entry', 'GHOST-1', 0.3, 99.0, 'short')
ex.ghost.add('GHOST-1')
r = m.process_range(INST, {'target_dir': 'long', 'entry_px': 100.0,
                           'exit_px': 110.0, 'contracts': 0.3},
                    cross_pos=0.0, isolated_pos=0.0)
en = slot(m, BUCKET_RANGE, 'entry')
new_ord = ex.orders.get(en.get('ord_id'))
ok('A1 幽灵单复位并按新方向重挂',
   en['state'] == 'PENDING' and en['dir'] == 'long' and en['ord_id'] != 'GHOST-1'
   and new_ord is not None and new_ord['side'] == 'buy'
   and 'GHOST-1' not in ex.cancel_calls,
   f"slot={en} cancel_calls={ex.cancel_calls}")

# A2: 保护窗口内（刚下单 10 秒）查无此单 → 按瞬时故障处理，保持 PENDING 不误复位
m, ex = new_mgr()
put_pending(m, INST, BUCKET_RANGE, 'entry', 'FRESH-1', 0.3, 99.0, 'short', age=10.0)
ex.ghost.add('FRESH-1')
m.process_range(INST, {'target_dir': 'long', 'entry_px': 100.0,
                       'exit_px': 110.0, 'contracts': 0.3},
                cross_pos=0.0, isolated_pos=0.0)
en = slot(m, BUCKET_RANGE, 'entry')
ok('A2 保护窗口内不误复位',
   en['state'] == 'PENDING' and en['ord_id'] == 'FRESH-1', f'slot={en}')

# A3: _reconcile_slot 每轮检查时识别超窗幽灵单 → 复位，不再卡死
m, ex = new_mgr()
put_pending(m, INST, BUCKET_TREND, 'entry', 'GHOST-2', 0.2, 88.0, 'long')
ex.ghost.add('GHOST-2')
r = m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.2, 'open_window': True},
                    cross_pos=0.0, isolated_pos=0.0)
en = slot(m, BUCKET_TREND, 'entry')
ok('A3 reconcile_slot复位幽灵单后当轮重挂',
   en['state'] == 'PENDING' and en['dir'] == 'long' and en['ord_id'] != 'GHOST-2',
   f'slot={en}')

# A4: 平仓槽(exit)上的幽灵单同样识别复位，有持仓时重新挂平仓单（不卡死不丢平仓保护）
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_TREND]
bk['held']['long'] = 0.2
bk['avg_px']['long'] = 100.0
put_pending(m, INST, BUCKET_TREND, 'exit', 'GHOST-X', 0.2, 105.0, 'long')
ex.ghost.add('GHOST-X')
m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                       'entry_px': 100.0, 'exit_px': 105.0,
                       'contracts': 0.2, 'open_window': True,
                       'close_window': True},
                cross_pos=0.2, isolated_pos=0.0)
exs = slot(m, BUCKET_TREND, 'exit')
ok('A4 平仓槽幽灵单复位并重挂平仓单',
   exs['state'] == 'PENDING' and exs['dir'] == 'long'
   and exs['ord_id'] != 'GHOST-X' and 'GHOST-X' not in ex.cancel_calls,
   f'slot={exs}')

print('--- B. 人工强制方向持续期开仓恢复（问题1） ---')

# B1: dual 模式窗口恒开（人工强制方向），entry 槽 FILLED 但仓位被人工平掉
#     → 终态槽位复位并重新挂开仓单（旧版上升沿失效永远开不了仓）
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_TREND]
bk['slots']['entry'] = {'state': 'FILLED', 'ord_id': 'OLD-1', 'price': 100.0,
                        'amount': 0.2, 'placed_ts': time.time() - 3600,
                        'acc_filled': 0.2, 'dir': 'long'}
bk['held']['long'] = 0.0
bk['prev_open_confirmed'] = True   # 窗口已连续开启（无上升沿）
r = m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.2, 'open_window': True},
                    cross_pos=0.0, isolated_pos=0.0)
en = slot(m, BUCKET_TREND, 'entry')
ok('B1 FILLED+空仓在持续窗口内复位重挂',
   en['state'] == 'PENDING' and en['dir'] == 'long' and en['ord_id'] != 'OLD-1'
   and any('挂单' in a for a in r['actions']),
   f'slot={en} actions={r["actions"]}')

# B2: 上轮挂单成交入账后（有持仓）→ 不重复挂开仓单
en_ord = en['ord_id']
ex.orders[en_ord]['state'] = 'filled'
ex.orders[en_ord]['accFillSz'] = 0.2
ex.orders[en_ord]['avgPx'] = 100.0
r = m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.2, 'open_window': True},
                    cross_pos=0.2, isolated_pos=0.0)
held, _ = m.get_position(INST, BUCKET_TREND, 'long')
en2 = slot(m, BUCKET_TREND, 'entry')
ok('B2 成交入账后不重复开仓',
   held == 0.2 and en2['state'] == 'FILLED'
   and not any('entry挂单' in a for a in r['actions']),
   f'held={held} slot={en2} actions={r["actions"]}')

# B2b: 持仓被人工平掉后 → 下一轮又能重新挂单（循环可恢复）
bk = m._inst(INST)[BUCKET_TREND]
bk['held']['long'] = 0.0
bk['avg_px']['long'] = 0.0
r = m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.2, 'open_window': True},
                    cross_pos=0.0, isolated_pos=0.0)
en3 = slot(m, BUCKET_TREND, 'entry')
ok('B2b 人工平仓后再次重挂',
   en3['state'] == 'PENDING' and en3['dir'] == 'long'
   and en3['ord_id'] != en_ord, f'slot={en3}')

# B3: single 模式同样恢复（FILLED + 空仓 + 方向未翻转）
m, ex = new_mgr()
bk = m._inst(INST)[BUCKET_TREND]
bk['slots']['entry'] = {'state': 'FILLED', 'ord_id': 'OLD-2', 'price': 100.0,
                        'amount': 0.2, 'placed_ts': time.time() - 3600,
                        'acc_filled': 0.2, 'dir': 'long'}
bk['last_desired'] = 'long'       # 方向未翻转（无翻转复位机会）
r = m.process_trend(INST, {'period_mode': 'single', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.2},
                    cross_pos=0.0, isolated_pos=0.0)
en = slot(m, BUCKET_TREND, 'entry')
ok('B3 single模式FILLED+空仓复位重挂',
   en['state'] == 'PENDING' and en['dir'] == 'long' and en['ord_id'] != 'OLD-2',
   f'slot={en}')

print('--- C. 人工加仓/入账吸收（问题3） ---')

# C1: 账本全空、真实多头 0.5 张 → 吸收进 last_desired 同向篮子（区间仓）
m, ex = new_mgr()
m._inst(INST)[BUCKET_RANGE]['last_desired'] = 'long'
m.reconcile(INST, 0.5, 0.0, price=2.0)
h_t, _ = m.get_position(INST, BUCKET_TREND, 'long')
h_r, a_r = m.get_position(INST, BUCKET_RANGE, 'long')
ok('C1 超额吸收进last_desired篮子',
   h_r == 0.5 and h_t == 0.0 and a_r == 2.0,
   f'trend={h_t} range={h_r}@{a_r}')

# C1b: 两篮子都空且无 last_desired → 归属趋势仓
m, ex = new_mgr()
m.reconcile(INST, 0.5, 0.0, price=2.0)
h_t, a_t = m.get_position(INST, BUCKET_TREND, 'long')
h_r, _ = m.get_position(INST, BUCKET_RANGE, 'long')
ok('C1b 无归属线索时默认趋势仓', h_t == 0.5 and h_r == 0.0 and a_t == 2.0,
   f'trend={h_t}@{a_t} range={h_r}')

# C2: 两篮子已持同向仓 → 按比例摊，现价加权均价
m, ex = new_mgr()
bt = m._inst(INST)[BUCKET_TREND]
br = m._inst(INST)[BUCKET_RANGE]
bt['held']['long'], bt['avg_px']['long'] = 0.3, 100.0
br['held']['long'], br['avg_px']['long'] = 0.1, 90.0
m.reconcile(INST, 0.8, 0.0, price=110.0)     # 超额 0.4，按 3:1 摊
h_t, a_t = m.get_position(INST, BUCKET_TREND, 'long')
h_r, a_r = m.get_position(INST, BUCKET_RANGE, 'long')
ok('C2 按比例摊+加权均价',
   h_t == 0.6 and h_r == 0.2
   and abs(a_t - (100 * 0.3 + 110 * 0.3) / 0.6) < 1e-6
   and abs(a_r - (90 * 0.1 + 110 * 0.1) / 0.2) < 1e-6,
   f'trend={h_t}@{a_t} range={h_r}@{a_r}')

# C3: 吸收后开仓放行（不再“欠账”拒单），且上限占用照常生效
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['last_desired'] = 'long'
m.reconcile(INST, 0.5, 0.0, price=2.0)
r = m._place_entry(INST, BUCKET_RANGE, 'long', 0.2, 100.0,
                    max_position=1.0, cross_pos=0.7, isolated_pos=0.0,
                    reason='吸收后放行验证')
en = slot(m, BUCKET_RANGE, 'entry')
ok('C3 吸收后开仓放行', r is True and en['state'] == 'PENDING',
   f'r={r} slot={en}')
r = m._place_entry(INST, BUCKET_TREND, 'long', 0.4, 100.0,
                    max_position=1.0, cross_pos=0.7, isolated_pos=0.0,
                    reason='上限占用验证')
ok('C3b 上限占用含已吸收+已挂单（0.5+0.2+0.4>1.0 拒单）', r is False, f'r={r}')

# C4: 吸收的人工持仓能正常挂平仓单（该平仓得平仓）
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['last_desired'] = 'long'
m.reconcile(INST, 0.5, 0.0, price=2.0)
r = m.process_trend(INST, {'period_mode': 'dual', 'target_dir': 'long',
                           'entry_px': 100.0, 'exit_px': 105.0,
                           'contracts': 0.5, 'close_window': True},
                    cross_pos=0.5, isolated_pos=0.0)
exs = slot(m, BUCKET_TREND, 'exit')
ok('C4 吸收持仓可挂平仓单',
   exs['state'] == 'PENDING' and exs['dir'] == 'long' and exs['amount'] == 0.5
   and ex.orders.get(exs['ord_id'], {}).get('reduce') is True,
   f'slot={exs} actions={r["actions"]}')

print('--- D. 对账缩减回归 ---')

# D1: 真实 < 账本 → 按比例缩减（0.3/0.1 按 0.25 缩为 0.075/0.025）
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.3
m._inst(INST)[BUCKET_TREND]['avg_px']['long'] = 100.0
m._inst(INST)[BUCKET_RANGE]['held']['long'] = 0.1
m._inst(INST)[BUCKET_RANGE]['avg_px']['long'] = 90.0
m.reconcile(INST, 0.1, 0.0, price=2.0)      # 真实只剩 0.1 张
h_t, a_t = m.get_position(INST, BUCKET_TREND, 'long')
h_r, a_r = m.get_position(INST, BUCKET_RANGE, 'long')
ok('D1 缩减按比例',
   abs(h_t - 0.075) < 1e-6 and abs(h_r - 0.025) < 1e-6 and a_t == 100.0,
   f'trend={h_t}@{a_t} range={h_r}@{a_r}')

# D1b: 缩减至粉尘（<=0.01）→ 篮子清零且均价归零，不残留脏账本
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.3
m._inst(INST)[BUCKET_TREND]['avg_px']['long'] = 100.0
m.reconcile(INST, 0.005, 0.0, price=2.0)
h_t, a_t = m.get_position(INST, BUCKET_TREND, 'long')
ok('D1b 缩减清零归均价', h_t == 0.0 and a_t == 0.0, f'trend={h_t}@{a_t}')

# D2: 粉尘死区 —— 差值 <=0.01 不缩减，超额 <=0.05 不吸收
m, ex = new_mgr()
m._inst(INST)[BUCKET_TREND]['held']['long'] = 0.3
m.reconcile(INST, 0.295, 0.0, price=2.0)
h_t, _ = m.get_position(INST, BUCKET_TREND, 'long')
ok('D2a 缩减死区不动作', h_t == 0.3, f'h={h_t}')
m, ex = new_mgr()
m.reconcile(INST, 0.05, 0.0, price=2.0)
h_t, _ = m.get_position(INST, BUCKET_TREND, 'long')
h_r, _ = m.get_position(INST, BUCKET_RANGE, 'long')
ok('D2b 吸收死区不动作', h_t == 0.0 and h_r == 0.0, f'trend={h_t} range={h_r}')

print(f'\n===== ALL {len(PASSED)} CHECKS PASSED =====')
