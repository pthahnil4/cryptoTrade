#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：下单幂等打标与"结果未知"反查（问题#4）
================================================
安全等级：🔒 纯离线 —— 用假 TradeAPI 顶掉真实客户端，不连 DB、不调 OKX、不发信。

历史坑：全项目一个 clOrdId/algoClOrdId 都没传，下单请求超时/连接被断时，客户端
无法判断单子有没有落到交易所，只能当失败返回；上层下一轮照原计划重发 →
重复开仓。追逐委托更糟：`for attempt in range(3)` 是无条件盲重试，一次网络抖动
最多能连发 3 张。

修复口径（只动 trade_executor 内部，不改下单语义与调用方契约）：
- 每笔委托带唯一 clOrdId/algoClOrdId；
- 请求异常后先按客户号反查定性：found=按成功复用（**不再重发**）、
  absent=确认没落地（可以重发）、unknown=反查也失败（停手并提示需人工核实）。

场景清单：
1. gen_cl_ord_id 合法（字符集/长度）且互不相同
2. execute_trade 正常成功 → 带 clOrdId，返回 cl_ord_id
3. 请求异常 + 反查命中 → 按成功返回、place 只被调用 1 次（没盲重发）
4. 请求异常 + 反查明确"查无此单" → 返回失败、不带 need_verify（可安全重发）
5. 请求异常 + 反查也失败 → 返回失败且 need_verify=True、place 只 1 次
6. execute_reduce_only_order 打标 + 异常后反查命中按成功
7. 追逐委托：反查也失败时只发 1 次就停手（改前会盲重试 3 次）
8. 追逐委托：反查确认未落地时按原逻辑重发，最终成功
9. TWAP/移动止损/计划止盈止损/强平 均带客户号（打标覆盖全）

