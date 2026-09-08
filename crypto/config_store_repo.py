#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置/缓存整份存取层（迁移批次7a + 批次8）
==========================================
"整份读写型" JSON 存入通用 kv_store 表（value 为 JSON 文本）：

    key='strategy_config'      task/config/config_trend_range.json（实盘交易策略配置）
    key='coin_selection'       config.json（币种自选 all_coins/starred/floating）
    key='instrument_spec_cache' 合约规格缓存（24h TTL，可重建）        ← 批次8
    key='market_scan_cache'     市场扫描榜单缓存（可重建）             ← 批次8

API：
    load_json_config(session, key)   读取整份配置（不存在/损坏 → None）
    load_json_config_cached(key)     带 TTL 的缓存读（高频读路径推荐）
    save_json_config(session, key, data)   整份覆盖写入（写穿透失效缓存）
    delete_json_config(session, key)   删除（测试清场用，同步失效缓存）
    invalidate_config_cache(key)     手工失效缓存（外部直接改库后调用）

调用方约定（与 JSON 版一致的容错语义）：
- 读：DB 优先，DB 不可用/无数据时回退本地文件，保证交易定时任务不受影响
- 写：DB 主存 + 本地文件双写，文件保留为兜底数据源
"""

import json
import copy
import time
import threading

from .models import KVStore

KEY_STRATEGY_CONFIG = 'strategy_config'
KEY_COIN_SELECTION = 'coin_selection'
KEY_INSTRUMENT_SPEC_CACHE = 'instrument_spec_cache'
KEY_MARKET_SCAN_CACHE = 'market_scan_cache'
# 智能减仓残留仓记录（key='instId|bucket|direction'，value=减仓快照）：
# 供 /api/task/residual-positions 接口查询，方便人工扭亏为盈操作
KEY_SMART_REDUCE_RESIDUALS = 'smart_reduce_residuals'
# 定时任务历史币种库：{instId: {first_used, last_used, use_count}}
# 每次保存交易配置时 upsert；从配置移除的币种不删除（变为"已停用"留存）
KEY_TASK_COIN_LIBRARY = 'task_coin_history_library'
# 实盘调度器「期望运行状态」（desired_running/account/auto_resume）：
# 供进程重启后判断是否该自动拉起实盘，详见 trading_runtime_repo.py
KEY_TRADING_RUNTIME = 'trading_runtime'


def load_json_config(session, key: str):
    """读取整份配置 JSON；键不存在或 value 非合法 JSON 时返回 None"""
    kv = session.get(KVStore, key)
    if kv is None or not kv.value:
        return None
    try:
        data = json.loads(kv.value)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# =============================================================================
# 进程内 TTL 缓存：配置类数据变更频率极低，高频读路径无需每请求打库。
# 单进程 Flask 部署下内存缓存即可；将来多 worker 时可无缝替换为 Redis。
# =============================================================================
_kv_cache = {}          # key -> (monotonic 时间戳, 配置 dict)
_kv_cache_lock = threading.Lock()
_KV_CACHE_TTL = 30.0    # 秒；兼顾数据新鲜度与 DB 压力（外部直改库最晚 30s 生效）


def load_json_config_cached(key: str, ttl: float = None):
    """带 TTL 的整份配置读取；TTL 内直接命中内存，无 DB 往返。

    返回深拷贝，防止调用方修改返回值污染缓存（与原每次 json.loads
    返回新对象的契约一致）；DB 不可用时返回 None，由调用方文件兜底。
    """
    now = time.monotonic()
    with _kv_cache_lock:
        ent = _kv_cache.get(key)
        if ent is not None and now - ent[0] < (ttl if ttl is not None else _KV_CACHE_TTL):
            return copy.deepcopy(ent[1])
    from .database import session_scope
    try:
        with session_scope() as session:
            data = load_json_config(session, key)
    except Exception:
        return None
    with _kv_cache_lock:
        _kv_cache[key] = (now, data)
    return copy.deepcopy(data)


def invalidate_config_cache(key: str = None):
    """失效缓存；key 为 None 时清空全部（外部绕过 repo 直改库后需调用）"""
    with _kv_cache_lock:
        if key is None:
            _kv_cache.clear()
        else:
            _kv_cache.pop(key, None)


def save_json_config(session, key: str, data: dict):
    """整份覆盖写入（utf-8 原文，中文不转义，与原文件保持一致）；
    写穿透失效对应缓存键，后续读回源 DB 重建"""
    text = json.dumps(data, ensure_ascii=False)
    kv = session.get(KVStore, key)
    if kv is None:
        session.add(KVStore(key=key, value=text))
    else:
        kv.value = text
    invalidate_config_cache(key)


def delete_json_config(session, key: str):
    """删除指定配置键（不存在时静默），同步失效缓存"""
    kv = session.get(KVStore, key)
    if kv is not None:
        session.delete(kv)
    invalidate_config_cache(key)
