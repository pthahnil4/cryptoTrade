#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
"""
加密货币批量多周期趋势分析脚本
==============================
遍历 crypto_coins.csv 中的所有加密货币，对每个币种分别执行
15分钟/1小时/4小时/日线的 Pro3 策略计算，提取趋势方向、交易价格、
交易时间和当前盈亏百分比，结果批量写入 CSV 文件。

可独立运行，也可通过 Flask 作为后台任务调用。
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import csv
import datetime
import logging
import numpy as np
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

# 确保可以从 crypto 目录导入本地模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logger = logging.getLogger(__name__)

# 只读接口限频/退避（问题#8）：本模块用线程池并发扫全市场 K 线，
# 是最容易打出 50011 的地方。导入失败时置 None，_rl() 直通不限频。
try:
    from task.utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        from crypto.task.utils.okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None


def _rl(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。"""
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

CSV_PATH = os.path.join(BASE_DIR, 'crypto_coins.csv')

# 全币种行情已迁移 MySQL（迁移批次7b，crypto_coins 表）：
# 读 DB 优先、无数据/不可用时回退 CSV 文件；写 DB 主存 + CSV 双写（文件保留为兜底数据源）。
# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 crypto 目录）
try:
    from crypto.database import session_scope as _db_session_scope
    from crypto import market_data_repo as _market_repo
except ImportError:
    _db_session_scope = None
    _market_repo = None

# 并发 worker 数：共享 httpx 客户端线程安全，小并发即可把总耗时从串行降到约 1/3，
# 同时控制峰值并发请求数（约 3 QPS）避免触发 OKX 限流。
_BATCH_WORKERS = 3

# 批量分析覆盖的周期（按时间尺度升序）：展示顺序与前端一致。
# 新增周期只需在此追加，并在 CSV/DB 列与 run() 写入处同步补齐对应字段。
_BATCH_BARS = ('15m', '1H', '4H', '1D')

# 共享 MarketAPI 客户端：okx SDK 基于 httpx.Client（http2=True），复用同一 HTTP/2
# 长连接可避免高频新建连接被 OKX 服务端主动断开（Server disconnected）导致的
# 请求失败与长时间挂起；httpx.Client 线程安全，可跨线程共享。
_shared_market_api = None
_market_api_lock = threading.Lock()

# SAR K线请求的显式超时（SDK 默认 5 秒偏短，弱网下给足余量）
_SAR_API_TIMEOUT = dict(connect=15.0, read=20.0, write=15.0, pool=15.0)

# SAR K线缓存：(instId, bar) -> (时间戳, K线列表)；指数K线几乎不变，
# 批量任务之间（重启后 5 分钟内）复用，避免重复下载。
_sar_kline_cache = {}
_sar_kline_cache_lock = threading.Lock()
_SAR_KLINE_CACHE_TTL = 300


def _reset_shared_market_api():
    """原子替换共享客户端引用（不主动 close 旧实例）。

    直接 close 共享客户端会让正在使用它的其他线程（监控台轮询/并发 worker）
    撞上 'Cannot send a request, as the client has been closed'；
    旧实例无引用后由 httpx 自动回收连接，因此只需替换引用即可安全重建。
    """
    global _shared_market_api
    with _market_api_lock:
        _shared_market_api = None


def _get_shared_market_api():
    """获取共享的 MarketAPI 客户端（首次创建时设置显式超时）"""
    global _shared_market_api
    with _market_api_lock:
        if _shared_market_api is not None:
            return _shared_market_api
        from api_config import get_api_config
        import okx.MarketData as MarketData
        import httpx
        config = get_api_config()
        api = MarketData.MarketAPI(flag=config.get('flag', '0'))
        try:
            api.timeout = httpx.Timeout(**_SAR_API_TIMEOUT)
        except Exception:
            pass
        _shared_market_api = api
        return api


# ====================================================================
#  进度状态管理（模块级，供 Flask 查询）
# ====================================================================

