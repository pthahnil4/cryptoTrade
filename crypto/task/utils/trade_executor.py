#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
交易执行模块
============

功能：
1. 执行时间加权委托（TWAP）
2. 执行追逐限价委托
3. 管理策略委托单
4. 处理委托单的创建、查询和撤销

作者：AI Assistant
创建时间：2025年9月13日
"""

import logging
import time
import json
import re
import threading
import hashlib
import uuid
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime, timedelta
import sys
import os

# 添加上级目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.PublicData as PublicData
import okx.Account as Account

# 配置日志格式 - 只显示时间戳和内容
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.handlers = []
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

# 只读接口限频/退避（问题#8）。双模式导入与 position_order_manager 保持一致；
# 万一导入失败就置 None，_rl() 退化为直连原始调用——绝不因为限频模块自身
# 出问题而阻断交易链路（宁可少一层保护，也不能多一层故障点）。
try:
    from .okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None
        logger.warning("okx_ratelimit 导入失败，只读接口将不限频直连 OKX（检查 "
                       "crypto/task/utils/okx_ratelimit.py 是否缺失）")


# =====================================================================
# 下单幂等打标（问题#4）
# =====================================================================
# 改前全项目一个 clOrdId/algoClOrdId 都没传：一旦下单请求超时/连接被断，客户端
# 根本不知道单子到底有没有落到交易所，只能拿 side+sz 去近似匹配
# （check_pending_orders_before_trade），匹配不上就当失败，下一轮重发——
# 重复开仓就是这么来的。现在每笔委托都带客户订单号：
#   1) 结果未知时可按 clOrdId 精确反查，查到即当成功复用，不再盲重发；
#   2) 事后对账/人工核实时能一眼定位是哪一轮、哪个篮子下的单。
# 注意：本次刻意**不上**「确定性 clOrdId（同参数同 ID，靠交易所拒重来去重）」，
# 因为那会改变下单语义——同币种同价同量的第二笔合法委托也会被当成重复单拒掉，
# 是否要这层保险得由交易侧先定。
_CL_ORD_ID_MAX_LEN = 64                     # OKX: clOrdId/algoClOrdId 1~64 位
_CL_ORD_ID_BAD_CHARS = re.compile(r'[^A-Za-z0-9_-]')


def gen_cl_ord_id(prefix: str = 'ct', seed: str = '') -> str:
    """生成合法且进程内唯一的客户订单号（clOrdId / algoClOrdId 通用）。

    OKX 只接受字母/数字/下划线/连字符，长度 1~64；重复的 clOrdId 会被拒单，
    所以尾部用「毫秒时间戳 + uuid」保证不撞号，seed 只用于日志里回溯下单参数。
    """
    token = f"{int(time.time() * 1000):x}{uuid.uuid4().hex[:8]}"
    cid = f"{prefix}_{token}" if prefix else token
    if seed:
        cid = f"{cid}_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:6]}"
    return _CL_ORD_ID_BAD_CHARS.sub('', cid)[:_CL_ORD_ID_MAX_LEN]


class TradeExecutor:
    """交易执行器"""
    
    def __init__(self, api_key: str = None, api_secret_key: str = None, 
                 passphrase: str = None, flag: str = '0'):
        """
        初始化交易执行器
        
        Args:
            api_key: API密钥
            api_secret_key: API密钥
            passphrase: 密码短语
            flag: 0实盘，1模拟盘
        """
        self.flag = flag
        # 保存凭证，供连接失效时重建客户端（HTTP/2长连接被服务端关闭等场景）
        self._api_key = api_key
        self._api_secret_key = api_secret_key
        self._passphrase = passphrase
        
        # 初始化API客户端
        self.market_api = MarketData.MarketAPI(flag=flag)
        self.trade_api = Trade.TradeAPI(api_key, api_secret_key, passphrase, False, flag)
        # 新版 SDK 构造函数首参是 api_key，公共接口免鉴权须用关键字 flag 传参，
        # 不能传 False 作首参（会被当成 api_key 塞进请求头直接抛 TypeError）
        self.public_api = PublicData.PublicAPI(flag=flag)
        self.account_api = Account.AccountAPI(api_key, api_secret_key, passphrase, False, flag)
        
        # 防重机制初始化
        self.strategy_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_state.json')
        self.strategy_last_order_sig = self._load_state()
        self.pending_order_ids: Set[str] = set()
        self._lock = threading.Lock()
        
        # 委托单类型
        self.ORDER_TYPE_TWAP = 'twap'                # 时间加权委托
        self.ORDER_TYPE_CHASE = 'chase_limit'        # 追逐限价委托
        self.ORDER_TYPE_TRAILING = 'move_order_stop' # 移动止盈止损
        self.ORDER_TYPE_CONDITIONAL = 'conditional'  # 单向止盈止损
        self.ORDER_TYPE_OCO = 'oco'                  # 双向止盈止损
        
        # 委托单状态
        self.ORDER_STATUS_PENDING = 'live'           # 待成交
        self.ORDER_STATUS_PARTIALLY = 'partially_filled'  # 部分成交
        self.ORDER_STATUS_FILLED = 'filled'          # 完全成交
        self.ORDER_STATUS_CANCELLED = 'cancelled'    # 已撤销
    
    def _load_state(self) -> Dict:
        """加载策略状态"""
        try:
            if os.path.exists(self.strategy_state_file):
                with open(self.strategy_state_file, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载策略状态失败: {e}")
            return {}

    def _save_state(self):
        """保存策略状态"""
        try:
            with open(self.strategy_state_file, 'w') as f:
                json.dump(self.strategy_last_order_sig, f, indent=4)
        except Exception as e:
            logger.error(f"保存策略状态失败: {e}")

    def update_order_status(self, order_id: str, status: str):
        """
        更新订单状态（用于移除pending）
        外部调度器检测到订单结束时应调用此方法
        """
        if status in [self.ORDER_STATUS_FILLED, self.ORDER_STATUS_CANCELLED]:
            with self._lock:
                if order_id in self.pending_order_ids:
                    self.pending_order_ids.remove(order_id)
                    logger.info(f"订单 {order_id} 已结束 ({status})，从 pending 移除")

    def _rl(self, group: str, func, *args, **kwargs):
        """只读 OKX 接口的统一出口：节流 + 命中限频时指数退避重试。

        限频模块不可用时直通原始调用，行为与改前完全一致。

        **严禁**把下单/撤单/改单/设杠杆塞进来 —— 那类请求结果未知时只能靠
        clOrdId 反查定性（见 gen_cl_ord_id），自动重发会直接造成重复下单。
        调用方自己已有异常重试循环的地方必须传 retry_on_error=False，
        否则两层网络重试互相放大单轮耗时。
        """
        if _rl_limited is None:
            return func(*args, **kwargs)
        return _rl_limited(group, func, *args, **kwargs)

    def get_last_price(self, inst_id: str) -> float:
        """获取指定合约的最新市场价格（ticker last），失败返回 0.0"""
        try:
            ticker = self._rl('market_ticker', self.market_api.get_ticker, instId=inst_id)
            if ticker and ticker.get('code') == '0' and ticker.get('data'):
                return float(ticker['data'][0].get('last', 0) or 0)
        except Exception as e:
            logger.warning(f"获取最新价格失败 {inst_id}: {e}")
        return 0.0

    # ---------------------------------------------------------------
    # “下单结果未知”定性（配合 gen_cl_ord_id 打标）
    # ---------------------------------------------------------------
    def probe_order_by_cl_id(self, inst_id: str, cl_ord_id: str,
                             tries: int = 3, delay: float = 1.0) -> Tuple[str, Optional[Dict]]:
        """按 clOrdId 反查普通委托，判上一笔“结果未知”的单到底有没有落地。

        Returns:
            ('found', info)  交易所里有这笔单（live/部分成交/已成交/已撤均算），
                             调用方必须按**成功**处理，绝不能再重发
            ('absent', None) 交易所权威回复“不存在/查无此单”，可以安全重发
            ('unknown', None) 查询本身失败（网络/未识别错误码）——宁可停手也
                             不拿“没查到”当“没有”

        判定口径与 probe_order 保持一致：code=='0' 且 data 为空才算权威空；
        错误码只对不上号时额外接受报文里的 'not exist' 措辞（OKX 查 clOrdId
        不存在时回的是错误码而不是空数据）。
        """
        last = None
        for i in range(max(1, tries)):
            try:
                result = self._rl('order_query', self.trade_api.get_order,
                                  instId=inst_id, clOrdId=cl_ord_id, retry_on_error=False)
            except Exception as e:
                logger.warning(f"[clOrdId反查] 第{i + 1}次查询异常 {inst_id} {cl_ord_id}: {e}")
                result = None
            last = result
            if result:
                code = str(result.get('code', ''))
                data = result.get('data') or []
                if code == '0':
                    if data:
                        logger.warning(
                            f"[clOrdId反查] 命中 {inst_id} {cl_ord_id} → "
                            f"ordId={data[0].get('ordId')} state={data[0].get('state')}")
                        return 'found', data[0]
                    return 'absent', None       # 查询成功且明确为空 = 真没有
                msg = str(result.get('msg') or '')
                if code in self.ORDER_NOT_FOUND_CODES or 'not exist' in msg.lower():
                    return 'absent', None
            if i < tries - 1:
                time.sleep(delay)
        logger.warning(f"[clOrdId反查] 无法定性 {inst_id} {cl_ord_id}，最后一次响应: {last}")
        return 'unknown', None

    def probe_algo_by_client_id(self, algo_cl_ord_id: str,
                                tries: int = 3, delay: float = 1.0) -> Tuple[str, Optional[Dict]]:
        """按 algoClOrdId 反查策略委托（追逐/TWAP/止盈止损），语义同 probe_order_by_cl_id。

        SDK 的 get_algo_order_details 只接 algoId/algoClOrdId（没有 instId），
        所以只能按客户号查；查不到时同样分 'absent' / 'unknown' 两种。
        """
        last = None
        for i in range(max(1, tries)):
            try:
                result = self._rl('algo_query', self.trade_api.get_algo_order_details,
                                  algoClOrdId=algo_cl_ord_id, retry_on_error=False)
            except Exception as e:
                logger.warning(f"[algoClOrdId反查] 第{i + 1}次查询异常 {algo_cl_ord_id}: {e}")
                result = None
            last = result
            if result:
                code = str(result.get('code', ''))
                data = result.get('data') or []
                if code == '0':
                    if data:
                        logger.warning(
                            f"[algoClOrdId反查] 命中 {algo_cl_ord_id} → "
                            f"algoId={data[0].get('algoId')} state={data[0].get('state')}")
                        return 'found', data[0]
                    return 'absent', None
                msg = str(result.get('msg') or '')
                if 'not exist' in msg.lower() or 'not found' in msg.lower():
                    return 'absent', None
            if i < tries - 1:
                time.sleep(delay)
        logger.warning(f"[algoClOrdId反查] 无法定性 {algo_cl_ord_id}，最后一次响应: {last}")
        return 'unknown', None

    @staticmethod
    def _unknown_place_failure(cl_ord_id: str, exc: Exception, state: str,
                               tag: str = '下单') -> Dict:
        """反查后的失败返回（absent=可重发，unknown=必须先人工核实）。

        两个分支都回 success=False（不改调用方契约），但 unknown 的 error 文案
        写清 clOrdId 与“可能已落地”，上层日志/告警能直接看出这单待定而不是干净
        的可重发；extra 字段供需要时单独取用。
        """
        if state == 'absent':
            return {'success': False, 'cl_ord_id': cl_ord_id,
                    'error': f'{tag}请求异常，已按 clOrdId={cl_ord_id} 反查确认交易所无此单: {exc}'}
        return {'success': False, 'need_verify': True, 'cl_ord_id': cl_ord_id,
                'error': (f'{tag}请求结果未知: {exc}｜clOrdId={cl_ord_id} 反查同样失败，'
                          f'该单可能已在交易所落地，请先人工核实挂单/成交后再重发')}

    def execute_trade(self, inst_id: str, side: str, amount: float, 
                     price: float = None, order_type: str = 'limit', 
                     trading_mode: str = 'cross', pos_side: str = None) -> Dict:
        """
        统一交易执行接口
        
        Args:
            inst_id: 产品ID
            side: 买卖方向 (buy/sell)
            amount: 数量
            price: 价格 (市价单可忽略)
            order_type: 订单类型 (limit/market)
            trading_mode: 交易模式 (cross/isolated)
            pos_side: 持仓方向 (long/short/net)，可选
            
        Returns:
            dict: 交易结果
        """
        try:
            logger.info(f"执行交易: {inst_id} {side} {amount}张 @ {price if price else '市价'} ({order_type}, {trading_mode})")
            
            # 客户订单号：结果未知时可精确反查（理由见模块头 gen_cl_ord_id 注释）
            cid = gen_cl_ord_id('ct', seed=f'{inst_id}|{side}|{amount}|{price}|{order_type}')
            # 构造下单参数
            params = {
                'instId': inst_id,
                'tdMode': trading_mode,
                'side': side,
                'ordType': order_type,
                'sz': str(amount),
                'clOrdId': cid
            }
            
            if order_type == 'limit':
                if not price:
                    return {'success': False, 'error': '限价单必须指定价格'}
                params['px'] = str(price)
            
            if pos_side:
                params['posSide'] = pos_side
                
            # 执行下单
            # 注意区分：请求抛异常 ≠ 单子没下。超时/连接被断时订单可能已经
            # 落地，直接当失败返回会让上层下一轮重发→重复开仓，所以先反查定性。
            try:
                result = self.trade_api.place_order(**params)
            except Exception as e:
                state, hit = self.probe_order_by_cl_id(inst_id, cid)
                if state == 'found':
                    logger.warning(
                        f"下单请求异常但 clOrdId={cid} 已查到订单，按成功处理不再重发: "
                        f"ordId={hit.get('ordId')} state={hit.get('state')}")
                    return {
                        'success': True,
                        'action': 'place',
                        'order_id': hit.get('ordId'),
                        'cl_ord_id': cid,
                        'recovered_by_cl_ord_id': True,
                        'price': price or self.get_last_price(inst_id),
                        'amount': amount,
                        'side': side,
                        'order_type': order_type
                    }
                logger.error(f"下单请求异常（反查结果 {state}）: {e}")
                return self._unknown_place_failure(cid, e, state, tag='下单')
            
            if result and result.get('code') == '0':
                order_data = result['data'][0]
                if order_data.get('sCode') == '0':
                    order_id = order_data.get('ordId')
                    logger.info(f"交易成功: 订单ID={order_id}")
                    # 市价单入参 price 可能为空/0，回填最新市场价，避免通知显示 0.0000
                    fill_price = price if price else self.get_last_price(inst_id)
                    return {
                        'success': True,
                        'action': 'place',
                        'order_id': order_id,
                        'cl_ord_id': cid,
                        'price': fill_price,
                        'amount': amount,
                        'side': side,
                        'order_type': order_type
                    }
                else:
                    err_msg = (f"{order_data.get('sCode')}: "
                               f"{order_data.get('sMsg') or '未知错误'}")
                    logger.error(f"交易下单失败: {err_msg}")
                    return {'success': False, 'error': err_msg}
            else:
                # OKX 在批量接口层面只回笼统的 msg（如 All operations failed），
                # 真正的拒单原因在 data[0].sMsg（如下单量低于最小下单量）。
                # 不透出子错误会把可直接定位的原因吞掉，使每轮拒单无法排查
                # （2026-09-01 POL 区间仓持续拒单却无原因可查的教训）
                err_msg = (result or {}).get('msg') or '未知错误'
                sub = (((result or {}).get('data') or [{}]) or [{}])[0] or {}
                if sub.get('sMsg'):
                    err_msg = f"{err_msg} | {sub.get('sCode')}: {sub.get('sMsg')}"
                logger.error(f"交易请求失败: {err_msg}")
                return {'success': False, 'error': err_msg}
                
        except Exception as e:
            logger.error(f"执行交易异常: {e}")
            return {'success': False, 'error': str(e)}

    def execute_twap_order(self, inst_id: str, side: str, total_amount: float,
                          interval: int, single_amount: float, duration: int, 
                          is_same_direction: bool = True,
                          strategy_id: str = 'default',
                          signal_timestamp: float = None,
                          px_var: str = None,
                          px_limit: str = None) -> Dict:
        """
        执行时间加权委托
        参考demo37_时间加权委托(twap).py
        
        Args:
            inst_id: 产品ID（如BTC-USDT-SWAP）
            side: 买卖方向（buy/sell）
            total_amount: 总委托量
            interval: 执行间隔（秒）
            single_amount: 单笔执行量
            duration: 总执行时间（秒）
            is_same_direction: 长短周期是否同向（决定交易模式）
            px_var: 优于盘口价差 (e.g. '0.01')
            px_limit: 吃单限制价 (e.g. '50000')
        
        Returns:
            dict: 委托结果
        """
        try:
            logger.info(f"开始执行TWAP委托: {inst_id} {side} 总量={total_amount} "
                       f"间隔={interval}秒 单笔={single_amount}")
            
            # 获取当前价格计算限制价
            ticker = self._rl('market_ticker', self.market_api.get_ticker, instId=inst_id)
            last_price = 0.0
            if ticker and ticker.get('code') == '0':
                last_price = float(ticker['data'][0]['last'])
                
                # 如果没有传入 px_limit，则使用默认逻辑
                if not px_limit:
                    px_limit_offset = 0.005  # 0.5%
                    if side == 'buy':
                        px_limit = str(round(last_price * (1 + px_limit_offset), 4))
                    else:
                        px_limit = str(round(last_price * (1 - px_limit_offset), 4))
            else:
                if not px_limit:
                    logger.warning("无法获取当前价格，使用默认限制")
                    px_limit = '-1'  # 市价
            
            # --- 防重机制开始 ---
            # 计算签名 (标的+方向+数量+价格+时间戳)
            # 注意：如果signal_timestamp为None，使用当前时间会导致每次签名不同，无法防重(除非并发完全同时)
            # 建议调用方传入 signal_timestamp
            ts = signal_timestamp if signal_timestamp else time.time()
            sig_str = f"{inst_id}|{side}|{total_amount}|{px_limit}|{ts}"
            sig = hashlib.md5(sig_str.encode()).hexdigest()
            
            with self._lock:
                last_info = self.strategy_last_order_sig.get(strategy_id)
                should_block = False
                
                if last_info and last_info.get('sig') == sig:
                    status = last_info.get('status')
                    order_id = last_info.get('order_id')
                    
                    # 检查状态：creating(正在下单), live(新建), partially_filled(部分成交)
                    # 或者 order_id 在 pending_order_ids 中
                    if status in ['creating', 'live', 'partially_filled']:
                        should_block = True
                    elif order_id and order_id in self.pending_order_ids:
                        should_block = True
                
                if should_block:
                    log_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    logger.warning(f"[{log_time}] Duplicate order blocked (sig: {sig})")
                    return {'success': False, 'error': f'Duplicate order blocked (sig: {sig})'}
                
                # 占用签名，标记为 creating
                self.strategy_last_order_sig[strategy_id] = {
                    'sig': sig,
                    'status': 'creating',
                    'timestamp': time.time(),
                    'order_id': None
                }
            # --- 防重机制结束 ---

            # TWAP参数（保持小数精度，不要转换为int）
            sz = str(round(max(0.2, total_amount), 1))  # 委托数量（合约张数，最小0.2）
            
            if not px_var:
                pxVar = '0.001'  # 价格优势比例 0.1%
            else:
                pxVar = px_var
                
            szLimit = str(round(max(0.2, single_amount), 1))  # 单笔执行数量（最小0.2）
            timeInterval = str(interval)  # 时间间隔
            
            # 检查参数合理性
            if float(szLimit) >= float(sz):
                # 如果单笔数量大于等于总数量，调整为总数量的一半
                szLimit = str(round(max(0.2, float(sz) / 2), 1))
            
            logger.info(f"TWAP参数: sz={sz}, szLimit={szLimit}, timeInterval={timeInterval}")
            
            # 检查TWAP委托所需保证金
            estimated_margin = total_amount * (float(px_limit) if px_limit != '-1' else last_price) / 10
            balance = self.get_account_balance()
            if balance:
                available = balance.get('available', 0)
                logger.info(f"TWAP委托预估保证金: {estimated_margin:.2f} USDT, 可用余额: {available:.2f} USDT")
                
                if available < estimated_margin:
                    logger.warning(f"TWAP委托余额可能不足，但继续尝试（TWAP分批执行）")
            
            # 根据demo37的实盘配置创建TWAP委托
            try:
                # 构建TWAP委托参数（参考demo37实盘配置）
                # 根据长短周期方向关系选择交易模式
                td_mode = 'cross' if is_same_direction else 'isolated'
                
                # 智能判断 posSide (Hedge Mode 兼容)
                target_pos_side = 'net'
                positions = self.get_positions(inst_id)
                if positions:
                    # 检查是否存在 Hedge 模式持仓
                    has_hedge_pos = any(p.get('posSide') in ['long', 'short'] for p in positions)
                    if has_hedge_pos:
                        # Hedge 模式逻辑
                        has_short = any(p.get('posSide') == 'short' and float(p.get('pos', 0)) != 0 for p in positions)
                        has_long = any(p.get('posSide') == 'long' and float(p.get('pos', 0)) != 0 for p in positions)
                        
                        if side == 'buy':
                            # 买入: 如果有空单则平空(short)，否则开多(long)
                            target_pos_side = 'short' if has_short else 'long'
                        else: # sell
                            # 卖出: 如果有多单则平多(long)，否则开空(short)
                            target_pos_side = 'long' if has_long else 'short'
                        logger.info(f"TWAP 检测到 Hedge 模式持仓，智能匹配 posSide={target_pos_side}")

                order_params = {
                    'instId': inst_id,
                    'tdMode': td_mode,  # 同向用全仓，不同向用逐仓
                    'side': side,
                    'posSide': target_pos_side,
                    'ordType': self.ORDER_TYPE_TWAP,
                    'sz': sz,
                    'szLimit': szLimit,
                    'timeInterval': timeInterval,
                    'pxLimit': px_limit,
                    'pxVar': pxVar,
                    # 打标：TWAP 是策略单，异常时只能按 algoClOrdId 反查定性
                    'algoClOrdId': gen_cl_ord_id('cttwap', seed=f'{inst_id}|{side}|{sz}')
                }
                
                # 实盘环境下使用智能检测的 posSide
                mode_desc = "全仓模式" if is_same_direction else "逐仓模式"
                logger.info(f"TWAP实盘模式: 使用{mode_desc}（同向={is_same_direction}），posSide={target_pos_side}")
                
                result = self.trade_api.place_algo_order(**order_params)
                
                # 检查TWAP委托结果
                if result and result.get('code') == '0':
                    order_data = result['data'][0]
                    if order_data.get('sCode') == '0':
                        algo_id = order_data.get('algoId')
                        logger.info(f"TWAP委托创建成功: 算法订单ID={algo_id}")
                    else:
                        error_msg = order_data.get('sMsg', '')
                        logger.error(f"TWAP委托创建失败: {error_msg}")
                        # 如果是权限问题，使用限价单代替
                        if '50030' in error_msg or 'permission' in error_msg.lower():
                            result = self._fallback_to_limit_order(inst_id, side, sz, px_limit, last_price)
                        else:
                            return {'success': False, 'error': error_msg}
                else:
                    logger.error(f"TWAP委托请求失败: {result}")
                    return {'success': False, 'error': result.get('msg', '未知错误')}
                    
            except Exception as e:
                logger.warning(f"TWAP委托异常，使用限价单: {e}")
                result = self._fallback_to_limit_order(inst_id, side, sz, px_limit, last_price)
            
            # 返回最终结果
            # 兼容 API 原始响应 (code='0') 和 fallback 返回的自定义字典 (success=True)
            final_success = False
            final_id = None
            
            if result and result.get('code') == '0':
                order_data = result.get('data', [{}])[0]
                final_id = order_data.get('algoId', '')
                if final_id:
                    final_success = True
            elif result and result.get('success') is True:
                final_success = True
                final_id = result.get('order_id', '') or result.get('algo_id', '')

            if final_success and final_id:
                logger.info(f"TWAP委托最终创建成功: ID={final_id}")
                
                # --- 更新状态: 成功 ---
                with self._lock:
                    if self.strategy_last_order_sig.get(strategy_id, {}).get('sig') == sig:
                        self.strategy_last_order_sig[strategy_id]['status'] = 'live'
                        self.strategy_last_order_sig[strategy_id]['order_id'] = final_id
                        self.pending_order_ids.add(final_id)
                        self._save_state()
                # ---------------------

                return {
                    'success': True,
                    'algo_id': final_id,
                    'order_type': self.ORDER_TYPE_TWAP if result.get('code') == '0' else result.get('order_type', 'limit'),
                    'inst_id': inst_id,
                    'side': side,
                    'total_amount': total_amount,
                    'start_time': datetime.now().isoformat()
                }
            else:
                logger.error(f"TWAP委托最终失败: {result}")
                
                # --- 更新状态: 失败 ---
                with self._lock:
                    if self.strategy_last_order_sig.get(strategy_id, {}).get('sig') == sig:
                        self.strategy_last_order_sig[strategy_id]['status'] = 'failed'
                # ---------------------
                
                return {'success': False, 'error': result.get('msg', '未知错误') if isinstance(result, dict) else str(result)}
                
        except Exception as e:
            logger.error(f"执行TWAP委托异常: {e}")
            
            # --- 更新状态: 异常 ---
            with self._lock:
                if 'sig' in locals() and self.strategy_last_order_sig.get(strategy_id, {}).get('sig') == sig:
                    self.strategy_last_order_sig[strategy_id]['status'] = 'failed'
            # ---------------------
            
            return {'success': False, 'error': str(e)}
    
    def _fallback_to_limit_order(self, inst_id: str, side: str, sz: str, 
                                px_limit: str, last_price: float) -> Dict:
        """
        降级为普通限价单
        
        Args:
            inst_id: 产品ID
            side: 买卖方向
            sz: 数量
            px_limit: 限价
            last_price: 当前价格
        
        Returns:
            dict: 委托结果
        """
        try:
            logger.info(f"使用限价单降级处理: {inst_id} {side} {sz}张")
            
            # 使用合理的限价
            price = px_limit if px_limit != '-1' else str(round(last_price, 4))
            
            _cid = gen_cl_ord_id('ctfb', seed=f'{inst_id}|{side}|{sz}|{price}')
            result = self.trade_api.place_order(
                instId=inst_id,
                tdMode='isolated',  # 逐仓模式
                side=side,
                ordType='limit',
                sz=sz,
                px=price,
                clOrdId=_cid
            )
            
            if result and result.get('code') == '0':
                order_id = result['data'][0].get('ordId', '')
                logger.info(f"限价单创建成功: 订单ID={order_id}")
                return {
                    'success': True,
                    'order_id': order_id,
                    'order_type': 'limit',
                    'inst_id': inst_id,
                    'side': side,
                    'amount': sz
                }
            else:
                logger.error(f"限价单创建失败: {result}")
                return {'success': False, 'error': result.get('msg', '未知错误')}
                
        except Exception as e:
            logger.error(f"限价单降级异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def execute_reduce_only_order(self, inst_id: str, side: str, amount: float, 
                                 trading_mode: str = None,
                                 order_type: str = 'market', price: float = None) -> Dict:
        """
        执行减仓/平仓委托（不占用额外保证金）
        参考demo27_市价减仓平仓交易.py
        
        Args:
            inst_id: 产品ID
            side: 买卖方向
            amount: 减仓数量
            trading_mode: 交易模式 ('cross'=全仓, 'isolated'=逐仓). 若为None则自动检测.
            order_type: 订单类型 ('market'=市价减仓, 'limit'=限价减仓/止盈挂单)
            price: 限价单价格（order_type='limit' 时必填）
        
        Returns:
            dict: 委托结果
        """
        try:
            if order_type == 'limit' and not price:
                return {'success': False, 'error': '限价减仓单必须指定价格'}
            _px_txt = f"@ {price}" if order_type == 'limit' else '市价'
            logger.info(f"执行减仓委托: {inst_id} {side} {amount:.1f}张 {_px_txt} ({order_type}, 模式:{trading_mode if trading_mode else 'Auto'})")
            
            # 先检查是否有对应的持仓
            positions = self.get_positions(inst_id)
            if not positions:
                logger.error(f"没有持仓可以减仓")
                return {'success': False, 'error': '没有持仓可以减仓'}
            
            # 自动检测交易模式
            pos_side = None
            if trading_mode is None:
                # 默认使用第一个持仓的模式
                trading_mode = positions[0].get('mgnMode', 'isolated')
                # 尝试获取持仓方向 (posSide)
                # 注意：如果是买入平空(buy close short)，需要posSide=short
                # 如果是卖出平多(sell close long)，需要posSide=long
                # 在单向持仓模式(net)下，posSide通常为net或空
                pos_side = positions[0].get('posSide')
                logger.info(f"自动检测到持仓模式: {trading_mode}, 持仓方向: {pos_side}")
            
            # 确定posSide参数
            # 如果是买卖模式(long/short)，必须指定posSide
            # 逻辑：
            # 如果 side='sell' (平多)，则 posSide 应该是 'long'
            # 如果 side='buy' (平空)，则 posSide 应该是 'short'
            # 如果原始持仓里有明确的 posSide (long/short)，则优先使用它
            
            final_pos_side = None
            
            # 遍历持仓找到匹配的方向
            target_pos_side = 'long' if side == 'sell' else 'short'
            for pos in positions:
                p_side = pos.get('posSide')
                if p_side == target_pos_side:
                    final_pos_side = p_side
                    break
            
            # 如果没找到匹配的(可能是net模式)，尝试使用第一个非net的，或者就默认None
            if not final_pos_side and positions:
                first_pos_side = positions[0].get('posSide')
                if first_pos_side in ['long', 'short']:
                    # 如果持仓是双向模式但我们没找到匹配的，可能意味着没有对应方向的持仓
                    # 但如果我们是减仓，那肯定得有持仓。
                    # 如果 side=sell, 我们找 posSide=long。如果没找到，说明没多单，那此时下单会失败。
                    pass
                elif first_pos_side == 'net':
                    final_pos_side = 'net'

            # 构建减仓订单参数（参考demo27）
            # 同样打 clOrdId：平仓单请求超时时，反查到就当成已发起，
            # 不能让上层以为“没平掉”而再发一市价单（可能反向开仓）
            cid = gen_cl_ord_id('ctr', seed=f'{inst_id}|{side}|{amount}|{order_type}|{price}')
            order_params = {
                'instId': inst_id,
                'tdMode': trading_mode,
                'side': side,
                'ordType': order_type,  # market=市价快速成交 / limit=限价止盈挂单
                'sz': str(round(amount, 1)),
                'reduceOnly': 'true',  # 字符串格式
                'clOrdId': cid
            }
            # 限价减仓：附加委托价格
            if order_type == 'limit':
                order_params['px'] = str(price)
            
            # 只有当检测到有效的 posSide 时才添加，避免 net 模式下出错 (虽然 net 也可以带 posSide=net)
            if final_pos_side:
                order_params['posSide'] = final_pos_side
            
            logger.info(f"减仓订单参数: {order_params}")
            
            # 执行下单
            try:
                result = self.trade_api.place_order(**order_params)
            except Exception as e:
                state, hit = self.probe_order_by_cl_id(inst_id, cid)
                if state == 'found':
                    logger.warning(
                        f"减仓请求异常但 clOrdId={cid} 已查到订单，按已发起处理不再重发: "
                        f"ordId={hit.get('ordId')} state={hit.get('state')}")
                    return {
                        'success': True,
                        'order_id': hit.get('ordId'),
                        'cl_ord_id': cid,
                        'recovered_by_cl_ord_id': True,
                        'order_type': 'reduce_only',
                        'side': side,
                        'amount': amount,
                        'price': hit.get('avgPx') or price or self.get_last_price(inst_id),
                        'trading_mode': trading_mode
                    }
                logger.error(f"减仓请求异常（反查结果 {state}）: {e}")
                return self._unknown_place_failure(cid, e, state, tag='减仓')
            
            logger.info(f"减仓订单返回结果: {result}")
            
            if result and result.get('code') == '0':
                order_data = result['data'][0]
                if order_data.get('sCode') == '0':
                    order_id = order_data.get('ordId')
                    logger.info(f"减仓委托创建成功: 订单ID={order_id}")
                    # 市价减仓无成交价返回，回填限价价/最新市场价，避免通知显示 0.0000
                    return {
                        'success': True,
                        'order_id': order_id,
                        'cl_ord_id': cid,
                        'order_type': 'reduce_only',
                        'side': side,
                        'amount': amount,
                        'price': price if (order_type == 'limit' and price) else self.get_last_price(inst_id),
                        'trading_mode': trading_mode
                    }
                else:
                    error_code = order_data.get('sCode', '')
                    error_msg = order_data.get('sMsg', '未知错误')
                    logger.error(f"减仓委托失败: 错误代码={error_code}, 错误信息={error_msg}")
                    return {'success': False, 'error': f'{error_code}: {error_msg}'}
            else:
                error_code = result.get('code', '')
                error_msg = result.get('msg', '未知错误')
                logger.error(f"减仓委托请求失败: 错误代码={error_code}, 错误信息={error_msg}")
                return {'success': False, 'error': f'{error_code}: {error_msg}'}
                
        except Exception as e:
            logger.error(f"执行减仓委托异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def _auto_cleanup_for_balance(self, inst_id: str, target_side: str, target_amount: float, 
                                 target_is_same_direction: bool) -> Dict:
        """
        自动清理错误持仓来释放余额
        
        Args:
            inst_id: 产品ID
            target_side: 目标委托方向
            target_amount: 目标委托数量
            target_is_same_direction: 目标是否同向（决定目标模式）
        
        Returns:
            dict: 清理结果
        """
        try:
            logger.info(f"[自动清理] 开始自动清理错误持仓释放余额")
            
            # 获取分模式持仓
            positions_by_mode = self.get_all_positions_by_mode(inst_id)
            cross_positions = positions_by_mode['cross']
            isolated_positions = positions_by_mode['isolated']
            
            # 确定目标方向（从目标委托方向推断信号方向）
            target_direction = 'long' if target_side == 'buy' else 'short'
            
            logger.info(f"[自动清理] 目标方向: {target_direction}, 目标模式: {'全仓' if target_is_same_direction else '逐仓'}")
            
            # 收集需要清理的持仓（优先级：方向错误 > 模式错误）
            cleanup_positions = []
            
            # 检查所有持仓，优先清理方向错误的
            all_positions = cross_positions + isolated_positions
            
            for position in all_positions:
                pos_amount = float(position.get('pos', 0))
                if abs(pos_amount) > 0.1:
                    pos_direction = 'long' if pos_amount > 0 else 'short'
                    pos_mode = position.get('mgnMode', 'isolated')
                    
                    # 判断是否需要清理
                    direction_wrong = (pos_direction != target_direction)
                    mode_wrong = (pos_mode == 'cross') != target_is_same_direction
                    
                    if direction_wrong:
                        # 方向错误，优先清理
                        cleanup_positions.insert(0, {
                            'position': position,
                            'priority': 'direction_wrong',
                            'reason': f'方向错误: {pos_direction} vs 目标{target_direction}'
                        })
                    elif mode_wrong:
                        # 模式错误，次优先清理
                        cleanup_positions.append({
                            'position': position,
                            'priority': 'mode_wrong', 
                            'reason': f'模式错误: {pos_mode} vs 目标{"cross" if target_is_same_direction else "isolated"}'
                        })
            
            # 执行清理（按优先级）
            cleaned_count = 0
            for cleanup_item in cleanup_positions:
                position = cleanup_item['position']
                priority = cleanup_item['priority']
                reason = cleanup_item['reason']
                
                pos_amount = float(position.get('pos', 0))
                if abs(pos_amount) > 0.1:
                    # 获取持仓详细信息
                    pos_side = position.get('posSide', 'net')  # 持仓方向
                    pos_direction = 'long' if pos_amount > 0 else 'short'
                    current_mode = position.get('mgnMode', 'isolated')
                    
                    # 确定平仓方向（与持仓方向相反）
                    # 多头持仓 → sell平仓
                    # 空头持仓 → buy平仓
                    reduce_side = 'sell' if pos_amount > 0 else 'buy'
                    reduce_amount = abs(pos_amount)
                    
                    logger.info(f"[自动清理] 持仓详情: {pos_amount:.1f}张 ({pos_direction}, {current_mode}, 持仓方向={pos_side})")
                    logger.info(f"[自动清理] 清理方案 ({priority}): {reduce_side} {reduce_amount:.1f}张")
                    logger.info(f"[自动清理] 清理原因: {reason}")
                    
                    # 按照demo27模板构建减仓订单
                    logger.info(f"[自动清理] 按demo27模板执行减仓: {reduce_side} {reduce_amount:.1f}张")
                    
                    order_params = {
                        'instId': inst_id,
                        'tdMode': current_mode,
                        'side': reduce_side,
                        'ordType': 'market',
                        'sz': str(round(reduce_amount, 1)),
                        'reduceOnly': True,
                        'posSide': pos_side,  # 明确指定持仓方向
                        'clOrdId': gen_cl_ord_id('ctclean',
                                                 seed=f'{inst_id}|{reduce_side}|{reduce_amount}')
                    }
                    
                    result = self.trade_api.place_order(**order_params)
                    
                    if result and result.get('code') == '0':
                        order_data = result['data'][0]
                        if order_data.get('sCode') == '0':
                            order_id = order_data.get('ordId')
                            logger.info(f"[自动清理] 市价清理成功: 订单ID={order_id}")
                            result = {
                                'success': True,
                                'order_id': order_id,
                                'order_type': 'market_cleanup'
                            }
                        else:
                            error_code = order_data.get('sCode', '')
                            error_msg = order_data.get('sMsg', '未知错误')
                            logger.error(f"[自动清理] 市价清理失败: {error_code}: {error_msg}")
                            result = {'success': False, 'error': f'{error_code}: {error_msg}'}
                    else:
                        error_msg = result.get('msg', '未知错误')
                        logger.error(f"[自动清理] 市价清理请求失败: {error_msg}")
                        result = {'success': False, 'error': error_msg}
                    
                    if result.get('success'):
                        logger.info(f"[自动清理] 清理成功: {result.get('order_id')}")
                        cleaned_count += 1
                        # 只清理一个持仓就够了，避免过度清理
                        break
                    else:
                        logger.error(f"[自动清理] 清理失败: {result.get('error')}")
            
            if cleaned_count > 0:
                return {
                    'success': True,
                    'message': f'已清理{cleaned_count}个错误持仓，释放资金'
                }
            else:
                return {
                    'success': False,
                    'error': '没有可清理的错误持仓'
                }
                
        except Exception as e:
            logger.error(f"自动清理持仓异常: {e}")
            return {'success': False, 'error': str(e)}

    def execute_chase_limit_order(self, inst_id: str, side: str, amount: float, 
                                 is_same_direction: bool = True) -> Dict:
        """
        执行追逐限价委托
        参考demo34_追逐限价委托(chase).py
        
        Args:
            inst_id: 产品ID
            side: 买卖方向
            amount: 委托量
            is_same_direction: 长短周期是否同向（决定交易模式）
        
        Returns:
            dict: 委托结果
        """
        try:
            logger.info(f"开始执行追逐限价委托: {inst_id} {side} 数量={amount}")
            
            # 获取当前价格
            ticker = self._rl('market_ticker', self.market_api.get_ticker, instId=inst_id)
            if not ticker or ticker.get('code') != '0':
                logger.error(f"获取行情失败: {ticker}")
                return {'success': False, 'error': '获取行情失败'}
            
            last_price = float(ticker['data'][0]['last'])
            logger.info(f"当前价格: {last_price}, 需要交易: {amount} 张, 价值约: {amount * last_price:.2f} USDT")
            
            # 计算限价（更保守的价格）
            if side == 'buy':
                limit_price = last_price * 1.002  # 买入最高价+0.2%
            else:
                limit_price = last_price * 0.998  # 卖出最低价-0.2%
            
            # 计算委托参数（保留1位小数精度）
            sz = str(round(max(0.2, amount), 1))  # 最小0.2张，保留1位小数
            required_margin = amount * last_price / 10  # 10倍杠杆保证金
            
            logger.info(f"追逐限价委托参数: sz={sz}张, px={limit_price:.4f}")
            logger.info(f"需要保证金约: {required_margin:.2f} USDT (10倍杠杆)")
            
            # 检查余额是否足够，不足时智能切换到减仓模式
            balance = self.get_account_balance()
            if balance:
                available = balance.get('available', 0)
                logger.info(f"当前可用余额: {available:.2f} USDT")
                
                if available < required_margin:
                    logger.warning(f"余额不足: 需要{required_margin:.2f}, 可用{available:.2f}")
                    
                    # 检查是否有持仓可以减仓释放资金
                    positions = self.get_positions(inst_id)
                    if positions:
                        # 计算需要释放的资金
                        needed_funds = required_margin - available + 1.0  # 加1USDT缓冲
                        
                        # 计算需要减仓的张数（考虑手续费，多减10%）
                        reduce_amount = (needed_funds * 10 / last_price) * 1.1  # 杠杆10倍，手续费缓冲
                        reduce_amount = round(max(0.2, reduce_amount), 1)
                        
                        logger.info(f"[余额] 需要释放资金: {needed_funds:.2f} USDT")
                        logger.info(f"[余额] 计算减仓量: {reduce_amount:.1f}张")
                        
                        # 确定减仓方向（与持仓相反，不是与目标委托相反）
                        position = positions[0]
                        pos_amount = float(position.get('pos', 0))
                        if pos_amount > 0:
                            reduce_side = 'sell'  # 多头持仓用sell减仓
                        elif pos_amount < 0:
                            reduce_side = 'buy'   # 空头持仓用buy减仓
                        else:
                            logger.error("持仓为0，无法减仓")
                            return {'success': False, 'error': '持仓为0，无法减仓'}
                        
                        # 获取持仓的交易模式
                        position = positions[0]
                        current_mode = position.get('mgnMode', 'isolated')
                        
                        logger.info(f"[余额] 先减仓释放资金: {reduce_side} {reduce_amount:.1f}张")
                        
                        # 执行减仓操作
                        reduce_result = self.execute_reduce_only_order(
                            inst_id, reduce_side, reduce_amount, current_mode
                        )
                        
                        if reduce_result.get('success'):
                            logger.info(f"[余额] 减仓成功，已释放资金，请稍后重试原委托")
                            return {
                                'success': True,
                                'action': 'reduce_position_for_balance',
                                'reduce_order_id': reduce_result.get('order_id'),
                                'message': f'余额不足，已减仓{reduce_amount:.1f}张释放资金'
                            }
                        else:
                            logger.error(f"[余额] 减仓失败: {reduce_result.get('error')}")
                            return {'success': False, 'error': f'余额不足且减仓失败: {reduce_result.get("error")}'}
                    else:
                        return {'success': False, 'error': f'余额不足: 需要{required_margin:.2f}, 可用{available:.2f}，且无持仓可减'}
            
            # 根据长短周期方向关系选择交易模式
            td_mode = 'cross' if is_same_direction else 'isolated'
            mode_desc = "全仓模式" if is_same_direction else "逐仓模式"
            logger.info(f"使用{mode_desc}（同向={is_same_direction}）")
            
            # 追逐委托是策略单，反查走 algoClOrdId（普通单的 clOrdId 查不到它）。
            # 一次逻辑委托只用同一个客户号：重发前先查单，确认没落地才重发，
            # 查不动就停手。改前是无条件盲重试 3 次（每一次都可能已经落地），
            # 一次网络抖动就能连发 3 张追逐单。
            algo_cid = gen_cl_ord_id('ctchase', seed=f'{inst_id}|{side}|{sz}')
            max_retries = 3
            retry_delay = 2  # 秒
            result = None
            
            # 智能判断 posSide (Hedge Mode 兼容)
            target_pos_side = 'net'
            positions = self.get_positions(inst_id)
            if positions:
                # 检查是否存在 Hedge 模式持仓
                has_hedge_pos = any(p.get('posSide') in ['long', 'short'] for p in positions)
                if has_hedge_pos:
                    # Hedge 模式逻辑
                    has_short = any(p.get('posSide') == 'short' and float(p.get('pos', 0)) != 0 for p in positions)
                    has_long = any(p.get('posSide') == 'long' and float(p.get('pos', 0)) != 0 for p in positions)
                    
                    if side == 'buy':
                        # 买入: 如果有空单则平空(short)，否则开多(long)
                        target_pos_side = 'short' if has_short else 'long'
                    else: # sell
                        # 卖出: 如果有多单则平多(long)，否则开空(short)
                        target_pos_side = 'long' if has_long else 'short'
                    logger.info(f"检测到 Hedge 模式持仓，智能匹配 posSide={target_pos_side}")
            
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"网络重试 {attempt + 1}/{max_retries}")
                        time.sleep(retry_delay)
                    
                    result = self.trade_api.place_algo_order(
                        instId=inst_id,
                        tdMode=td_mode,  # 动态选择交易模式
                        side=side,
                        posSide=target_pos_side,
                        ordType='chase',  # 追逐限价委托
                        sz=sz,
                        algoClOrdId=algo_cid
                    )
                    
                    # 成功获取结果，跳出重试循环
                    break
                    
                except Exception as network_error:
                    # 请求异常 ≠ 没下单：先按 algoClOrdId 反查定性，再决定重发/停手
                    a_state, a_hit = self.probe_algo_by_client_id(algo_cid)
                    if a_state == 'found':
                        logger.warning(
                            f"追逐委托请求异常 (尝试 {attempt + 1}/{max_retries})，但 "
                            f"algoClOrdId={algo_cid} 已查到委托，按成功处理不再重发: "
                            f"algoId={a_hit.get('algoId')} state={a_hit.get('state')}")
                        result = {'code': '0',
                                  'data': [{'algoId': a_hit.get('algoId'),
                                            'sCode': '0', 'sMsg': ''}]}
                        break
                    if a_state == 'unknown':
                        # 定性不了就停手：继续重发等于在一张未知单上再叠一张
                        logger.error(f"追逐委托请求异常且反查无法定性，停止重发待人工核实: "
                                     f"{network_error}")
                        return {'success': False, 'need_verify': True,
                                'algo_cl_ord_id': algo_cid,
                                'error': (f'追逐委托请求结果未知: {network_error}｜'
                                          f'algoClOrdId={algo_cid} 反查同样失败，该委托可能已落地，'
                                          f'请先核对策略委托列表再决定重发')}
                    logger.warning(f"网络请求失败 (尝试 {attempt + 1}/{max_retries})，"
                                   f"反查确认未落地: {network_error}")
                    if attempt == max_retries - 1:
                        # 最后一次尝试失败，使用限价单作为备用（同样打标 + 异常后反查）
                        logger.warning(f"网络超时，使用限价单作为备用方案")
                        fb_cid = gen_cl_ord_id('ctchasefix', seed=f'{inst_id}|{side}|{sz}')
                        try:
                            result = self.trade_api.place_order(
                                instId=inst_id,
                                tdMode=td_mode,
                                side=side,
                                posSide=target_pos_side,
                                ordType='limit',
                                sz=sz,
                                px=str(round(limit_price, 4)),
                                clOrdId=fb_cid
                            )
                            break
                        except Exception as fallback_error:
                            f_state, f_hit = self.probe_order_by_cl_id(inst_id, fb_cid)
                            if f_state == 'found':
                                logger.warning(
                                    f"备用限价单请求异常但 clOrdId={fb_cid} 已查到，按成功处理: "
                                    f"ordId={f_hit.get('ordId')}")
                                result = {'code': '0',
                                          'data': [{'algoId': f_hit.get('ordId'),
                                                    'sCode': '0', 'sMsg': ''}]}
                                break
                            logger.error(f"备用限价单也失败: {fallback_error}")
                            return self._unknown_place_failure(
                                fb_cid, fallback_error, f_state, tag='追逐备用限价单')
                    continue
            
            # 检查是否支持追逐限价
            if result and result.get('code') != '0':
                error_data = result.get('data', [{}])[0]
                error_code = error_data.get('sCode', '')
                error_msg = error_data.get('sMsg', '')
                
                logger.error(f"追逐限价委托失败: 错误代码={error_code}, 错误信息={error_msg}")
                
                # 检测余额不足错误
                if error_code == '51008' and 'insufficient' in error_msg.lower():
                    logger.warning(f"检测到余额不足错误，尝试清理错误持仓释放资金")
                    
                    # 清理不符合方向和模式的持仓
                    cleanup_result = self._auto_cleanup_for_balance(inst_id, side, amount, is_same_direction)
                    if cleanup_result.get('success'):
                        return {
                            'success': True,
                            'action': 'auto_cleanup_for_balance',
                            'cleanup_info': cleanup_result.get('message'),
                            'original_error': f'{error_code}: {error_msg}'
                        }
                    else:
                        return {'success': False, 'error': f'余额不足且清理失败: {cleanup_result.get("error")}'}
                
                elif 'not supported' in error_msg.lower() or 'permission' in error_msg.lower() or error_code in ['50030', '50027']:
                    logger.warning(f"追逐限价不支持，使用限价单: {error_msg}")
                    # 使用限价单代替（打标，便于超时后反查定性）
                    _fb2_cid = gen_cl_ord_id('ctchasefix', seed=f'{inst_id}|{side}|{sz}')
                    result = self.trade_api.place_order(
                        instId=inst_id,
                        tdMode=td_mode,  # 使用动态交易模式
                        side=side,
                        posSide=target_pos_side,
                        ordType='limit',
                        sz=sz,
                        px=str(round(limit_price, 4)),
                        clOrdId=_fb2_cid
                    )
                    logger.info(f"限价单代替结果: {result}")
                else:
                    return {'success': False, 'error': f'{error_code}: {error_msg}'}
            
            if result and result.get('code') == '0':
                # 降级走的是普通限价单，响应里只有 ordId 没有 algoId，
                # 原来的硬下标取法会 KeyError 把整笔委托当作异常吞掉
                _d0 = (result.get('data') or [{}])[0]
                algo_id = _d0.get('algoId') or _d0.get('ordId')
                logger.info(f"追逐限价委托创建成功: 算法订单ID={algo_id}")
                
                return {
                    'success': True,
                    'algo_id': algo_id,
                    'algo_cl_ord_id': algo_cid,
                    'order_type': self.ORDER_TYPE_CHASE,
                    'inst_id': inst_id,
                    'side': side,
                    'amount': amount,
                    'limit_price': limit_price
                }
            else:
                logger.error(f"追逐限价委托创建失败: {result}")
                return {'success': False, 'error': result.get('msg', '未知错误')}
                
        except Exception as e:
            logger.error(f"执行追逐限价委托异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def cancel_algo_order(self, algo_id: str, inst_id: str) -> bool:
        """
        撤销策略委托单
        参考demo39_查询并撤销策略委托订单.py
        
        Args:
            algo_id: 策略委托单ID
            inst_id: 产品ID
        
        Returns:
            bool: 是否成功
        """
        try:
            logger.info(f"撤销策略委托单: 算法订单ID={algo_id} 产品ID={inst_id}")
            
            result = self.trade_api.cancel_algo_order([
                {'algoId': algo_id, 'instId': inst_id}
            ])
            
            if result and result.get('code') == '0':
                logger.info(f"策略委托单撤销成功: {algo_id}")
                return True
            else:
                logger.error(f"策略委托单撤销失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"撤销策略委托单异常: {e}")
            return False
    
    def check_pending_orders_before_trade(self, inst_id: str, side: str, amount: float) -> Dict:
        """
        下单前检查是否有相同的未成交委托
        
        Args:
            inst_id: 产品ID
            side: 交易方向
            amount: 交易数量
        
        Returns:
            dict: 检查结果
        """
        try:
            logger.info(f"检查未成交委托: {inst_id} {side} {amount:.1f}张")
            
            # 获取未成交订单（增加重试机制）
            max_retries = 3
            result = None
            
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"检查委托重试 {attempt + 1}/{max_retries}")
                        time.sleep(1)
                    
                    # 外层已自带 3 次网络重试，这里不再叠加网络重试，
                    # 只取节流 + 限频退避，避免两层重试互相放大单轮耗时
                    result = self._rl('order_query', self.trade_api.get_order_list,
                                      retry_on_error=False, instType="SWAP", instId=inst_id)
                    break  # 成功则跳出重试循环
                    
                except Exception as network_error:
                    logger.warning(f"检查委托网络失败 (尝试 {attempt + 1}/{max_retries}): {network_error}")
                    if attempt == max_retries - 1:
                        logger.error(f"检查未成交委托最终失败，跳过检查")
                        return {'has_duplicate': False, 'message': '网络超时，跳过重复检查'}
            
            if result and result.get('code') == '0':
                orders = result.get('data', [])
                
                # 检查普通委托单
                for order in orders:
                    order_side = order.get('side', '')
                    order_sz = float(order.get('sz', '0'))
                    order_state = order.get('state', '')
                    
                    # 只检查活动状态的订单
                    if order_state in ['live', 'partially_filled']:
                        if order_side == side and abs(order_sz - amount) < 0.1:
                            logger.warning(f"发现相同的普通委托: {order.get('ordId')} {side} {order_sz}张")
                            return {
                                'has_duplicate': True,
                                'order_id': order.get('ordId'),
                                'existing_amount': order_sz,
                                'message': f"已存在相同普通委托: {side} {order_sz:.1f}张"
                            }
                
                # 🎯 关键修复：同时检查策略委托单（追逐限价委托）
                try:
                    algo_result = self._rl('algo_query', self.trade_api.get_algo_order_list,
                                           instType="SWAP", instId=inst_id)
                    
                    if algo_result and algo_result.get('code') == '0':
                        algo_orders = algo_result.get('data', [])
                        
                        for algo_order in algo_orders:
                            algo_side = algo_order.get('side', '')
                            algo_sz = float(algo_order.get('sz', '0'))
                            algo_state = algo_order.get('state', '')
                            
                            # 检查活动状态的策略委托
                            if algo_state in ['live', 'partially_filled']:
                                if algo_side == side and abs(algo_sz - amount) < 0.2:
                                    logger.warning(f"发现相同的策略委托: {algo_order.get('algoId')} {side} {algo_sz}张")
                                    return {
                                        'has_duplicate': True,
                                        'order_id': algo_order.get('algoId'),
                                        'existing_amount': algo_sz,
                                        'message': f"已存在相同策略委托: {side} {algo_sz:.1f}张"
                                    }
                except Exception as algo_error:
                    logger.warning(f"检查策略委托失败: {algo_error}")
                    
                logger.info(f"未发现重复委托，可以下单")
                return {'has_duplicate': False, 'message': '无重复委托'}
            else:
                logger.error(f"查询未成交委托失败: {result}")
                return {'has_duplicate': False, 'message': '查询失败，继续下单'}
                
        except Exception as e:
            logger.error(f"检查未成交委托异常: {e}")
            return {'has_duplicate': False, 'message': f'检查异常: {e}'}

    def get_pending_limit_orders(self, inst_id: str = None) -> List[Dict]:
        """
        获取未成交的普通限价单（live / partially_filled）
        区别于 get_pending_algo_orders（后者仅针对策略委托单）。

        Args:
            inst_id: 产品ID（可选）

        Returns:
            list: 未成交普通订单列表
        """
        try:
            params = {'instType': 'SWAP'}
            if inst_id:
                params['instId'] = inst_id
            result = self._rl('order_query', self.trade_api.get_order_list, **params)
            if result and result.get('code') == '0':
                return result.get('data', [])
            logger.error(f"获取未成交限价单失败: {result}")
            return []
        except Exception as e:
            logger.error(f"获取未成交限价单异常: {e}")
            return []

    # 订单详情接口中权威判定“订单不存在”的错误码（跨账号残留 / 订单已归档）。
    # 与网络超时等瞬时故障严格区分：不存在可安全复位槽位，瞬时故障必须保持 PENDING
    ORDER_NOT_FOUND_CODES = {'51603'}

    def probe_order(self, inst_id: str, ord_id: str) -> Tuple[str, Dict]:
        """
        查询单个普通订单状态，并区分三种结果：
            ('ok', info)       查询成功，info 含 state/accFillSz/avgPx 等
            ('not_found', {})  交易所明确回复订单不存在（跨账号遗留/已归档）
            ('error', {})      网络异常/其他失败（瞬时，下轮重试）
        """
        try:
            result = self._rl('order_query', self.trade_api.get_order,
                              instId=inst_id, ordId=ord_id)
        except Exception as e:
            logger.warning(f"查询订单状态异常 {inst_id} {ord_id}: {e}")
            return 'error', {}
        if result and result.get('code') == '0':
            data = result.get('data', [])
            if data:
                return 'ok', data[0]
            return 'not_found', {}
        code = str((result or {}).get('code', ''))
        if code in self.ORDER_NOT_FOUND_CODES:
            logger.info(f"订单不存在 {inst_id} {ord_id}（code={code}，可能为跨账号遗留）")
            return 'not_found', {}
        logger.warning(f"查询订单状态失败 {inst_id} {ord_id}: {result}")
        return 'error', {}

    def get_order_info(self, inst_id: str, ord_id: str) -> Dict:
        """
        查询单个普通订单状态。

        Returns:
            dict: 订单详情（含 state: live/partially_filled/filled/canceled，fillSz, avgPx 等），失败返回 {}
        """
        status, info = self.probe_order(inst_id, ord_id)
        return info if status == 'ok' else {}

    def cancel_normal_order(self, inst_id: str, ord_id: str) -> bool:
        """
        撤销单个普通限价单（非策略委托）。

        Returns:
            bool: 是否撤销成功
        """
        try:
            result = self.trade_api.cancel_order(instId=inst_id, ordId=ord_id)
            if result and result.get('code') == '0':
                data = result.get('data', [{}])
                if data and data[0].get('sCode') == '0':
                    logger.info(f"撤单成功: {inst_id} {ord_id}")
                    return True
                logger.warning(f"撤单未生效 {inst_id} {ord_id}: {data}")
                return False
            logger.warning(f"撤单请求失败 {inst_id} {ord_id}: {result}")
            return False
        except Exception as e:
            logger.warning(f"撤单异常 {inst_id} {ord_id}: {e}")
            return False

    def amend_order(self, inst_id: str, ord_id: str, new_price: float = None,
                    new_size: float = None, cxl_on_fail: bool = False) -> bool:
        """修改未成交普通限价单的价格/数量（amend-order）。

        Args:
            inst_id: 产品ID
            ord_id: 系统订单ID
            new_price: 新委托价格（为 None 则不改）
            new_size: 新委托张数（为 None 则不改）
            cxl_on_fail: 修改失败时是否自动撤单

        Returns:
            bool: 是否修改成功
        """
        try:
            params = {'instId': inst_id, 'ordId': ord_id,
                      'cxlOnFail': 'true' if cxl_on_fail else 'false'}
            if new_price is not None:
                params['newPx'] = str(new_price)
            if new_size is not None:
                params['newSz'] = str(new_size)
            result = self.trade_api.amend_order(**params)
            if result and result.get('code') == '0':
                data = result.get('data', [{}])
                if data and data[0].get('sCode') == '0':
                    logger.info(f"改单成功: {inst_id} {ord_id} newPx={new_price}")
                    return True
                logger.warning(f"改单未生效 {inst_id} {ord_id}: {data}")
                return False
            logger.warning(f"改单请求失败 {inst_id} {ord_id}: {result}")
            return False
        except Exception as e:
            logger.warning(f"改单异常 {inst_id} {ord_id}: {e}")
            return False

    def set_leverage(self, inst_id: str, lever, mgn_mode: str = 'cross',
                     pos_side: str = None) -> bool:
        """
        设置合约杠杆倍数。

        Args:
            inst_id: 产品ID
            lever: 杠杆倍数
            mgn_mode: 保证金模式 ('cross'/'isolated')
            pos_side: 持仓方向 ('long'/'short')，逐仓双向时需指定

        Returns:
            bool: 是否设置成功
        """
        try:
            params = {'instId': inst_id, 'lever': str(lever), 'mgnMode': mgn_mode}
            if pos_side and mgn_mode == 'isolated':
                params['posSide'] = pos_side
            result = self.account_api.set_leverage(**params)
            if result and result.get('code') == '0':
                logger.info(f"设置杠杆成功: {inst_id} {lever}x ({mgn_mode})")
                return True
            logger.warning(f"设置杠杆失败 {inst_id}: {result}")
            return False
        except Exception as e:
            logger.warning(f"设置杠杆异常 {inst_id}: {e}")
            return False

    def get_leverage_setting(self, inst_id: str,
                             mgn_mode: str = 'cross') -> Optional[float]:
        """查询交易所侧某合约当前的杠杆设置值（GET /account/leverage-info）。

        供挂单管理器周期性校验本地杠杆缓存是否失真 —— 人工在 App 改杠杆、
        交易所侧重置或账户杠杆模式变更都会使本地缓存与实际值偏离。
        查询失败返回 None（调用方应保守处理，不得因此造成不下单）。
        """
        try:
            result = self._rl('leverage', self.account_api.get_leverage,
                              instId=inst_id, mgnMode=mgn_mode)
            if result and result.get('code') == '0' and result.get('data'):
                return float(result['data'][0].get('lever') or 0) or None
            logger.warning(f"查询杠杆设置失败 {inst_id} {mgn_mode}: {result}")
        except Exception as e:
            logger.warning(f"查询杠杆设置异常 {inst_id} {mgn_mode}: {e}")
        return None

    def get_pending_algo_orders(self, inst_id: str = None, ord_type: str = None) -> List[Dict]:
        """
        获取未完成的策略委托单
        参考demo39_查询并撤销策略委托订单.py
        
        Args:
            inst_id: 产品ID（可选）
            ord_type: 委托类型（可选）
        
        Returns:
            list: 委托单列表
        """
        try:
            params = {}
            if ord_type:
                params['ordType'] = ord_type
            if inst_id:
                params['instId'] = inst_id
            
            result = self._rl('algo_query', self.trade_api.order_algos_list, **params)
            
            if result and result.get('code') == '0':
                orders = result.get('data', [])
                logger.info(f"获取到 {len(orders)} 个未完成策略委托单")
                return orders
            else:
                logger.error(f"获取未完成策略委托单失败: {result}")
                return []
                
        except Exception as e:
            logger.error(f"获取未完成策略委托单异常: {e}")
            return []
    
    def get_algo_order_details(self, algo_id: str) -> Dict:
        """
        获取策略委托单详情
        
        Args:
            algo_id: 策略委托单ID
        
        Returns:
            dict: 委托单详情
        """
        try:
            result = self._rl('algo_query', self.trade_api.get_algo_order_details,
                              algoId=algo_id)
            
            if result and result.get('code') == '0':
                return result.get('data', [{}])[0]
            else:
                logger.error(f"获取策略委托单详情失败: {result}")
                return {}
                
        except Exception as e:
            logger.error(f"获取策略委托单详情异常: {e}")
            return {}
    
    def _rebuild_account_api(self):
        """重建账户API客户端。

        OKX SDK 内部使用持久化的 httpx HTTP/2 长连接；定时任务长时间运行时，
        长连接可能被服务端或中间网络断开，复用时抛出传输层异常（其字符串
        形式常为空，即日志里看到的“获取持仓信息异常: ”）。重建客户端可新建连接。
        """
        try:
            self.account_api = Account.AccountAPI(
                self._api_key, self._api_secret_key, self._passphrase, False, self.flag)
            logger.info("[持仓] 已重建账户API连接")
            return True
        except Exception as e:
            logger.error(f"重建账户API连接失败: {type(e).__name__}: {e!r}")
            return False

    def _get_positions_raw(self, inst_id: str = None) -> Dict:
        """调用OKX持仓接口，带重试与连接重建，返回原始 result。

        - 网络/传输层异常：最多重试3次，每次重建连接；均失败则抛出最后异常。
        - API返回 code!=0：非连接问题，直接返回 result，由上层判断。
        """
        params = {}
        if inst_id:
            params['instId'] = inst_id
            params['instType'] = 'SWAP'  # 默认查询永续合约
        last_err = None
        for attempt in range(1, 4):
            try:
                # 本函数自己带 3 次“重建连接重试”，所以不再叠加网络重试
                result = self._rl('positions', self.account_api.get_positions,
                                  retry_on_error=False, **params)
                # code!=0 也属于“成功拿到响应”，不属于连接问题，直接交上层
                return result
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[持仓] 查询异常(第{attempt}/3次) {type(e).__name__}: {e!r}，重建连接后重试")
                self._rebuild_account_api()
                time.sleep(min(0.5 * attempt, 2))
        raise last_err

    def get_all_positions_by_mode(self, inst_id: str) -> Dict:
        """
        获取指定产品的所有持仓，按交易模式分类
        
        Args:
            inst_id: 产品ID
        
        Returns:
            dict: {'cross': 全仓持仓, 'isolated': 逐仓持仓}
        """
        try:
            positions = self.get_positions(inst_id)
            
            result = {'cross': [], 'isolated': []}
            
            for position in positions:
                pos_amount = float(position.get('pos', 0))
                margin_mode = position.get('mgnMode', 'isolated')
                
                # 详细日志，帮助调试
                logger.debug(f"持仓详情: pos={pos_amount}, mgnMode={margin_mode}, 其他信息={position}")
                
                if abs(pos_amount) > 0.001:  # 降低过滤阈值
                    if margin_mode == 'cross':
                        result['cross'].append(position)
                        logger.debug(f"添加到全仓: {pos_amount}张")
                    else:
                        result['isolated'].append(position)
                        logger.debug(f"添加到逐仓: {pos_amount}张")
                else:
                    logger.debug(f"忽略极小持仓: {pos_amount}张")
            
            # 统计信息
            cross_total = sum(float(p.get('pos', 0)) for p in result['cross'])
            isolated_total = sum(float(p.get('pos', 0)) for p in result['isolated'])
            
            logger.info(f"[持仓] {inst_id} 全仓={cross_total:.1f} 逐仓={isolated_total:.1f}")
            
            return result
                
        except Exception as e:
            logger.error(f"获取分模式持仓失败: {e}")
            return {'cross': [], 'isolated': []}

    def try_get_positions_by_mode(self, inst_id: str) -> Optional[Dict]:
        """按模式获取持仓；查询失败(网络/API错误)返回 None，成功返回 {'cross':[], 'isolated':[]}。

        与 get_all_positions_by_mode 的区别：后者失败时返回空 dict（上层无法区分
        “真实空仓” 与 “查询失败”，断网时会误判为空仓 → 可能重复开仓超额）；
        本方法失败返回 None，供上层区分并跳过本轮交易，避免用错误持仓量下单。
        """
        try:
            result = self._get_positions_raw(inst_id)
            if not (result and result.get('code') == '0'):
                logger.error(f"[持仓-严格] {inst_id} 查询失败: {result}")
                return None
            by_mode = {'cross': [], 'isolated': []}
            for pos in result.get('data', []):
                pos_amount = float(pos.get('pos', 0) or 0)
                if abs(pos_amount) <= 0.001:
                    continue
                data = pos.copy()
                data['pos'] = pos_amount
                mode = pos.get('mgnMode', 'isolated')
                by_mode['cross' if mode == 'cross' else 'isolated'].append(data)
            return by_mode
        except Exception as e:
            logger.error(f"[持仓-严格] {inst_id} 查询异常: {type(e).__name__}: {e!r}")
            return None

    def get_position_trading_mode(self, inst_id: str) -> str:
        """
        获取当前持仓的交易模式
        
        Args:
            inst_id: 产品ID
        
        Returns:
            str: 交易模式 ('cross'=全仓, 'isolated'=逐仓, 'none'=无持仓)
        """
        try:
            positions = self.get_positions(inst_id)
            if positions:
                # 获取第一个持仓的交易模式
                position = positions[0]
                margin_mode = position.get('mgnMode', 'isolated')  # 默认逐仓
                
                # OKX API中的保证金模式映射
                if margin_mode == 'cross':
                    return 'cross'  # 全仓
                else:
                    return 'isolated'  # 逐仓
            else:
                return 'none'  # 无持仓
                
        except Exception as e:
            logger.error(f"获取持仓交易模式失败: {e}")
            return 'isolated'  # 默认返回逐仓

    def get_positions(self, inst_id: str = None) -> List[Dict]:
        """
        获取持仓信息
        参考demo07_持仓查询.py
        
        Args:
            inst_id: 产品ID（可选）
        
        Returns:
            list: 持仓列表
        """
        try:
            result = self._get_positions_raw(inst_id)
            
            if result and result.get('code') == '0':
                positions = result.get('data', [])
                
                # 过滤有效持仓（持仓量大于0），保留完整的原始数据
                valid_positions = []
                for pos in positions:
                    if float(pos.get('pos', '0')) != 0:
                        # 保留原始数据，同时添加简化字段
                        position_data = pos.copy()  # 保留所有原始字段
                        
                        # 添加简化字段（向后兼容）
                        position_data.update({
                            'inst_id': pos['instId'],
                            'pos_side': pos['posSide'],
                            'pos': float(pos['pos']),
                            'avg_px': float(pos.get('avgPx', '0')),
                            'upl': float(pos.get('upl', '0')),
                            'upl_ratio': float(pos.get('uplRatio', '0')),
                            'last': float(pos.get('last', '0'))
                        })
                        
                        valid_positions.append(position_data)
                
                logger.info(f"[持仓] 有效持仓数={len(valid_positions)}")
                return valid_positions
            else:
                logger.error(f"获取持仓信息失败: {result}")
                return []
                
        except Exception as e:
            logger.error(f"获取持仓信息异常: {type(e).__name__}: {e!r}")
            return []
    
    def get_account_balance(self, ccy: str = 'USDT') -> Dict:
        """
        获取账户余额
        参考demo02_账户余额查询.py
        
        Args:
            ccy: 币种
        
        Returns:
            dict: 余额信息
        """
        try:
            result = self._rl('balance', self.account_api.get_account_balance)
            # logger.info(f"账户余额原始数据: {result}")
            
            if result and result.get('code') == '0':
                data = result.get('data', [{}])[0]
                
                # 获取USDT的详细信息
                usdt_details = None
                for detail in data.get('details', []):
                    if detail.get('ccy') == 'USDT':
                        usdt_details = detail
                        break
                
                if not usdt_details:
                    logger.error(f"未找到USDT余额信息")
                    return {}
                
                balance_info = {
                    'ccy': usdt_details.get('ccy', 'USDT'),
                    'equity': float(usdt_details.get('eq', '0')),        # 权益
                    'cash_bal': float(usdt_details.get('cashBal', '0')), # 现金余额
                    'upl': float(usdt_details.get('upl', '0')),          # 未实现盈亏
                    'available': float(usdt_details.get('availBal', '0')), # 可用余额
                    'frozen': float(usdt_details.get('frozenBal', '0')),    # 冻结余额
                    'total_equity': float(data.get('totalEq', '0'))      # 总权益
                }
                
                logger.info(f"[余额] 权益={balance_info['equity']:.2f} 可用={balance_info['available']:.2f}")
                return balance_info
            else:
                logger.error(f"获取账户余额失败: {result}")
                return {}
                
        except Exception as e:
            logger.error(f"获取账户余额异常: {e}")
            return {}
    
    def create_trailing_stop_order(self, inst_id: str, side: str, amount: float,
                                  callback_ratio: float, active_price: float = None) -> Dict:
        """
        创建移动止盈止损委托
        参考demo36_移动止盈止损(move_order_stop).py
        
        Args:
            inst_id: 产品ID
            side: 买卖方向（与持仓相反）
            amount: 数量（张数）
            callback_ratio: 回调比例（如0.01表示1%）
            active_price: 激活价格（可选）
        
        Returns:
            dict: 委托结果
        """
        try:
            # 保疙1位小数精度
            sz = str(round(max(0.2, amount), 1))
            logger.info(f"创建移动止盈止损: {inst_id} {side} 数量={sz}张 回调={callback_ratio:.2%}")
            
            # 根据demo36构建移动止盈止损参数
            params = {
                'instId': inst_id,
                'tdMode': 'isolated',  # 逐仓模式（demo36推荐）
                'side': side,
                'ordType': self.ORDER_TYPE_TRAILING,
                'sz': sz,  # 使用保疙1位小数的张数
                'callbackRatio': str(callback_ratio),
                'algoClOrdId': gen_cl_ord_id('cttrail', seed=f'{inst_id}|{side}|{sz}')
            }
            
            # 激活价格（可选）
            if active_price:
                params['activePx'] = str(round(active_price, 4))
            
            # demo36说明：实盘环境下可能需要移除posSide参数
            logger.info(f"移动止损实盘模式: 使用逐仓模式，移除posSide参数")
            
            result = self.trade_api.place_algo_order(**params)
            
            if result and result.get('code') == '0':
                algo_id = result['data'][0]['algoId']
                logger.info(f"移动止盈止损创建成功: 算法订单ID={algo_id}")
                
                return {
                    'success': True,
                    'algo_id': algo_id,
                    'order_type': self.ORDER_TYPE_TRAILING,
                    'inst_id': inst_id,
                    'side': side,
                    'amount': amount,
                    'callback_ratio': callback_ratio
                }
            else:
                logger.error(f"移动止盈止损创建失败: {result}")
                return {'success': False, 'error': result.get('msg', '未知错误')}
                
        except Exception as e:
            logger.error(f"创建移动止盈止损异常: {e}")
            return {'success': False, 'error': str(e)}
    
    def create_tp_sl_order(self, inst_id: str, side: str, amount: float,
                           trading_mode: str = 'cross',
                           tp_trigger_px: float = None, sl_trigger_px: float = None,
                           trigger_px_type: str = 'last',
                           reduce_only: bool = True) -> Dict:
        """
        为已有持仓创建交易所侧止盈/止损策略委托（计划止盈止损）
        参考 demo32_单向止盈止损(conditional).py / demo33_双向止盈止损(oco).py

        只传一侧触发价 → conditional；两侧都传 → oco（任一触发，另一侧自动失效）。
        成交价统一用 -1（市价执行），避免极端行情下限价单挂不出去。

        Args:
            inst_id: 产品ID
            side: 平仓方向（与持仓相反：平多=sell，平空=buy）
            amount: 委托张数
            trading_mode: 保证金模式 ('cross'=全仓, 'isolated'=逐仓)
            tp_trigger_px: 止盈触发价（为 None 则不设止盈）
            sl_trigger_px: 止损触发价（为 None 则不设止损）
            trigger_px_type: 触发价类型 ('last'=最新价, 'mark'=标记价, 'index'=指数价)
            reduce_only: 是否只减仓（防止反向开仓）

        Returns:
            dict: {'success': bool, 'algo_id': str, 'order_type': str, ...}
        """
        try:
            tp = float(tp_trigger_px) if tp_trigger_px and float(tp_trigger_px) > 0 else 0.0
            sl = float(sl_trigger_px) if sl_trigger_px and float(sl_trigger_px) > 0 else 0.0
            if tp <= 0 and sl <= 0:
                return {'success': False, 'error': '止盈/止损触发价至少需指定一个'}

            sz = round(float(amount), 1)
            if sz < 0.1:
                return {'success': False, 'error': f'委托张数过小: {sz}'}

            ord_type = self.ORDER_TYPE_OCO if (tp > 0 and sl > 0) \
                else self.ORDER_TYPE_CONDITIONAL

            params = {
                'instId': inst_id,
                'tdMode': trading_mode,
                'side': side,
                'ordType': ord_type,
                'sz': str(sz),
                'algoClOrdId': gen_cl_ord_id('cttpsl',
                                             seed=f'{inst_id}|{side}|{sz}|{tp}|{sl}')
            }
            # 净持仓模式不传 posSide（传了会被拒），靠 cross/isolated 区分多空
            if reduce_only:
                params['reduceOnly'] = 'true'
            if tp > 0:
                params['tpTriggerPx'] = str(round(tp, 6))
                params['tpOrdPx'] = '-1'              # -1=市价执行
                params['tpTriggerPxType'] = trigger_px_type
            if sl > 0:
                params['slTriggerPx'] = str(round(sl, 6))
                params['slOrdPx'] = '-1'
                params['slTriggerPxType'] = trigger_px_type

            _desc = ' '.join(
                ([f"止盈@{tp:.6g}"] if tp > 0 else []) +
                ([f"止损@{sl:.6g}"] if sl > 0 else []))
            logger.info(f"创建计划止盈止损: {inst_id} {side} {sz}张 {_desc} "
                        f"({ord_type}, 模式:{trading_mode})")

            result = self.trade_api.place_algo_order(**params)

            if result and result.get('code') == '0':
                data = (result.get('data') or [{}])[0]
                algo_id = data.get('algoId')
                logger.info(f"计划止盈止损创建成功: algoId={algo_id}")
                return {
                    'success': True,
                    'algo_id': algo_id,
                    'order_type': ord_type,
                    'inst_id': inst_id,
                    'side': side,
                    'amount': sz,
                    'tp_trigger_px': tp or None,
                    'sl_trigger_px': sl or None
                }

            # 提取子错误码（OKX 常将真正原因放在 data[0].sMsg）
            err = (result or {}).get('msg', '未知错误')
            sub = ((result or {}).get('data') or [{}])[0]
            if sub.get('sMsg'):
                err = f"{err} | {sub.get('sCode')}: {sub.get('sMsg')}"
            logger.error(f"计划止盈止损创建失败: {result}")
            return {'success': False, 'error': err}

        except Exception as e:
            logger.error(f"创建计划止盈止损异常: {e}")
            return {'success': False, 'error': str(e)}

    def close_position(self, inst_id: str, pos_side: str = 'net') -> bool:
        """
        平仓
        
        Args:
            inst_id: 产品ID
            pos_side: 持仓方向（net/long/short）
        
        Returns:
            bool: 是否成功
        """
        try:
            logger.info(f"执行平仓: {inst_id} {pos_side}")
            
            result = self.trade_api.close_positions(
                instId=inst_id,
                posSide=pos_side,
                mgnMode='cross',
                clOrdId=gen_cl_ord_id('ctclose', seed=f'{inst_id}|{pos_side}')
            )
            
            if result and result.get('code') == '0':
                logger.info(f"平仓成功: {inst_id}")
                return True
            else:
                logger.error(f"平仓失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            return False
