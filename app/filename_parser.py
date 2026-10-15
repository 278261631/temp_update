#!/usr/bin/env python3
"""FITS 文件名解析器：从文件名中提取望远镜名称与天区编号。"""

import os
import re
from typing import Dict, Optional


class FITSFilenameParser:
    def __init__(self):
        self.patterns = [
            # GY5_K053-1_No Filter_60S_Bin2_UTC20250622_182433_-14.9C_.fit
            r"(?P<tel_name>GY[1-6])_(?P<k_number>K\d{3})-?(?P<k_suffix>\d*)",
            # download_GY5_20250718_K096_001.fits
            r"download_(?P<tel_name>GY[1-6])_(?P<date>\d{8})_(?P<k_number>K\d{3})_(?P<sequence>\d+)",
            # GY5_20250718_K096_filename.fits
            r"(?P<tel_name>GY[1-6])_(?P<date>\d{8})_(?P<k_number>K\d{3})",
            # GY5_K096_xxx.fits
            r"(?P<tel_name>GY[1-6])_(?P<k_number>K\d{3})",
        ]
        self.compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.patterns]

    def parse_filename(self, filename: str) -> Optional[Dict[str, str]]:
        base_name = os.path.splitext(os.path.basename(filename))[0]
        for i, pattern in enumerate(self.compiled_patterns):
            match = pattern.search(base_name)
            if match:
                result = match.groupdict()
                result["pattern_index"] = i
                result["original_filename"] = filename
                result["base_name"] = base_name
                self._normalize_result(result)
                return result
        return None

    @staticmethod
    def _normalize_result(result: Dict[str, str]) -> None:
        if "tel_name" in result and result["tel_name"]:
            result["tel_name"] = result["tel_name"].upper()
        if "k_number" in result and result["k_number"]:
            k_num = result["k_number"].upper()
            result["k_number"] = k_num
        if result.get("k_suffix"):
            result["k_full"] = f"{result['k_number']}-{result['k_suffix']}"
        else:
            result["k_full"] = result.get("k_number", "")
