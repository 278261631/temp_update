#!/usr/bin/env python3
"""GY1 天区索引：日期 -> [天区列表]，以多行紧凑 JSON 保存。"""

import json
import logging
import os
from typing import Dict

from .config import BASE_DIR

INDEX_PATH = os.path.join(BASE_DIR, "data", "gy1_region_index.json")

logger = logging.getLogger(__name__)


def load_gy1_index(path: str = INDEX_PATH) -> Dict[str, list]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("读取 GY1 索引文件失败，将使用空索引: %s", e)
    return {}


def save_gy1_index(index: Dict[str, list], path: str = INDEX_PATH) -> None:
    """写回索引：大括号单独一行，每个日期一行，按日期排序。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{\n")
            keys = sorted(index.keys())
            for i, k in enumerate(keys):
                line = "  " + json.dumps(str(k), ensure_ascii=False) + ":" + \
                    json.dumps(index[k], ensure_ascii=False, separators=(",", ":"))
                if i < len(keys) - 1:
                    line += ","
                f.write(line + "\n")
            f.write("}")
    except Exception as e:
        logger.warning("写入 GY1 索引文件失败: %s", e)
