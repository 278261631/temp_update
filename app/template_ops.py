#!/usr/bin/env python3
"""天区收集 / 升级模板 的核心逻辑（与 GUI 解耦，便于测试）。"""

import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from .config import AppConfig
from .region_scanner import RegionScanner
from .fits_scanner import WebFitsScanner, DirectoryScanner
from .downloader import FitsDownloader
from .region_index import load_gy1_index, save_gy1_index
from .targets import parse_targets, find_recent_dates_for_region

LogFn = Callable[[str], None]
StatusFn = Callable[[str, str], None]


def _noop_log(_msg: str) -> None:
    pass


def _noop_status(_msg: str, _color: str = "black") -> None:
    pass


def format_bytes(num) -> str:
    if num is None:
        return "未知"
    try:
        num = float(num)
    except Exception:
        return str(num)
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"


@dataclass
class UpdateSettings:
    recent_days: int = 3
    min_images: int = 30
    preprocess_threads: int = 20
    lm_thres: float = 15.0
    ell_thres: float = 0.2
    fwhm_thres: float = 10.0
    swarp_threads: int = 0  # SWarp 线程数（0=自动）
    swarp_resampling: str = "BILINEAR"  # SWarp 重采样核
    swarp_combine: str = "MEDIAN"  # SWarp 合成方式: MEDIAN(中值,推荐) / AVERAGE(平均)
    add_constant_sigma: float = 5.0  # 叠加后加常数 C=k·σ(背景) 并截断负值到0（0=关闭）
    nan_fill: str = "keep"  # 无覆盖(NaN)像素填充: keep/背景常数(bg)/zero
    min_frames: int = 0  # 只保留被 ≥min_frames 帧覆盖的像素（0=关闭；设为≥帧数即要求全部帧，复刻原版干净观感）
    overwrite_templates: bool = False  # 输出模板已存在时是否覆盖重生成


def collect_regions(config: AppConfig, start: str, end: str,
                    log: LogFn = _noop_log, status: StatusFn = _noop_status) -> dict:
    """按日期范围收集 GY1 天区：优先读取本地缓存，缺失时才扫描远端。"""
    scanner = RegionScanner()
    index = load_gy1_index()
    total = from_cache = scanned = 0

    dt = datetime.strptime(start, "%Y%m%d")
    end_dt = datetime.strptime(end, "%Y%m%d")
    one_day = timedelta(days=1)

    while dt <= end_dt:
        date_str = dt.strftime("%Y%m%d")
        total += 1
        regions = index.get(date_str)
        if isinstance(regions, list) and regions:
            from_cache += 1
            log(f"[GY1][{date_str}] 本地已有缓存，跳过扫描。")
        else:
            try:
                base_url = config.build_base_url("GY1", date_str)
                scanned_regions = scanner.scan_available_regions(base_url)
                normalized = sorted({str(r).upper() for r in scanned_regions if r})
                index[date_str] = normalized
                scanned += 1
                log(f"[GY1][{date_str}] 扫描完成，{len(normalized)} 个天区 -> 已缓存。")
                time.sleep(0.5)  # 保护服务器
            except Exception as e:
                log(f"[GY1][{date_str}] 扫描失败: {e}")
        status(f"进度: {from_cache + scanned}/{total} 天；缓存: {from_cache} 天；扫描: {scanned} 天", "green")
        dt += one_day

    save_gy1_index(index)
    status(f"完成：合计{total}天，其中缓存{from_cache}天，扫描{scanned}天。", "green")
    return index


