#!/usr/bin/env python3
"""模板预览：生成 asinh 拉伸的 PNG（可直接用于缩放/平移查看）。"""

import os
import tempfile

import numpy as np
from astropy.io import fits
from astropy.visualization import AsinhStretch, ImageNormalize
from PySide6.QtGui import QImage


def _load_data(path: str) -> np.ndarray:
    try:
        hdul = fits.open(path, memmap=True)
    except Exception:
        hdul = fits.open(path, memmap=False)
    with hdul:
        return np.asarray(hdul[0].data, dtype="float32")


def asinh_png(fits_path: str, out_png: str = None, max_size: int = 2000,
              a: float = 0.1, low: float = 1.0, high: float = 99.5) -> str:
    """读取 FITS，做 asinh 拉伸，写出 8-bit 灰度 PNG，返回 PNG 路径。"""
    data = _load_data(fits_path)
    finite = np.isfinite(data)
    if not finite.any():
        raise ValueError("模板数据全部为非有限值")
    vals = data[finite]
    vmin = float(np.percentile(vals, low))
    vmax = float(np.percentile(vals, high))
    if not (vmax > vmin):
        vmax = vmin + 1.0

    ny, nx = data.shape
    factor = max(1, int(np.ceil(max(ny, nx) / float(max_size))))
    ds = data[::factor, ::factor] if factor > 1 else data
    ds = np.where(np.isfinite(ds), ds, vmin)

    norm = ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(a=a))
    img = (np.clip(norm(ds), 0.0, 1.0) * 255.0).astype(np.uint8)
    img = np.ascontiguousarray(img)
    h, w = img.shape

    qimg = QImage(img.data, w, h, img.strides[0], QImage.Format_Grayscale8).copy()
    if out_png is None:
        out_png = os.path.join(tempfile.gettempdir(),
                               os.path.splitext(os.path.basename(fits_path))[0] + ".asinh.png")
    qimg.save(out_png, "PNG")
    return out_png