class _ProgressState:
    """线程安全的进度状态"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.status = "idle"       # idle / running / completed / error
            self.total = 0
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = ""
            self.start_time = None
            self.end_time = None

    def start(self, total: int):
        with self._lock:
            self.status = "running"
            self.total = total
            self.current = 0
            self.success = 0
            self.error = 0
            self.current_symbol = ""
            self.message = "开始批量分析..."
            self.start_time = datetime.datetime.now()
            self.end_time = None

    def update(self, current: int, symbol: str = "",
               success: int = None, error: int = None, message: str = None):
        with self._lock:
            self.current = current
            if symbol:
                self.current_symbol = symbol
            if success is not None:
                self.success = success
            if error is not None:
                self.error = error
            if message:
                self.message = message

    def finish(self, message: str = None):
        with self._lock:
            self.status = "completed"
            self.end_time = datetime.datetime.now()
            if message:
                self.message = message
            else:
                elapsed = (self.end_time - self.start_time).total_seconds() if self.start_time else 0
                self.message = (
                    "批量分析完成！成功 %d/%d，失败 %d，耗时 %.1f 秒"
                    % (self.success, self.total, self.error, elapsed)
                )

    def set_error(self, message: str):
        with self._lock:
            self.status = "error"
            self.end_time = datetime.datetime.now()
            self.message = message

    def to_dict(self) -> Dict:
        with self._lock:
            elapsed = 0
            if self.start_time:
                if self.end_time:
                    elapsed = (self.end_time - self.start_time).total_seconds()
                else:
                    elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
            return {
                "status": self.status,
                "total": self.total,
                "current": self.current,
                "success": self.success,
                "error": self.error,
                "current_symbol": self.current_symbol,
                "message": self.message,
                "elapsed_seconds": round(elapsed, 1),
                "progress_pct": round(self.current / self.total * 100, 1)
                if self.total > 0 else 0,
            }


# 全局进度实例
_progress = _ProgressState()


def get_progress() -> Dict:
    """获取当前进度（供外部调用）"""
    return _progress.to_dict()


# ====================================================================
#  趋势信号提取
# ====================================================================

def _extract_trend_info(coin_result: Dict) -> Dict:
    """从 calculate_single_coin_data 返回中提取趋势+技术指标信息

    Parameters
    ----------
    coin_result : dict
        calculate_single_coin_data 返回的完整结果

    Returns
    -------
    dict
        {
            'trend': '上涨'/'下跌'/'观望'/'错误',
            'trend_flag': 'rise'/'fall'/'wait'/'error',
            'trade_price': float or '',
            'trade_time': str or '',
            'profit_pct': float or '',
            'close_price': float or '',
            'macd_hist': float or '',
            'dif': float or '',
            'adx': float or '',
            'atr_pct': float or '',
        }
    """
    if coin_result is None:
        return {
            'trend': '错误',
            'trend_flag': 'error',
            'trade_price': '',
            'trade_time': '',
            'profit_pct': '',
            'close_price': '',
            'macd_hist': '',
            'dif': '',
            'adx': '',
            'atr_pct': '',
        }

    analysis = coin_result.get('analysis', {})
    modify_flag = analysis.get('modify_flag', 'wait')

    if modify_flag == 'rise':
        trend = '上涨'
    elif modify_flag == 'fall':
        trend = '下跌'
    else:
        trend = '观望'
        modify_flag = 'wait'

    trade_info = coin_result.get('trade_info', {})
    trade_price = trade_info.get('last_trade_price', 0)
    trade_time = trade_info.get('trade_time', '')
    profit_pct = trade_info.get('current_profit', 0)

    # 当前收盘价（用于 ATR% 计算和 SAR 颜色判断）
    close_price = float(coin_result.get('price', 0)) if coin_result.get('price') else 0

    indicators = coin_result.get('indicators', {})
    macd_data = indicators.get('macd', {})
    adx_data = indicators.get('adx', {})

    macd_hist = macd_data.get('histogram', 0)
    dif_val = macd_data.get('dif', 0)
    adx_val = adx_data.get('adx', 0)
    atr_val = indicators.get('atr', 0)

    # ATR% = (ATR值 / 收盘价) * 100 * 10（10倍杠杆）
    atr_pct = ''
    if atr_val is not None and atr_val != '' and close_price > 0:
        try:
            atr_pct = round(float(atr_val) / close_price * 100 * 10, 4)
        except (ValueError, ZeroDivisionError):
            atr_pct = ''

    return {
        'trend': trend,
        'trend_flag': modify_flag,
        'trade_price': round(float(trade_price), 4) if trade_price and float(trade_price) != 0 else '',
        'trade_time': str(trade_time) if trade_time else '',
        'profit_pct': round(float(profit_pct), 2) if profit_pct is not None and profit_pct != '' else '',
        'close_price': round(close_price, 4) if close_price > 0 else '',
        'macd_hist': round(float(macd_hist), 4) if macd_hist is not None and macd_hist != '' else '',
        'dif': round(float(dif_val), 4) if dif_val is not None and dif_val != '' else '',
        'adx': round(float(adx_val), 2) if adx_val is not None and adx_val != '' else '',
        'atr_pct': atr_pct,
    }


# ====================================================================
#  SAR 抛物线指标计算
# ====================================================================

def _compute_sar_from_klines(klines: List[List]) -> Dict:
    """从 OHLC K线数据计算最新 SAR 值和趋势方向

    基于标准抛物线 SAR 算法 (af_start=0.02, af_step=0.02, af_max=0.20)

    Parameters
    ----------
    klines : list
        OKX API 返回的K线数据，每条为 [ts, o, h, l, c, vol, ...]

    Returns
    -------
    dict
        {'sar': float, 'sar_trend': '↑'|'↓'|'--'}
    """
    if not klines or len(klines) < 2:
        return {'sar': '', 'sar_trend': '--'}

    try:
        # OKX 返回的 K 线为“最新在前”（降序），而抛物线 SAR 是有方向的递推指标，
        # 必须按时间正序（最旧在前）逐根推进，故先反转；反转后 sar[-1] 即最新值。
        klines = list(reversed(klines))
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        n = len(highs)

        sar = np.zeros(n)
        ep = np.zeros(n)  # extreme point
        af = np.zeros(n)  # acceleration factor
        trend = np.ones(n, dtype=int)  # 1=uptrend, -1=downtrend

        # 初始化
        sar[0] = lows[0]
        ep[0] = highs[0]
        af[0] = 0.02
        trend[0] = 1

        for i in range(1, n):
            # 计算 SAR
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])

            if trend[i - 1] == 1:  # 上升趋势
                # SAR 不能高于前两根K线的最低点
                low_min = min(lows[i - 1], lows[i]) if i >= 1 else lows[i]
                sar[i] = min(sar[i], low_min)

                if lows[i] < sar[i]:  # 趋势反转
                    trend[i] = -1
                    sar[i] = max(highs[i], highs[i - 1]) if i >= 1 else highs[i]
                    ep[i] = lows[i]
                    af[i] = 0.02
                else:
                    trend[i] = 1
                    if highs[i] > ep[i - 1]:
                        ep[i] = highs[i]
                        af[i] = min(af[i - 1] + 0.02, 0.20)
                    else:
                        ep[i] = ep[i - 1]
                        af[i] = af[i - 1]
            else:  # 下降趋势
                # SAR 不能低于前两根K线的最高点
                high_max = max(highs[i - 1], highs[i]) if i >= 1 else highs[i]
                sar[i] = max(sar[i], high_max)

                if highs[i] > sar[i]:  # 趋势反转
                    trend[i] = 1
                    sar[i] = min(lows[i], lows[i - 1]) if i >= 1 else lows[i]
                    ep[i] = highs[i]
                    af[i] = 0.02
                else:
                    trend[i] = -1
                    if lows[i] < ep[i - 1]:
                        ep[i] = lows[i]
                        af[i] = min(af[i - 1] + 0.02, 0.20)
                    else:
                        ep[i] = ep[i - 1]
                        af[i] = af[i - 1]

        last_sar = round(float(sar[-1]), 4)
        last_trend = '↑' if trend[-1] == 1 else '↓'
        return {'sar': last_sar, 'sar_trend': last_trend}

    except Exception as e:
        logger.warning("SAR计算失败: %s", str(e))
        return {'sar': '', 'sar_trend': '--'}


def _fetch_klines_for_sar(inst_id: str, bar: str, limit: int = 100) -> List[List]:
    """获取K线数据用于SAR计算（共享连接 + 缓存 + 自动重试）

    - 同一 (inst_id, bar) 在 _SAR_KLINE_CACHE_TTL 秒内直接复用缓存；
    - 前 2 次失败直接重试（httpx 会自动重连断开的 HTTP/2 连接），
      连续 2 次失败才重建共享客户端，且采用“替换引用”而非 close，
      避免并发线程撞上 'Cannot send a request, as the client has been closed'。
    """
    cache_key = (inst_id, bar)
    now = time.time()
    with _sar_kline_cache_lock:
        cached = _sar_kline_cache.get(cache_key)
        if cached and (now - cached[0]) < _SAR_KLINE_CACHE_TTL:
            return cached[1]

    last_err = None
    for attempt in range(1, 4):
        try:
            market_api = _get_shared_market_api()
            # 合约 instId（如 BTC-USDT-SWAP）去掉 '-SWAP' 后缀即为指数/现货 instId（BTC-USDT），
            # 供 get_index_candlesticks 使用（旧写法 replace('-SWAP','-USDT') 会得到 BTC-USDT-USDT 非法 ID，导致返回空、SAR 缺失）。
            index_inst_id = inst_id.replace('-SWAP', '')
            # 本函数已自带 3 次异常重试，只取节流与 50011 退避，不叠加两层重试
            result = _rl('market_candles', market_api.get_index_candlesticks,
                         retry_on_error=False,
                         instId=index_inst_id, bar=bar, limit=str(limit))
            if result.get('code') == '0' and result.get('data'):
                with _sar_kline_cache_lock:
                    _sar_kline_cache[cache_key] = (time.time(), result['data'])
                return result['data']
            return []
        except Exception as e:
            last_err = e
            logger.warning("获取SAR K线数据失败 [%s %s] 第%d次: %s", inst_id, bar, attempt, str(e))
            if attempt < 3:
                if attempt == 2:
                    # 连续失败才重建客户端（替换引用，不 close）
                    _reset_shared_market_api()
                time.sleep(1.0 * attempt)
    logger.warning("获取SAR K线数据失败 [%s %s] 已重试3次: %s", inst_id, bar, last_err)
    return []

def _read_csv() -> List[Dict]:
    """读取全币种行情：迁移批次7b 起 DB 优先，DB 无数据/不可用时回退 CSV 文件"""
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                rows = _market_repo.load_coin_rows(_s)
            if rows:
                return rows
        except Exception as e:
            logger.warning("从DB读取 crypto_coins 失败，回退CSV文件: %s", e)

    rows = []
    if not os.path.exists(CSV_PATH):
        logger.warning("CSV文件不存在: %s", CSV_PATH)
        return rows
    # utf-8-sig 兼容存量 BOM（避免首列名变 '\ufeffrank'）
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # 确保新增列存在（使用周期后缀命名规范）
    new_cols = ['4H_趋势', '4H_交易价格', '4H_交易时间', '4H_盈亏%', '4H_收盘价',
                '1D_趋势', '1D_交易价格', '1D_交易时间', '1D_盈亏%', '1D_收盘价',
                '1H_趋势', '1H_交易价格', '1H_交易时间', '1H_盈亏%', '1H_收盘价',
                'MACD_4H', 'DIF_4H', 'ADX_4H', 'ATR_4H', 'SAR_4H', 'SAR颜色_4H',
                'MACD_1D', 'DIF_1D', 'ADX_1D', 'ATR_1D', 'SAR_1D', 'SAR颜色_1D',
                'MACD_1H', 'DIF_1H', 'ADX_1H', 'ATR_1H', 'SAR_1H', 'SAR颜色_1H',
                '15m_趋势', '15m_交易价格', '15m_交易时间', '15m_盈亏%', '15m_收盘价',
                'MACD_15m', 'DIF_15m', 'ADX_15m', 'ATR_15m', 'SAR_15m', 'SAR颜色_15m']
    if rows:
        for col in new_cols:
            if col not in rows[0]:
                for row in rows:
                    row[col] = ''

    return rows


def _write_csv(rows: List[Dict]):
    """写入全币种行情：DB 主存 + CSV 文件双写（文件保留为兜底数据源）"""
    if not rows:
        return
    if _db_session_scope is not None and _market_repo is not None:
        try:
            with _db_session_scope() as _s:
                _market_repo.save_coin_rows(_s, rows)
        except Exception as e:
            logger.warning("写入 crypto_coins 到DB失败，仅写CSV文件: %s", e)
    fieldnames = list(rows[0].keys())
    with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CSV 已保存: %s (%d 行)", CSV_PATH, len(rows))


def read_csv_for_display() -> List[Dict]:
    """读取 CSV 数据供前端表格展示（使用周期后缀命名规范）"""
    rows = _read_csv()
    records = []
    for row in rows:
        records.append({
            '排名': str(row.get('rank', '')),
            '币种': str(row.get('symbol', '')),
            '交易对': str(row.get('inst_id', '')),
            '名称': str(row.get('name_cn', '')),
            # 15m 周期
            '15m_趋势': str(row.get('15m_趋势', '')),
            '15m_交易价格': str(row.get('15m_交易价格', '')),
            '15m_交易时间': str(row.get('15m_交易时间', '')),
            '15m_盈亏%': str(row.get('15m_盈亏%', '')),
            'MACD_15m': str(row.get('MACD_15m', '')),
            'DIF_15m': str(row.get('DIF_15m', '')),
            'ADX_15m': str(row.get('ADX_15m', '')),
            'ATR_15m': str(row.get('ATR_15m', '')),
            'SAR_15m': str(row.get('SAR_15m', '')),
            'SAR颜色_15m': str(row.get('SAR颜色_15m', '')),
            # 1H 周期
            '1H_趋势': str(row.get('1H_趋势', '')),
            '1H_交易价格': str(row.get('1H_交易价格', '')),
            '1H_交易时间': str(row.get('1H_交易时间', '')),
            '1H_盈亏%': str(row.get('1H_盈亏%', '')),
            'MACD_1H': str(row.get('MACD_1H', '')),
            'DIF_1H': str(row.get('DIF_1H', '')),
            'ADX_1H': str(row.get('ADX_1H', '')),
            'ATR_1H': str(row.get('ATR_1H', '')),
            'SAR_1H': str(row.get('SAR_1H', '')),
            'SAR颜色_1H': str(row.get('SAR颜色_1H', '')),
            # 4H 周期
            '4H_趋势': str(row.get('4H_趋势', '')),
            '4H_交易价格': str(row.get('4H_交易价格', '')),
            '4H_交易时间': str(row.get('4H_交易时间', '')),
            '4H_盈亏%': str(row.get('4H_盈亏%', '')),
            'MACD_4H': str(row.get('MACD_4H', '')),
            'DIF_4H': str(row.get('DIF_4H', '')),
            'ADX_4H': str(row.get('ADX_4H', '')),
            'ATR_4H': str(row.get('ATR_4H', '')),
            'SAR_4H': str(row.get('SAR_4H', '')),
            'SAR颜色_4H': str(row.get('SAR颜色_4H', '')),
            # 1D 周期
            '1D_趋势': str(row.get('1D_趋势', '')),
            '1D_交易价格': str(row.get('1D_交易价格', '')),
            '1D_交易时间': str(row.get('1D_交易时间', '')),
            '1D_盈亏%': str(row.get('1D_盈亏%', '')),
            'MACD_1D': str(row.get('MACD_1D', '')),
            'DIF_1D': str(row.get('DIF_1D', '')),
            'ADX_1D': str(row.get('ADX_1D', '')),
            'ATR_1D': str(row.get('ATR_1D', '')),
            'SAR_1D': str(row.get('SAR_1D', '')),
            'SAR颜色_1D': str(row.get('SAR颜色_1D', '')),
        })
    return records


def get_filtered_records() -> List[Dict]:
    """获取筛选后的一致性币种记录（4H 和 1D 趋势相同）"""
    all_records = read_csv_for_display()
    filtered = []
    for r in all_records:
        trend_4h = r.get('4H_趋势', '')
        trend_1d = r.get('1D_趋势', '')
        # 两个周期都为"上涨"或都为"下跌"
        if trend_4h in ('上涨', '下跌') and trend_4h == trend_1d:
            filtered.append(r)
    return filtered


# ====================================================================
#  批量趋势分析器
# ====================================================================

class BatchTrendAnalyzer:
    """批量多周期趋势分析器

    使用示例::

        analyzer = BatchTrendAnalyzer()
        analyzer.run()              # 同步运行
        # 或
        analyzer.run_in_background()  # 后台线程运行
        progress = get_progress()     # 查询进度
    """

    def __init__(self):
        global _progress
        self.progress = _progress

    def _process_single_symbol(self, inst_id: str, on_step=None) -> Dict:
        """处理单个币种，返回各周期（_BATCH_BARS：15m/1H/4H/1D）的趋势+技术指标信息

        Parameters
        ----------
        inst_id : str
            币种交易对ID，如 'BTC-USDT-SWAP'
        on_step : callable, optional
            每完成一个周期后的回调，签名为 on_step(bar)，用于细化进度展示

        Returns
        -------
        dict
            {'15m': {...}, '1H': {...}, '4H': {...}, '1D': {...}}（失败时额外带 'error' 键）
        """
        from real_strategy_adapter import calculate_single_coin_data

        try:
            infos = {}
            for bar in _BATCH_BARS:
                result = calculate_single_coin_data(inst_id, bar=bar, max_retries=2)
                infos[bar] = _extract_trend_info(result)
                if on_step:
                    on_step(bar)

            # 计算 SAR（各周期独立）：SAR < 收盘价 → 绿色（看涨），SAR > 收盘价 → 红色（看跌）
            for bar in _BATCH_BARS:
                info = infos[bar]
                klines = _fetch_klines_for_sar(inst_id, bar, limit=100)
                sar_result = _compute_sar_from_klines(klines)
                info['sar'] = sar_result['sar']
                close_p = info.get('close_price', 0)
                try:
                    sar_val = float(info['sar']) if info.get('sar') != '' else None
                    cp = float(close_p) if close_p != '' and close_p != 0 else None
                    if sar_val is not None and cp is not None and cp > 0:
                        info['sar_color'] = 'green' if sar_val < cp else 'red'
                    else:
                        info['sar_color'] = ''
                except (ValueError, TypeError):
                    info['sar_color'] = ''

            return {bar: infos[bar] for bar in _BATCH_BARS}
        except Exception as e:
            logger.warning("[%s] 批量分析失败: %s", inst_id, str(e))
            empty_info = {
                'trend': '错误', 'trend_flag': 'error',
                'trade_price': '', 'trade_time': '', 'profit_pct': '',
                'close_price': '',
                'macd_hist': '', 'dif': '', 'adx': '', 'atr_pct': '',
                'sar': '', 'sar_color': '',
            }
            failed = {bar: dict(empty_info) for bar in _BATCH_BARS}
            failed['error'] = str(e)
            return failed

    def run(self, callback=None) -> Dict:
        """同步运行批量分析（并发处理）

        使用共享 K 线客户端 + 最多 _BATCH_WORKERS 个并发 worker 处理币种，
        总耗时约为串行的 1/3；进度状态线程安全，可按币种/周期粒度更新。

        Parameters
        ----------
        callback : callable, optional
            每处理完一个币种后的回调函数，签名为 callback(progress_dict)。

        Returns
        -------
        dict
            {'total': int, 'success': int, 'error': int}
        """
        rows = _read_csv()
        total = len(rows)

        self.progress.start(total)
        success_count = 0
        error_count = 0
        completed = 0

        def _on_step(bar, cur_symbol):
            """每完成一个周期更新一次进度消息，避免长时间无反馈"""
            self.progress.update(
                current=completed,
                symbol=cur_symbol,
                message="处理中: %d/%d — %s [%s]" % (completed, total, cur_symbol, bar),
            )

        def _process_one(idx_row):
            """单个币种处理（在 worker 线程执行）"""
            idx, row = idx_row
            inst_id = row.get('inst_id', '')
            symbol = row.get('symbol', inst_id)
            self.progress.update(
                current=completed,
                symbol=symbol,
                message="处理中: %d/%d — %s" % (completed, total, symbol),
            )
            info = self._process_single_symbol(
                inst_id, on_step=lambda bar: _on_step(bar, symbol)
            )
            return idx, symbol, info

        with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as pool:
            future_to_idx = {
                pool.submit(_process_one, (idx, row)): idx
                for idx, row in enumerate(rows)
            }
            for future in as_completed(future_to_idx):
                idx, symbol, info = future.result()
                completed += 1

                if info and 'error' not in info:
                    row = rows[idx]
                    # 各周期字段写入（列名规范：趋势/价格/时间/盈亏/收盘 用 '{bar}_字段'，
                    # 技术指标 MACD/DIF/ADX/ATR/SAR/SAR颜色 用 '字段_{bar}'）
                    for bar in _BATCH_BARS:
                        info_bar = info[bar]
                        row['%s_趋势' % bar] = info_bar['trend']
                        row['%s_交易价格' % bar] = str(info_bar['trade_price']) if info_bar['trade_price'] != '' else ''
                        row['%s_交易时间' % bar] = info_bar['trade_time']
                        row['%s_盈亏%%' % bar] = str(info_bar['profit_pct']) if info_bar['profit_pct'] != '' else ''
                        row['%s_收盘价' % bar] = str(info_bar['close_price']) if info_bar['close_price'] != '' else ''
                        row['MACD_%s' % bar] = str(info_bar['macd_hist']) if info_bar['macd_hist'] != '' else ''
                        row['DIF_%s' % bar] = str(info_bar['dif']) if info_bar['dif'] != '' else ''
                        row['ADX_%s' % bar] = str(info_bar['adx']) if info_bar['adx'] != '' else ''
                        row['ATR_%s' % bar] = str(info_bar['atr_pct']) if info_bar['atr_pct'] != '' else ''
                        row['SAR_%s' % bar] = str(info_bar.get('sar', '')) if info_bar.get('sar', '') != '' else ''
                        row['SAR颜色_%s' % bar] = str(info_bar.get('sar_color', '')) if info_bar.get('sar_color', '') else ''
                    success_count += 1
                else:
                    error_count += 1
                    logger.error("[%s] 处理失败", symbol)

                self.progress.update(
                    current=completed,
                    success=success_count,
                    error=error_count,
                )

                # 每处理 5 个或最后一个时保存 CSV
                if completed % 5 == 0 or completed == total:
                    _write_csv(rows)

                if callback:
                    callback(self.progress.to_dict())

        self.progress.finish()

        return {
            'total': total,
            'success': success_count,
            'error': error_count,
        }

    def run_in_background(self) -> threading.Thread:
        """后台线程运行批量分析

        Returns
        -------
        threading.Thread
            已启动的后台线程
        """
        thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="batch-trend-analyzer",
        )
        thread.start()
        return thread


# ====================================================================
#  命令行入口
# ====================================================================

def main():
    """命令行直接运行"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    print("=" * 70)
    print("  加密货币批量多周期趋势分析")
    print("=" * 70)

    analyzer = BatchTrendAnalyzer()
    result = analyzer.run(callback=lambda p: print(
        "\r  进度: %s/%s (%.1f%%) — %s | 成功:%s 失败:%s   " % (
            p['current'], p['total'], p['progress_pct'],
            p['current_symbol'], p['success'], p['error'],
        ), end='', flush=True
    ))

    print("\n")
    print("  总计: %d  成功: %d  失败: %d" % (
        result['total'], result['success'], result['error']))

    # 打印前 10 条结果
    records = read_csv_for_display()
    filtered = [r for r in records
                if r['4H_趋势'] in ('上涨', '下跌')
                and r['4H_趋势'] == r['1D_趋势']]
    print("\n  双周期一致性币种 (%d 个):" % len(filtered))
    for r in filtered:
        print("    %-5s %-8s  4H: %s(%s%%)  1D: %s(%s%%)" % (
            r['币种'], r['名称'],
            r['4H_趋势'], r['4H_盈亏%'] or '--',
            r['1D_趋势'], r['1D_盈亏%'] or '--',
        ))


if __name__ == '__main__':
    main()