class TemplateEngine:
    """下载 -> 预处理 -> 生成模板 的成套操作。"""

    def __init__(self, config: AppConfig, settings: UpdateSettings,
                 targets_text: str, workspace_root: str,
                 log: LogFn = _noop_log, status: StatusFn = _noop_status):
        self.config = config
        self.settings = settings
        self.targets_text = targets_text
        self.workspace_root = workspace_root
        self.update_root = os.path.join(workspace_root, "update")
        self.output_root = os.path.join(workspace_root, "output")
        self.bad_dir = os.path.join(workspace_root, "bad_img")
        self.log = log
        self.status = status

    # ---------- 通用 ----------
    def _load_targets(self):
        system_name, problems = parse_targets("", self.targets_text)
        if not system_name:
            self.status("无法识别系统名称，请在天区中带上系统前缀（例如 GY6_K002-8）。", "red")
            return None, None
        tel = system_name.lower()
        self.update_root = os.path.join(self.workspace_root, "update", tel)
        self.output_root = os.path.join(self.workspace_root, "output", tel)
        self.cropped_root = os.path.join(self.workspace_root, "cropped", tel)
        return system_name, problems

    # QHY600(IMX455) 满幅 9600x6422 -> 有效 9576x6388：左 24 列 + 末端 34 行光学黑/dummy
    OB_CROP_COLS_BIN1 = 24
    OB_CROP_ROWS_BIN1 = 34

    def _save_cropped(self, fits_path: str) -> bool:
        """按 bin 裁掉边缘光学黑像素，另存到 cropped/<tel>/。返回是否成功。"""
        import numpy as np
        from astropy.io import fits

        cropped_root = getattr(self, "cropped_root", None)
        if not cropped_root:
            return False
        try:
            with fits.open(fits_path, memmap=False, do_not_scale_image_data=True) as hdul:
                hdr = hdul[0].header.copy()
                data = np.asarray(hdul[0].data)
            binx = int(hdr.get("XBINNING", hdr.get("BINX", 1)) or 1)
            biny = int(hdr.get("YBINNING", hdr.get("BINY", 1)) or 1)
            lcols = self.OB_CROP_COLS_BIN1 // binx   # bin1=24, bin2=12
            brows = self.OB_CROP_ROWS_BIN1 // biny   # bin1=34, bin2=17
            ny, nx = data.shape
            if lcols <= 0 and brows <= 0:
                return False
            if lcols >= nx or brows >= ny:
                self.log(f"[预处理-裁切] 尺寸异常，跳过: {fits_path}")
                return False
            cropped = data[:ny - brows, lcols:].copy()
            if "CRPIX1" in hdr:
                hdr["CRPIX1"] = float(hdr["CRPIX1"]) - lcols
            for k in ("NAXIS", "NAXIS1", "NAXIS2"):
                hdr.pop(k, None)
            os.makedirs(cropped_root, exist_ok=True)
            fits.PrimaryHDU(data=cropped, header=hdr).writeto(
                os.path.join(cropped_root, os.path.basename(fits_path)), overwrite=True)
            return True
        except Exception as e:
            self.log(f"[预处理-裁切] 失败 {fits_path}: {e}")
            return False

    def _template_source_dir(self) -> str:
        """模板生成只使用裁切后的帧目录 cropped/<tel>/（不再回退原帧）。"""
        return getattr(self, "cropped_root", self.update_root)

    def _select_candidates(self, fits_files, k_full):
        pattern = k_full.lower()
        return [(fn, url, size) for fn, url, size in fits_files if pattern in fn.lower()]

    def _scan_region_url(self, url: str):
        try:
            files = self.directory_scanner.scan_directory_listing(url)
            if not files:
                files = self.web_scanner.scan_fits_files(url)
            return files
        except Exception as e:
            self.log(f"[扫描] 失败 {url}: {e}")
            return []

    @property
    def directory_scanner(self) -> DirectoryScanner:
        scanner = getattr(self, "_dir_scanner", None)
        if scanner is None:
            scanner = DirectoryScanner()
            self._dir_scanner = scanner
        return scanner

    @property
    def web_scanner(self) -> WebFitsScanner:
        scanner = getattr(self, "_web_scanner", None)
        if scanner is None:
            scanner = WebFitsScanner()
            self._web_scanner = scanner
        return scanner

    def _make_downloader(self) -> FitsDownloader:
        return FitsDownloader(max_workers=1, retry_times=3, timeout=30)

    def _compute_lm5sig(self, fits_path: Path) -> Optional[float]:
        import numpy as np
        from astropy.io import fits
        try:
            check_path = fits_path.with_name(f"{fits_path.stem}.check.fits")
            if not check_path.exists():
                return None
            with fits.open(check_path) as chdul:
                data = chdul[0].data
            if data is None:
                return None
            data = np.asarray(data, dtype=np.float64)
            med = float(np.median(data))
            if med <= 0:
                return None
            with fits.open(str(fits_path), mode="update") as hdul2:
                hdr = hdul2[0].header
                exptime = float(hdr.get("EXPOSURE", hdr.get("EXPTIME", 1.0)) or 1.0)
                if exptime <= 0:
                    exptime = 1.0
                zp = None
                for key in ("ZP", "PHOTZP", "MAGZP", "ZP_MAG"):
                    if key in hdr:
                        zp = hdr.get(key)
                        break
                zp = float(zp) if zp is not None else 25.0
                lmag = float(round(zp - 2.5 * np.log10(med * 5.0 * np.pi * 4.0 ** 2 / exptime), 3))
                hdr["LM5SIG"] = (lmag, "5-sigma limiting magnitude")
                hdul2.flush()
            return lmag
        except Exception as e:
            self.log(f"[LM5SIG] 计算失败 {fits_path}: {e}")
            return None

    def _quality_of(self, file_path: str):
        """返回 (is_bad, reasons)。"""
        import numpy as np
        from astropy.io import fits
        from .quality import FITSQualityAnalyzer

        analyzer = getattr(self, "_quality", None)
        if analyzer is None:
            analyzer = FITSQualityAnalyzer()
            self._quality = analyzer

        reasons: List[str] = []
        is_bad = False

        try:
            if not self.astap_processor.process_fits_file(file_path):
                reasons.append("ASTAP失败")
                is_bad = True
        except Exception as e:
            self.log(f"[预处理] ASTAP异常 {file_path}: {e}")
            reasons.append("ASTAP异常")
            is_bad = True

        fwhm = ellip = float("nan")
        lm_val = None
        try:
            with fits.open(file_path) as hdul:
                data = hdul[0].data
            if data is not None:
                metrics = analyzer.calculate_quality_metrics(data.astype(np.float64))
                if metrics:
                    fwhm = float(metrics.get("fwhm", float("nan")))
                    ellip = float(metrics.get("ellipticity", float("nan")))
                    lm_val = metrics.get("lm5sig")
        except Exception as e:
            self.log(f"[预处理] 质量分析失败 {file_path}: {e}")

        lm_from_check = self._compute_lm5sig(Path(file_path))
        if lm_from_check is not None:
            lm_val = lm_from_check

        s = self.settings
        if lm_val is None or not (isinstance(lm_val, (int, float)) and np.isfinite(lm_val)):
            reasons.append("LM未知")
            is_bad = True
        elif lm_val < s.lm_thres:
            reasons.append(f"LM5SIG={lm_val:.2f}<{s.lm_thres}")
            is_bad = True
        if not (isinstance(fwhm, (int, float)) and np.isfinite(fwhm)) or fwhm > s.fwhm_thres:
            reasons.append(f"FWHM={fwhm}")
            is_bad = True
        if not (isinstance(ellip, (int, float)) and np.isfinite(ellip)) or ellip > s.ell_thres:
            reasons.append(f"ELL={ellip}")
            is_bad = True

        return is_bad, reasons

    @property
    def astap_processor(self):
        proc = getattr(self, "_astap", None)
        if proc is None:
            from .astap_processor import ASTAPProcessor
            proc = ASTAPProcessor(self.config)
            self._astap = proc
        return proc

    # ---------- 下载目标天区对应观测文件 ----------
    def download_problem_templates(self) -> None:
        system_name, selected = self._load_targets()
        if system_name is None:
            return
        if not selected:
            self.status("请输入要处理的天区列表（例如 GY6_K002-8,GY6_K002-9）。", "orange")
            return
        index = load_gy1_index()
        if not index:
            self.status("GY1索引为空，请先执行“天区收集”。", "red")
            return

        system_upper = system_name.upper()
        os.makedirs(self.update_root, exist_ok=True)
        downloader = self._make_downloader()

        tasks = []
        for item in selected:
            dates = find_recent_dates_for_region(index, item["k_number"], self.settings.recent_days)
            if not dates:
                self.log(f"[模板更新] 天区 {item['k_number']} 在 GY1 索引中没有找到任何日期，跳过。")
                continue
            for date in dates:
                tasks.append((item, date))

        if not tasks:
            self.status("没有需要下载的文件（所有天区在索引中都不存在）", "orange")
            return

        total = len(tasks)
        completed = 0
        for idx, (problem, date) in enumerate(tasks, 1):
            region = problem["k_number"]
            k_full = problem["k_full"]
            region_url = self.config.build_url(system_upper, date, region)
            self.log(f"[模板更新] 扫描 {system_upper} {date} {k_full} - {region_url}")
            fits_files = self._scan_region_url(region_url)
            if not fits_files:
                self.log(f"[模板更新] {system_upper} {date} {k_full} 未找到任何FITS文件")
                continue
            candidates = self._select_candidates(fits_files, k_full)
            if not candidates:
                self.log(f"[模板更新] {system_upper} {date} 未找到包含 {k_full} 的文件（共 {len(fits_files)} 个）")
                continue
            for j, (filename, file_url, _size) in enumerate(candidates, 1):
                self.log(f"[模板更新] ({idx}/{total}) #{j}/{len(candidates)} 下载 {system_upper} {date} {k_full} -> {filename}")
                result = downloader.download_single_file(file_url, self.update_root, self._progress_cb("模板更新下载中"))
                if isinstance(result, str) and result.startswith("跳过已存在文件"):
                    self.status(f"模板更新: 已存在，跳过 - {k_full} -> {result}", "orange")
                else:
                    completed += 1
                    self.status(f"模板更新: 已下载 {completed} 个文件 - {k_full} -> {result}", "green")

        if completed:
            self.status(f"模板更新完成：成功 {completed}/{total} 个下载任务。", "green")
        else:
            self.status("模板更新完成：没有成功下载任何文件。", "orange")

    def _progress_cb(self, prefix: str):
        start_time = time.time()
        last = [start_time]

        def cb(downloaded, total, fn):
            now = time.time()
            if now - last[0] < 0.3 and (total is None or downloaded < (total or 0)):
                return
            last[0] = now
            speed = format_bytes(downloaded / max(now - start_time, 1e-3))
            total_str = format_bytes(total) if total is not None else "未知"
            self.status(f"{prefix}: {fn} {format_bytes(downloaded)}/{total_str} @ {speed}/s", "blue")

        return cb

    # ---------- 预处理 ----------
    def preprocess_images(self) -> None:
        system_name, _ = self._load_targets()
        if not system_name:
            return
        update_root_path = Path(self.update_root)
        if not update_root_path.exists():
            self.status("下载目录不存在，请先点击“下载目标天区对应观测文件”。", "red")
            return

        fits_files = []
        for ext in (".fits", ".fit", ".fts"):
            fits_files.extend(update_root_path.glob(f"*{ext}"))
            fits_files.extend(update_root_path.glob(f"*{ext.upper()}"))
        fits_files = sorted(set(fits_files))
        if not fits_files:
            self.status("预处理失败：下载目录中未找到任何FITS文件。", "orange")
            return

        os.makedirs(self.bad_dir, exist_ok=True)
        os.makedirs(self.cropped_root, exist_ok=True)
        total = len(fits_files)
        counters = {"processed": 0, "bad": 0, "cropped": 0}
        lock = threading.Lock()

        def _process(path: Path):
            is_bad, reasons = self._quality_of(str(path))
            if not is_bad:
                # 合格帧：按 bin 裁掉边缘光学黑，另存到 cropped/<tel>/
                if self._save_cropped(str(path)):
                    with lock:
                        counters["cropped"] += 1
            else:
                try:
                    shutil.move(str(path), os.path.join(self.bad_dir, path.name))
                    self.log(f"[预处理] 移动到 bad_img: {path.name} ({'; '.join(reasons)})")
                except Exception as e:
                    self.log(f"[预处理] 移动坏图像失败 {path}: {e}")
            with lock:
                counters["processed"] += 1
                if is_bad:
                    counters["bad"] += 1
                processed, bad = counters["processed"], counters["bad"]
            self.status(f"预处理: ({processed}/{total}) {path.name} -> {'BAD' if is_bad else 'OK'}，坏图像 {bad} 张",
                        "orange" if is_bad else "green")

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, self.settings.preprocess_threads)
        self.status(f"开始预处理 {total} 个FITS文件（ASTAP + 质量筛选 + 边缘裁切）...", "blue")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            executor.map(_process, fits_files)
        self.status(f"预处理完成：共处理 {total} 个文件，坏图像 {counters['bad']} 个，"
                    f"裁切保存 {counters['cropped']} 个 -> {self.cropped_root}", "green")

    # ---------- 生成模板（SWarp 后端）----------
    def _coadd_swarp(self, input_files, field_name: str):
        import numpy as np
        from astropy.io import fits
        from .swarp_backend import SwarpBackend, SWARP_EXE

        backend = SwarpBackend()
        if not backend.available():
            self.log(f"[生成] 项目内置 swarp 不可用: {SWARP_EXE}")
            return None, None

        out_dir = Path(self.output_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        coadd_path = str(out_dir / f"_swarp_{field_name}.fits")
        weight_path = coadd_path.replace(".fits", ".weight.fits")

        ok, msg = backend.coadd(
            input_files, coadd_path,
            combine_type=getattr(self.settings, "swarp_combine", "MEDIAN") or "MEDIAN",
            resampling=self.settings.swarp_resampling,
            threads=self.settings.swarp_threads,
        )
        if not ok:
            self.log(f"[生成] swarp 失败: {msg}")
            return None, None

        try:
            with fits.open(coadd_path) as h:
                data = h[0].data.astype("float32")
                header = h[0].header.copy()
            weight = None
            if os.path.exists(weight_path):
                try:
                    with fits.open(weight_path) as w:
                        weight = w[0].data
                except Exception:
                    weight = None
            if weight is not None:
                keep = weight > 0
                min_frames = int(getattr(self.settings, "min_frames", 0) or 0)
                if min_frames > 0:
                    try:
                        from astropy.wcs import WCS
                        cov = self._frame_coverage(input_files, WCS(header), data.shape)
                        low = (weight > 0) & (cov < min_frames)
                        n_masked = int(low.sum())
                        keep = keep & (cov >= min_frames)
                        self.log(f"[生成] {field_name} 最小覆盖帧数={min_frames}/{len(input_files)}："
                                 f"遮蔽低覆盖像素 {n_masked} 个")
                    except Exception as e:
                        self.log(f"[生成] {field_name} 覆盖帧数计算失败，跳过该掩膜: {e}")
                data = np.where(keep, data, np.nan).astype("float32")

            # 稳健加常数：以 k·σ(背景) 为直流偏置直接写入数据，残余负值截断到 0
            k = float(getattr(self.settings, "add_constant_sigma", 0.0) or 0.0)
            if k > 0:
                from astropy.stats import sigma_clipped_stats
                finite = data[np.isfinite(data)]
                if finite.size:
                    _, _, std = sigma_clipped_stats(finite, sigma=3.0, maxiters=5)
                    c = float(np.ceil(k * float(std)))
                    data = np.where(np.isfinite(data), np.maximum(data + c, 0.0), data).astype("float32")
                    header["BKGADD"] = (c, "constant added after coadd (robust k*sigma)")
                    header["BKGSIG"] = (float(std), "sigma-clipped background RMS")
                    self.log(f"[生成] {field_name} 加常数 C={c:.0f} (k={k}·σ, σ={std:.2f})，负值截断到 0")
        finally:
            for p in (coadd_path, weight_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return data, header

    def _write_template(self, field_name: str, input_files) -> bool:
        import numpy as np
        from astropy.io import fits

        start_time = time.time()
        self.status(f"模板生成: {field_name} - 使用 SWarp 合成...", "blue")
        data, header = self._coadd_swarp(input_files, field_name)
        if data is None:
            return False

        out_dir = Path(self.output_root)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 裁剪范围由“有限像素”决定（必须在填充 NaN 之前算）
        mask = np.isfinite(data)
        if not mask.any():
            self.log(f"[生成] {field_name} 合成结果全部为NaN，跳过裁剪。")
            return False
        ys, xs = np.where(mask)
        y_min, y_max = int(ys.min()), int(ys.max())
        x_min, x_max = int(xs.min()), int(xs.max())

        # 依据 nan_fill 选项填充无覆盖像素（仅保留裁剪后的正式模板，不写 no_trim）
        data_filled = self._apply_nan_fill(data, header)
        trimmed = data_filled[y_min:y_max + 1, x_min:x_max + 1].astype("float32")
        header = header.copy()
        if "CRPIX1" in header:
            header["CRPIX1"] -= x_min
        if "CRPIX2" in header:
            header["CRPIX2"] -= y_min
        fits.writeto(out_dir / f"{field_name}.fits", trimmed, header, overwrite=True)

        self.log(f"[生成] 完成 {field_name} (swarp, nan_fill={getattr(self.settings,'nan_fill','keep')}) "
                 f"-> {out_dir / (field_name + '.fits')}，用时 {time.time() - start_time:.1f} 秒")
        return True

    def _frame_coverage(self, input_files, out_wcs, shape, stride: int = 4):
        """统计每个输出像素被多少输入帧覆盖（不依赖 SWarp 权重）。

        在输出网格上按 stride 采样求天球坐标，逐帧 world->pixel 判断是否落在帧内，
        再上采样回全分辨率。
        """
        import numpy as np
        from astropy.io import fits
        from astropy.wcs import WCS

        h, w = shape
        yy, xx = np.mgrid[0:h:stride, 0:w:stride]
        ra, dec = out_wcs.all_pix2world(xx.astype(float), yy.astype(float), 0)
        count = np.zeros(ra.shape, dtype=np.int32)
        for f in input_files:
            try:
                with fits.open(f, memmap=True, do_not_scale_image_data=True) as hdul:
                    fw = WCS(hdul[0].header)
                    nx = int(hdul[0].header.get("NAXIS1", 0))
                    ny = int(hdul[0].header.get("NAXIS2", 0))
            except Exception:
                continue
            try:
                x, y = fw.all_world2pix(ra, dec, 0, quiet=True)
            except TypeError:
                x, y = fw.all_world2pix(ra, dec, 0)
            except Exception:
                continue
            inside = (x >= 0) & (x < nx) & (y >= 0) & (y < ny) & np.isfinite(x) & np.isfinite(y)
            count += inside.astype(np.int32)
        if stride > 1:
            count = np.repeat(np.repeat(count, stride, axis=0), stride, axis=1)[:h, :w]
        return count

    def _apply_nan_fill(self, data, header):
        """按设置填充非有限值：keep=保留NaN, bg=背景常数(BKGADD), zero=0。"""
        import numpy as np

        mode = (getattr(self.settings, "nan_fill", "keep") or "keep").lower()
        if mode == "keep":
            return data
        val = 0.0
        if mode == "bg":
            try:
                val = float(header.get("BKGADD", 0.0))
            except Exception:
                val = 0.0
        return np.where(np.isfinite(data), data, val).astype("float32")

    def make_templates(self) -> None:
        from astropy.io import fits
        from astropy.wcs import WCS

        system_name, selected = self._load_targets()
        if system_name is None:
            return
        if not selected:
            self.status("请输入要处理的天区列表（例如 GY6_K002-8,GY6_K002-9）。", "orange")
            return
        # 只使用裁切后的帧（cropped/<tel>/），不回退原帧
        source_dir = self._template_source_dir()
        update_root_path = Path(source_dir)
        has_frames = update_root_path.is_dir() and any(
            p for ext in ("*.fits", "*.fit", "*.fts", "*.FITS", "*.FIT")
            for p in update_root_path.glob(ext))
        if not has_frames:
            self.status(f"未找到裁切后的帧（{source_dir}），请先执行“预处理图像”。", "red")
            self.log(f"[模板生成] 无裁切帧，终止。目录: {source_dir}")
            return
        self.log(f"[模板生成] 使用裁切帧目录: {source_dir}")

        os.makedirs(self.output_root, exist_ok=True)

        self.status("正在对帧文件执行ASTAP解算...", "blue")

        def _astap_progress(current, total, file_path, success):
            self.status(f"ASTAP解算: ({current}/{total}) {'成功' if success else '失败'} - {os.path.basename(file_path)}",
                        "blue" if success else "orange")

        try:
            self.astap_processor.process_directory(source_dir, progress_callback=_astap_progress)
        except Exception as e:
            self.log(f"[模板生成] ASTAP解算过程中出错: {e}")

        total_templates = len(selected)
        made = 0
        for idx, item in enumerate(selected, 1):
            k_number = item.get("k_number")
            suffix = item.get("suffix")
            k_full = item.get("k_full")
            if not (k_number and suffix and k_full):
                continue
            try:
                field_name = f"K{int(str(k_number).lstrip('Kk')):03d}-{int(suffix)}"
            except Exception:
                self.log(f"[模板生成] 无法解析天区编号: {k_number}-{suffix}")
                continue

            if (Path(self.output_root) / f"{field_name}.fits").exists() and not getattr(self.settings, "overwrite_templates", False):
                self.log(f"[模板生成] 已存在输出模板 {k_full}，跳过（如需覆盖请勾选“覆盖已存在模板”）。")
                continue

            pattern = k_full.lower()
            fits_files = [str(p) for ext in ("*.fits", "*.fit", "*.fts")
                          for p in update_root_path.glob(ext) if pattern in p.name.lower()]
            if not fits_files:
                self.log(f"[模板生成] {k_full} 在下载目录中未找到任何FITS文件，跳过。")
                continue

            self.status(f"模板生成: ({idx}/{total_templates}) {k_full} - 使用 {len(fits_files)} 个文件合成...", "blue")
            used_files = []
            for fpath in fits_files:
                try:
                    with fits.open(fpath, memmap=True) as hdul:
                        if not WCS(hdul[0].header).has_celestial:
                            self.log(f"[模板生成] 文件缺少天球WCS，跳过: {fpath}")
                            continue
                    used_files.append(fpath)
                except Exception as e:
                    self.log(f"[模板生成] 打开文件失败 {fpath}: {e}")
            if not used_files:
                continue
            if len(used_files) < max(1, self.settings.min_images):
                self.log(f"[模板生成] {k_full} 可用图像数 {len(used_files)} 小于最少参与图像数 {self.settings.min_images}，跳过。")
                continue

            try:
                if self._write_template(field_name, used_files):
                    made += 1
                    self.status(f"模板生成: ({idx}/{total_templates}) {k_full} -> {field_name}.fits", "green")
            except Exception as e:
                self.log(f"[模板生成] {k_full} 处理失败: {e}")

        if made:
            self.status(f"模板生成完成：成功生成 {made}/{total_templates} 个模板，输出目录: {self.output_root}", "green")
        else:
            self.status("模板生成完成：没有成功生成任何新模板。", "orange")

    # ---------- 一键：边下载边预处理 -> 达标即生成 ----------
    def auto_generate(self) -> None:
        """对每个天区：按最近日期依次 下载→预处理，直到合格帧数达到 min_images
        或日期用尽，然后用合格(裁切)帧直接生成模板。"""
        system_name, selected = self._load_targets()
        if system_name is None:
            return
        if not selected:
            self.status("请输入要处理的天区列表（例如 GY6_K002-8,GY6_K002-9）。", "orange")
            return
        system_upper = system_name.upper()
        for d in (self.update_root, self.output_root, self.cropped_root, self.bad_dir):
            os.makedirs(d, exist_ok=True)

        index = load_gy1_index()
        if not index:
            self.status("GY1索引为空，请先执行“天区收集”。", "red")
            return
        downloader = self._make_downloader()
        min_images = max(1, int(self.settings.min_images or 1))
        recent_days = max(1, int(self.settings.recent_days or 1))
        total = len(selected)
        made = 0

        for idx, item in enumerate(selected, 1):
            k_number = item.get("k_number")
            suffix = item.get("suffix")
            k_full = item.get("k_full")
            if not (k_number and suffix and k_full):
                continue
            try:
                field_name = f"K{int(str(k_number).lstrip('Kk')):03d}-{int(suffix)}"
            except Exception:
                continue

            if (Path(self.output_root) / f"{field_name}.fits").exists() and not self.settings.overwrite_templates:
                self.status(f"[{idx}/{total}] {k_full} 已存在，跳过（勾选“覆盖已存在模板”可重生成）。", "orange")
                continue

            dates = find_recent_dates_for_region(index, k_number, recent_days)
            if not dates:
                self.status(f"[{idx}/{total}] {k_full} 在索引中无日期，跳过。", "orange")
                continue

            self.status(f"[{idx}/{total}] {k_full} 开始：目标≥{min_images} 帧（最多 {recent_days} 天）", "blue")
            qualified, stop = [], False
            for date in dates:
                if stop:
                    break
                region_url = self.config.build_url(system_upper, date, k_number)
                fits_files = self._scan_region_url(region_url)
                candidates = self._select_candidates(fits_files, k_full)
                if not candidates:
                    self.log(f"[自动] {k_full} {date}：无匹配文件")
                    continue
                for _fn, file_url, _size in candidates:
                    if len(qualified) >= min_images:
                        stop = True
                        break
                    result = downloader.download_single_file(file_url, self.update_root, self._progress_cb("自动下载"))
                    local = os.path.join(self.update_root, downloader.get_filename_from_url(file_url))
                    if not os.path.exists(local):
                        self.log(f"[自动] {k_full} 下载失败: {result}")
                        continue
                    is_bad, reasons = self._quality_of(local)
                    if is_bad:
                        try:
                            shutil.move(local, os.path.join(self.bad_dir, os.path.basename(local)))
                        except Exception:
                            pass
                        self.log(f"[自动] {k_full} 剔除坏帧 {os.path.basename(local)} ({'; '.join(reasons)})")
                        continue
                    if self._save_cropped(local):
                        qualified.append(os.path.join(self.cropped_root, os.path.basename(local)))
                    self.status(f"[{idx}/{total}] {k_full} 合格 {len(qualified)}/{min_images}（{date}）", "green")

            if not qualified:
                self.status(f"[{idx}/{total}] {k_full} 没有合格帧，跳过。", "red")
                continue
            if len(qualified) < min_images:
                self.log(f"[自动] {k_full} 日期用尽，合格 {len(qualified)}<{min_images}，仍生成。")

            self.status(f"[{idx}/{total}] {k_full} 生成模板（{len(qualified)} 帧）...", "blue")
            try:
                if self._write_template(field_name, qualified):
                    made += 1
            except Exception as e:
                self.log(f"[自动] {k_full} 生成失败: {e}")

        self.status(f"一键完成：成功生成 {made}/{total} 个模板 -> {os.path.join(self.workspace_root, 'output')}",
                    "green" if made else "orange")


