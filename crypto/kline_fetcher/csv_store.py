# -*- coding: utf-8 -*-
"""
CSV 存储模块
============

负责 K 线数据在本地 CSV 文件中的所有读写操作：

  - 表头初始化            write_header_if_needed
  - 快速读取末尾时间戳    tail_last_timestamp
  - 加载全部已有时间戳    load_existing_timestamps
  - 按 timestamp 去重追加 append_rows_dedup
  - 从 CSV 读取完整行     load_rows

CSV 列结构固定为：
    timestamp, datetime, open, high, low, close, volume, exchange
其中 timestamp 为毫秒级 UTC 时间戳，作为唯一去重键。
"""

import csv
import datetime
import os
from typing import Dict, List, Set, Tuple

# CSV 表头（所有存储文件统一使用）
CSV_HEADER = [
    "timestamp", "datetime", "open", "high", "low", "close", "volume", "exchange"
]


def write_header_if_needed(path: str) -> None:
    """文件不存在或为空时写入统一表头。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def tail_last_timestamp(path: str) -> int:
    """
    读取文件末尾一段，快速定位「最后一行的 timestamp」（毫秒）。

    仅用于轻量续传判断；不做全量去重（全量请用 load_existing_timestamps）。
    文件不存在或无有效数据行时返回 None。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            offset = max(end - 8192, 0)
            f.seek(offset)
            chunk = f.read().decode("utf-8", errors="ignore")
            lines = [ln for ln in chunk.splitlines() if ln.strip()]
            for line in reversed(lines):
                parts = line.split(",")
                if parts:
                    try:
                        return int(parts[0])
                    except Exception:
                        continue
    except FileNotFoundError:
        return None
    return None


def load_existing_timestamps(path: str) -> Tuple[Set[int], List[int]]:
    """
    全量加载 CSV 中的 timestamp。

    返回:
        seen    : set，用于 O(1) 去重判断
        ordered : list，升序排列的全部 timestamp，用于缺口检测
    """
    seen: Set[int] = set()
    ordered: List[int] = []
    if not os.path.exists(path):
        return seen, ordered
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for row in reader:
            if not row:
                continue
            try:
                ts = int(row[0])
            except Exception:
                continue
            if ts not in seen:
                seen.add(ts)
                ordered.append(ts)
    ordered.sort()
    return seen, ordered


def append_rows_dedup(
    path: str, exchange_id: str, rows: List[List], seen_ts: Set[int]
) -> int:
    """
    追加写入 K 线行，按 timestamp 去重（已存在则跳过）。

    参数:
        rows      : 每个元素为 ccxt 原始结构 [ts, open, high, low, close, volume]
        seen_ts   : 已存在时间戳集合，写入后会同步更新，供后续批次去重
    返回实际追加写入的行数。
    """
    appended = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            ts = r[0]
            if ts in seen_ts:
                continue
            dt = datetime.datetime.fromtimestamp(
                ts / 1000, datetime.timezone.utc
            ).isoformat()
            w.writerow([ts, dt, r[1], r[2], r[3], r[4], r[5], exchange_id])
            seen_ts.add(ts)
            appended += 1
    return appended


def load_rows(path: str) -> List[Dict]:
    """读取整个 CSV 为字典列表（供测试/校验使用）。"""
    rows: List[Dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows
