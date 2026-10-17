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
    """返回 (默认system_name, targets)。

    支持**每个天区自带系统前缀**（如 GY4_K092-4, GY5_K092-7），逐项解析：
    targets 元素含 system / k_number / suffix / k_full。
    去重按 (system, k_full)，因此不同系统的同名天区都会保留。
    """
    default_system = (system_name or "").strip().upper()
    first_system = default_system
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

        m_sys = re.search(r"(GY\d+)", token, re.IGNORECASE)
        sys_name = m_sys.group(1).upper() if m_sys else default_system
        key = (sys_name, k_full)
        if key in seen:
            continue
        seen.add(key)

        targets.append({"system": sys_name, "k_number": k_number, "suffix": suffix, "k_full": k_full})
        if not first_system and sys_name:
            first_system = sys_name

    return first_system, targets


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
