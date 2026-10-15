#!/usr/bin/env python3
"""ASTAP 处理器：根据文件名中的天区编号匹配坐标并执行 ASTAP 解析。"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import AppConfig
from .filename_parser import FITSFilenameParser


class ASTAPProcessor:
    def __init__(self, config: AppConfig):
        self.config = config
        self.filename_parser = FITSFilenameParser()
        self.logger = logging.getLogger(self.__class__.__name__)

    def extract_k_full_from_filename(self, filename: str) -> Optional[str]:
        parsed = self.filename_parser.parse_filename(filename)
        if parsed and parsed.get("k_full"):
            return parsed["k_full"]
        match = re.search(r"K(\d{3})-(\d)", filename)
        if match:
            return f"K{match.group(1)}-{match.group(2)}"
        return None

    def get_coordinates_for_region(self, k_full: str) -> Optional[Tuple[float, float]]:
        region_data = self.config.get_region_data()
        coords = region_data.get(k_full)
        if isinstance(coords, list) and len(coords) >= 2:
            return float(coords[0]), float(coords[1])
        self.logger.warning("未找到天区 %s 的坐标数据", k_full)
        return None

    def generate_astap_command(self, fits_file_path: str, ra: float, dec: float) -> Optional[str]:
        template = self.config.get_astap_cmd_template()
        if not template:
            self.logger.error("配置缺少 astap_cmd_template")
            return None
        astap_spd = dec + 90
        return template.format(
            astap_exe=self.config.get_astap_exe(),
            astap_ra=ra,
            astap_spd=astap_spd,
            fits_file_path=fits_file_path,
        )

    def execute_astap_command(self, command: str) -> bool:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return True
            self.logger.error("ASTAP 命令执行失败，返回码: %s", result.returncode)
            if result.stderr:
                self.logger.error("错误信息: %s", result.stderr)
            return False
        except subprocess.TimeoutExpired:
            self.logger.error("ASTAP 命令执行超时")
            return False
        except Exception as e:
            self.logger.error("执行 ASTAP 命令时出错: %s", e)
            return False

    def process_fits_file(self, fits_file_path: str) -> bool:
        try:
            if not os.path.exists(fits_file_path):
                self.logger.error("FITS 文件不存在: %s", fits_file_path)
                return False

            # 已有 WCS/ASTAP 结果则跳过
            try:
                from astropy.io import fits
                with fits.open(fits_file_path) as hdul:
                    hdr = hdul[0].header
                    has_wcs = ("CRVAL1" in hdr and "CRVAL2" in hdr) or ("ASTAP" in hdr or "ASTAP0" in hdr)
                if has_wcs:
                    return True
            except Exception:
                pass

            k_full = self.extract_k_full_from_filename(os.path.basename(fits_file_path))
            if not k_full:
                self.logger.error("无法从文件名提取天区编号: %s", fits_file_path)
                return False

            coordinates = self.get_coordinates_for_region(k_full)
            if not coordinates:
                self.logger.error("无法获取天区 %s 的坐标（请在 url_config.json 的 regionData 中补充）", k_full)
                return False

            ra, dec = coordinates
            command = self.generate_astap_command(fits_file_path, ra, dec)
            if not command:
                return False
            return self.execute_astap_command(command)
        except Exception as e:
            self.logger.error("处理 FITS 文件时出错: %s", e)
            return False

    def process_directory(self, directory_path: str, progress_callback=None) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        if not os.path.isdir(directory_path):
            self.logger.error("目录不存在: %s", directory_path)
            return results

        fits_files = []
        for ext in (".fits", ".fit", ".fts"):
            fits_files.extend(Path(directory_path).glob(f"*{ext}"))
            fits_files.extend(Path(directory_path).glob(f"*{ext.upper()}"))
        fits_files = sorted(set(fits_files))
        if not fits_files:
            return results

        total = len(fits_files)
        for idx, fits_file in enumerate(fits_files, 1):
            path = str(fits_file)
            success = self.process_fits_file(path)
            results[path] = success
            if progress_callback is not None:
                try:
                    progress_callback(idx, total, path, success)
                except Exception:
                    pass
        return results
