#!/usr/bin/env python3
"""解析用户输入的目标系统与天区列表。

输入示例：
    system_name = "GY6"
    text        = "GY6_K002-8,GY6_K002-9"
也接受不含系统前缀的形式： "K002-8, K002-9" 或逐行填写。
"""

import re
from typing import Dict, List, Tuple


def parse_targets(system_name: str, text: str) -> Tuple[str, List[Dict[str, str]]]:
    """返回 (system_name, targets)。

    targets 元素含 k_number / suffix / k_full，例如 K002 / 8 / K002-8。
    """
    system = (system_name or "").strip().upper()
    targets: List[Dict[str, str]] = []
    seen = set()

    for token in re.split(r"[,;\s]+", text or ""):
        token = token.strip()
        if not token:
            continue
        m = re.search(r"(K\d{3})-(\d+)", token, re.IGNORECASE)
        if not m:
            continue
        k_number = m.group(1).upper()
        suffix = m.group(2)
        k_full = f"{k_number}-{suffix}"
        if k_full in seen:
            continue
        seen.add(k_full)
        targets.append({"k_number": k_number, "suffix": suffix, "k_full": k_full})
        if not system:
            m_sys = re.search(r"(GY\d+)", token, re.IGNORECASE)
            if m_sys:
                system = m_sys.group(1).upper()

    return system, targets


def find_recent_dates_for_region(index: Dict[str, list], region: str, max_count: int) -> List[str]:
    """在索引（日期 -> 天区列表）中查找包含指定天区的最近若干日期。"""
    region_upper = str(region).upper()
    result: List[str] = []
    for date in sorted(index.keys(), reverse=True):
        regions = index.get(date) or []
        try:
            if region_upper in [str(r).upper() for r in regions]:
                result.append(date)
                if len(result) >= max_count:
                    break
        except Exception:
            continue
    return result
