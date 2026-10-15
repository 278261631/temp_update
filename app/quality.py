#!/usr/bin/env python3
"""FITS 图像质量分析：FWHM / 椭圆度 / 5σ 限制星等。"""

import logging
from typing import Optional

import numpy as np
import sep


class FITSQualityAnalyzer:
    """基于 SEP 的图像质量分析器（移植自 fits_checking.fits_monitor）。"""

    def __init__(self, use_central_region: bool = True, central_region_size: int = 200,
                 min_image_size: int = 300):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.use_central_region = use_central_region
        self.central_region_size = central_region_size
        self.min_image_size = min_image_size

    def extract_central_region(self, image_data: np.ndarray):
        try:
            height, width = image_data.shape
            if not self.use_central_region:
                return self._contiguous(image_data), False, (width, height)
            if height < self.min_image_size or width < self.min_image_size:
                return self._contiguous(image_data), False, (width, height)
            if height < self.central_region_size or width < self.central_region_size:
                return self._contiguous(image_data), False, (width, height)

            center_y, center_x = height // 2, width // 2
            half = self.central_region_size // 2
            region = image_data[center_y - half:center_y + half, center_x - half:center_x + half].copy()
            return self._contiguous(region), True, (width, height)
        except Exception as e:
            self.logger.error("抽取中央区域时出错: %s", e)
            return image_data, False, image_data.shape

    @staticmethod
    def _contiguous(data: np.ndarray) -> np.ndarray:
        return data if data.flags["C_CONTIGUOUS"] else np.ascontiguousarray(data)

    def calculate_quality_metrics(self, image_data: np.ndarray) -> Optional[dict]:
        try:
            analysis_data, is_extracted, original_size = self.extract_central_region(image_data)
            bkg = sep.Background(analysis_data)
            data_sub = analysis_data - bkg.back()
            objects = sep.extract(data_sub, thresh=3.0, err=bkg.globalrms)

            region_name = "central_%dx%d" % (self.central_region_size, self.central_region_size) \
                if is_extracted else "full_image"

            if len(objects) == 0:
                return {
                    "n_sources": 0,
                    "fwhm": np.nan,
                    "ellipticity": np.nan,
                    "lm5sig": np.nan,
                    "background_mean": float(np.mean(bkg.back())),
                    "background_rms": float(bkg.globalrms),
                    "analysis_region": region_name,
                }

            return {
                "n_sources": len(objects),
                "fwhm": self.calculate_fwhm(objects),
                "ellipticity": self.calculate_ellipticity(objects),
                "lm5sig": self.calculate_limiting_magnitude(bkg.globalrms),
                "background_mean": float(np.mean(bkg.back())),
                "background_rms": float(bkg.globalrms),
                "analysis_region": region_name,
                "original_size": f"{original_size[0]}x{original_size[1]}",
            }
        except Exception as e:
            self.logger.error("计算质量指标时出错: %s", e)
            return None

    @staticmethod
    def calculate_fwhm(objects) -> float:
        a = objects["a"]
        b = objects["b"]
        mask = (a > 0) & (b > 0) & (a < 50) & (b < 50)
        if np.sum(mask) == 0:
            return np.nan
        geometric_mean_radius = np.sqrt(a[mask] * b[mask])
        return float(np.median(2.355 * geometric_mean_radius))

    @staticmethod
    def calculate_ellipticity(objects) -> float:
        a = objects["a"]
        b = objects["b"]
        mask = (a > 0) & (b > 0) & (a >= b)
        if np.sum(mask) == 0:
            return np.nan
        return float(np.median(1.0 - (b[mask] / a[mask])))

    @staticmethod
    def calculate_limiting_magnitude(background_rms: float, zeropoint: float = 25.0) -> float:
        threshold_5sigma = 5.0 * background_rms
        aperture_area = np.pi * 3.0 ** 2
        flux_5sigma = threshold_5sigma * np.sqrt(aperture_area)
        if flux_5sigma > 0:
            return float(zeropoint - 2.5 * np.log10(flux_5sigma))
        return np.nan