何时重跑：改 trade_executor.py 的 gen_cl_ord_id / probe_order_by_cl_id /
probe_algo_by_client_id / execute_trade / execute_reduce_only_order /
execute_chase_limit_order 任一逻辑。
"""
import os
import re
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    sys.path.insert(0, _p)
sys.path.insert(0, os.path.join(_HERE, 'utils'))

from utils.trade_executor import TradeExecutor, gen_cl_ord_id  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


# ------------------------------------------------------------------ 假交易所
class FakeTradeAPI:
    """按脚本需要依次吐出预设响应/异常，并记录每次调用入参"""

    def __init__(self):
        self.place_calls = []
        self.place_script = []          # 每项: dict(响应) 或 Exception
        self.get_order_script = []      # 每项: dict(响应) 或 Exception
        self.get_order_calls = []
        self.place_algo_calls = []
        self.place_algo_script = []
        self.get_algo_script = []
        self.get_algo_calls = []

    @staticmethod
    def _pop(script):
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {'code': '0', 'data': [{}]}

    def place_order(self, **kw):
        self.place_calls.append(kw)
        return self._pop(self.place_script)

    def get_order(self, **kw):
        self.get_order_calls.append(kw)
        return self._pop(self.get_order_script)

    def place_algo_order(self, **kw):
        self.place_algo_calls.append(kw)
        return self._pop(self.place_algo_script)

    def get_algo_order_details(self, **kw):
        self.get_algo_calls.append(kw)
        return self._pop(self.get_algo_script)

    def close_positions(self, **kw):
        self.place_calls.append(kw)
        return self._pop(self.place_script)


OK_ORDER = {'code': '0', 'data': [{'sCode': '0', 'sMsg': '', 'ordId': 'ord-777'}]}
TIMEOUT = RuntimeError('Max retries exceeded with url: /api/v5/trade/order (read timed out)')


class FakeMarketAPI:
    @staticmethod
    def get_ticker(instId=None, **kw):
        return {'code': '0', 'data': [{'last': '3.2'}]}


def new_executor():
    """绕开 __init__（不建真实客户端），只注入假 API"""
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.trade_api = FakeTradeAPI()
    ex.market_api = FakeMarketAPI()
    ex.flag = '0'
    ex._lock = threading.Lock()
    # __new__ 不跑 __init__，单证类型常量得补上（否则被测函数取不到）
    ex.ORDER_TYPE_TWAP = 'twap'
    ex.ORDER_TYPE_CHASE = 'chase_limit'
    ex.ORDER_TYPE_TRAILING = 'move_order_stop'
    ex.ORDER_TYPE_CONDITIONAL = 'conditional'
    ex.ORDER_TYPE_OCO = 'oco'
    # 防重签名与落盘路径给内存/临时件，避免测试碰真实的 trade_state.json
    ex.strategy_last_order_sig = {}
    ex.pending_order_ids = set()
    ex.strategy_state_file = os.path.join(
        tempfile.gettempdir(), 'smoke_trade_state_%d.json' % os.getpid())
    return ex


def first(lst):
    """取第一条请求参数；空列表返回 {} 而不是抛 IndexError（否则挂一条带一片）"""
    return lst[0] if lst else {}


# ---------------------------------------------------------------- 1 客户号格式
print("\n[1] clOrdId 生成规则")
ids = {gen_cl_ord_id('ct', seed=f'NEAR-USDT-SWAP|buy|0.2|3.14|limit') for _ in range(200)}
sample = next(iter(ids))
check("1a 字符集合法（字母数字_）", bool(re.fullmatch(r'[A-Za-z0-9_]+', sample)), sample)
check("1b 长度 ≤64", len(sample) <= 64, len(sample))
check("1c 200 次不撞号", len(ids) == 200, len(ids))
check("1d 超长被截断而非报错",
      len(gen_cl_ord_id('x' * 100, seed='y' * 100)) <= 64)

# ---------------------------------------------------------------- 2~5 普通委托
print("\n[2] execute_trade 正常成功")
ex = new_executor()
ex.trade_api.place_script = [OK_ORDER]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
sent = first(ex.trade_api.place_calls)
check("2a 下单带 clOrdId", bool(sent.get('clOrdId')), sent)
check("2b 成功返回 order_id 与 cl_ord_id",
      r.get('success') and r.get('order_id') == 'ord-777' and r.get('cl_ord_id'), r)

print("\n[3] 请求超时 + 反查命中 → 按成功处理，不重发")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': [
    {'ordId': 'ord-888', 'state': 'live', 'px': '3.14'}]}]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("3a 只有 1 次下单请求（未盲重发）", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))
check("3b 按成功返回并复用反查到的 ordId",
      r.get('success') and r.get('order_id') == 'ord-888'
      and r.get('recovered_by_cl_ord_id'), r)
check("3c 反查用的就是本次打标的 clOrdId",
      ex.trade_api.get_order_calls and
      ex.trade_api.get_order_calls[0].get('clOrdId') == r.get('cl_ord_id'),
      ex.trade_api.get_order_calls)

print("\n[4] 请求异常 + 反查明确无此单 → 失败且可安全重发")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': []}]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("4a 返回失败", not r.get('success'), r)
check("4b 不带 need_verify（确认可重发）", not r.get('need_verify'), r)
check("4c 文案说明已反查确认无此单", '反查确认交易所无此单' in str(r.get('error')), r)

print("\n[5] 请求异常 + 反查也失败 → 停手并要求人工核实")
ex = new_executor()
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [TIMEOUT, TIMEOUT, TIMEOUT]
r = ex.execute_trade('NEAR-USDT-SWAP', 'buy', 0.2, price=3.14,
                     order_type='limit', trading_mode='cross')
check("5a 只有 1 次下单请求", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))
check("5b success=False 且 need_verify=True",
      (not r.get('success')) and r.get('need_verify'), r)
check("5c 文案含 clOrdId 与人工核实提示",
      '人工核实' in str(r.get('error')) and r.get('cl_ord_id') in str(r.get('error')),
      r.get('error'))

# ---------------------------------------------------------------- 6 平仓单
print("\n[6] execute_reduce_only_order 打标与恢复")
ex = new_executor()
ex.get_positions = lambda inst_id=None: [{'posSide': 'net', 'mgnMode': 'cross',
                                          'pos': '10'}]
ex.get_last_price = lambda inst_id: 3.2
ex.trade_api.place_script = [TIMEOUT]
ex.trade_api.get_order_script = [{'code': '0', 'data': [
    {'ordId': 'ord-999', 'state': 'filled', 'avgPx': '3.19'}]}]
r = ex.execute_reduce_only_order('NEAR-USDT-SWAP', 'sell', 10.0, trading_mode='cross')
check("6a 平仓请求异常但反查命中 → 按已发起处理",
      r.get('success') and r.get('order_id') == 'ord-999', r)
check("6b 回填成交价取反查 avgPx（不是 0）", float(r.get('price') or 0) > 3.1, r)
check("6c 只发了一次平仓请求", len(ex.trade_api.place_calls) == 1,
      len(ex.trade_api.place_calls))

ex = new_executor()
ex.get_positions = lambda inst_id=None: [{'posSide': 'net', 'mgnMode': 'cross',
                                          'pos': '10'}]
ex.get_last_price = lambda inst_id: 3.2
ex.trade_api.place_script = [OK_ORDER]
ex.execute_reduce_only_order('NEAR-USDT-SWAP', 'sell', 10.0, trading_mode='cross',
                             order_type='limit', price=3.5)
check("6d 正常平仓也带 clOrdId", bool(first(ex.trade_api.place_calls).get('clOrdId')),
      first(ex.trade_api.place_calls))

# ---------------------------------------------------------------- 7~8 追逐委托
print("\n[7] 追逐委托：定性不了就停手（改前会盲重试 3 次）")


def chase_setup():
    ex = new_executor()
    ex.get_positions = lambda inst_id=None: []
    ex.get_account_balance = lambda *a, **kw: {}
    return ex


ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT]
ex.trade_api.get_algo_script = [TIMEOUT, TIMEOUT, TIMEOUT]
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("7a place_algo_order 只被调用 1 次", len(ex.trade_api.place_algo_calls) == 1,
      len(ex.trade_api.place_algo_calls))
check("7b 返回 need_verify 且带 algoClOrdId",
      (not r.get('success')) and r.get('need_verify') and r.get('algo_cl_ord_id'), r)
check("7c 首次请求就带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

print("\n[8] 追逐委托：反查确认未落地 → 按原逻辑重发并最终成功")
ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT, TIMEOUT,
                                  {'code': '0', 'data': [{'algoId': 'algo-1'}]}]
ex.trade_api.get_algo_script = [{'code': '0', 'data': []}] * 3   # 明确查无此单
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("8a 重发到第 3 次成功", len(ex.trade_api.place_algo_calls) == 3
      and r.get('success') and r.get('algo_id') == 'algo-1', r)
check("8b 重发沿用同一个 algoClOrdId",
      len({c.get('algoClOrdId') for c in ex.trade_api.place_algo_calls}) == 1,
      [c.get('algoClOrdId') for c in ex.trade_api.place_algo_calls])

ex = chase_setup()
ex.trade_api.place_algo_script = [TIMEOUT]
ex.trade_api.get_algo_script = [{'code': '0', 'data': [
    {'algoId': 'algo-2', 'state': 'live'}]}]
r = ex.execute_chase_limit_order('NEAR-USDT-SWAP', 'buy', 0.5)
check("8c 反查命中 → 按成功处理且不再重发",
      r.get('success') and r.get('algo_id') == 'algo-2'
      and len(ex.trade_api.place_algo_calls) == 1, r)

# ---------------------------------------------------------------- 9 打标覆盖
print("\n[9] 其余下单出口全部打标")

ex = new_executor()
ex.get_positions = lambda inst_id=None: []
ex.get_account_balance = lambda *a, **kw: {}
ex.execute_twap_order('NEAR-USDT-SWAP', 'buy', 1.0, interval=60, single_amount=0.5,
                      duration=120, is_same_direction=True)
check("9a TWAP 带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_algo_script = [{'code': '0', 'data': [{'algoId': 'a2'}]}]
ex.create_trailing_stop_order('NEAR-USDT-SWAP', 'sell', 1.0, 0.01)
check("9b 移动止损带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_algo_script = [{'code': '0', 'data': [{'algoId': 'a3'}]}]
ex.create_tp_sl_order('NEAR-USDT-SWAP', 'sell', 1.0,
                      trading_mode='cross', tp_trigger_px=4.0)
check("9c 计划止盈止损带 algoClOrdId",
      bool(first(ex.trade_api.place_algo_calls).get('algoClOrdId')),
      first(ex.trade_api.place_algo_calls))

ex = new_executor()
ex.trade_api.place_script = [OK_ORDER]
ex.close_position('NEAR-USDT-SWAP')
check("9d 一键强平带 clOrdId", bool(first(ex.trade_api.place_calls).get('clOrdId')),
      first(ex.trade_api.place_calls))

print("\n" + "=" * 52)
print(f"  PASS {len(PASS)}  /  FAIL {len(FAIL)}")
if FAIL:
    print("  失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
