#!/usr/bin/env python3
"""模板质量检查：评估哪些模板"有问题"。

两层判据（A 自洽 + B 群体/几何），可选 C 抽样对照：
  A 单模板自洽：NaN/Inf 比例、背景中位数/RMS、FWHM、椭圆度、5σ限制星等、源数量。
  B 群体几何：FOV 宽/高按系统分组，用 IQR×6 判离群（与既有报告口径一致）。
  C 抽样对照（开关）：把模板 FWHM/背景 与同天区最近观测帧对比，判断叠加是否走样。

输出与既有 fits_check_report*.txt 兼容（FOV 统计 + NaN/Inf + 问题汇总），并附带 CSV。

用法:
  python -m app.check_templates <模板目录> [--report 报告路径] [--frames 观测帧目录] [--diff N]
"""

import argparse
import csv
import glob
import os
from datetime import datetime
from typing import Callable, Dict, List, Optional

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales

from .quality import FITSQualityAnalyzer

IQR_K_WARN = 6.0   # 与既有报告一致：IQR × 6 之外 -> 警告
IQR_K_ERR = 12.0   # 更远 -> 错误
FWHM_RATIO_WARN = 1.5  # 抽样对照：模板 FWHM > 该倍数 × 单帧 FWHM -> 警告
MIN_GEO_SAMPLES = 8    # 样本太少时不做几何离群判定（避免退化）

LogFn = Callable[[str], None]
StatusFn = Callable[[str, str], None]


def _noop_log(_m: str) -> None:
    pass


def _noop_status(_m: str, _c: str = "black") -> None:
    pass


def is_template_file(name: str) -> bool:
    n = name.lower()
    if not n.endswith((".fits", ".fit", ".fts")):
        return False
    if "no_trim" in n or "weight" in n or n.startswith("_"):
        return False
    return True


def measure_template(path: str, analyzer: FITSQualityAnalyzer) -> Dict:
    m: Dict = {"path": os.path.abspath(path), "name": os.path.basename(path),
               "errors": [], "warnings": []}
    try:
        hdul = fits.open(path, memmap=True)
    except Exception:
        hdul = fits.open(path, memmap=False)
    with hdul:
        hdr = hdul[0].header
        data = np.asarray(hdul[0].data, dtype="float32")
        ny, nx = data.shape
        m["shape"] = (ny, nx)
        total = data.size
        nonfinite = int(np.count_nonzero(~np.isfinite(data)))
        m["nonfinite"] = nonfinite
        m["finite_frac"] = 1.0 - (nonfinite / total) if total else 0.0

        finite = data[np.isfinite(data)]
        if finite.size:
            med = float(np.median(finite))
            _, _, rms = sigma_clipped_stats(finite, sigma=3.0, maxiters=5)
            m["bg_median"] = med
            m["bg_rms"] = float(rms)
        else:
            med = 0.0
            m["bg_median"] = float("nan")
            m["bg_rms"] = float("nan")

        # 几何
        try:
            w = WCS(hdr)
            scales = proj_plane_pixel_scales(w)  # deg/pixel (x, y)
            m["fov_w_arcmin"] = float(scales[0]) * nx * 60.0
            m["fov_h_arcmin"] = float(scales[1]) * ny * 60.0
            m["pixscale_arcsec"] = float(np.mean(scales)) * 3600.0
            m["crval"] = (float(hdr.get("CRVAL1", np.nan)), float(hdr.get("CRVAL2", np.nan)))
        except Exception:
            m["fov_w_arcmin"] = float("nan")
            m["fov_h_arcmin"] = float("nan")
            m["pixscale_arcsec"] = float("nan")
            m["crval"] = (float("nan"), float("nan"))

        m["ncombine"] = hdr.get("NCOMBINE")

    # 质量指标（NaN 用背景中位数填补，避免分析器报错）
    try:
        clean = np.where(np.isfinite(data), data, med)
        qm = analyzer.calculate_quality_metrics(clean.astype("float64"))
        if qm:
            m["n_sources"] = int(qm.get("n_sources", 0))
            m["fwhm"] = float(qm.get("fwhm", np.nan))
            m["ellipticity"] = float(qm.get("ellipticity", np.nan))
            m["lm5sig"] = float(qm.get("lm5sig", np.nan))
    except Exception as e:
        m["n_sources"] = 0
        m["fwhm"] = m["ellipticity"] = m["lm5sig"] = float("nan")
        m["warnings"].append(f"质量分析失败: {e}")
    return m


def _group_stats(values: List[float]) -> Optional[Dict]:
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if v.size == 0:
        return None
    med = float(np.median(v))
    q1, q3 = np.percentile(v, [25, 75])
    iqr = float(q3 - q1)
    if iqr > 0:
        w, e = IQR_K_WARN * iqr, IQR_K_ERR * iqr
        lo, hi, lo_err, hi_err = q1 - w, q3 + w, q1 - e, q3 + e
    else:
        # IQR 退化为 0（群体几乎一致）：用中位数的 ±10%/±20% 作为替代带宽
        w, e = 0.10 * abs(med), 0.20 * abs(med)
        lo, hi, lo_err, hi_err = med - w, med + w, med - e, med + e
    return {
        "n": int(v.size), "median": med, "mean": float(v.mean()),
        "q1": float(q1), "q3": float(q3), "iqr": iqr,
        "lo": float(lo), "hi": float(hi), "lo_err": float(lo_err), "hi_err": float(hi_err),
    }


def _flag_outlier(m: Dict, key: str, stats: Optional[Dict], label: str) -> None:
    val = m.get(key, float("nan"))
    if stats is None or not np.isfinite(val):
        return
    # 样本过少，或群体完全无尺度（全为 0），跳过几何离群判定
    if stats["n"] < MIN_GEO_SAMPLES or (stats["iqr"] <= 0 and abs(stats["median"]) == 0):
        return
    if val < stats["lo_err"] or val > stats["hi_err"]:
        m["errors"].append(f"{label}离群({val:.1f})")
    elif val < stats["lo"] or val > stats["hi"]:
        m["warnings"].append(f"{label}离群({val:.1f})")


def _lightframe_compare(entries: List[Dict], frame_dir: str, sample: int,
                        analyzer: FITSQualityAnalyzer, log: LogFn) -> None:
    """C 抽样对照：模板 FWHM 与同天区最近观测帧对比。"""
    if sample <= 0:
        return
    if not frame_dir or not os.path.isdir(frame_dir):
        log("[检查] 抽样对照：未提供有效的观测帧目录，跳过。")
        return
    frames = [f for ext in ("*.fit", "*.fits", "*.fts") for f in
              glob.glob(os.path.join(frame_dir, "**", ext), recursive=True)]
    checked = 0
    for m in entries:
        if checked >= sample:
            break
        import re
        mm = re.search(r"(K\d{3}-\d+)", m["name"])
        if not mm:
            continue
        k_full = mm.group(1)
        cand = [f for f in frames if k_full in os.path.basename(f)]
        if not cand:
            continue
        try:
            with fits.open(cand[0], memmap=False) as h:
                fd = np.asarray(h[0].data, dtype="float64")
            qm = analyzer.calculate_quality_metrics(fd)
            ff = float(qm.get("fwhm", np.nan)) if qm else float("nan")
            tf = m.get("fwhm", float("nan"))
            if np.isfinite(ff) and np.isfinite(tf) and ff > 0 and tf > FWHM_RATIO_WARN * ff:
                m["warnings"].append(f"抽样对照: 模板FWHM({tf:.2f})>{FWHM_RATIO_WARN}×单帧({ff:.2f})")
            checked += 1
        except Exception as e:
            log(f"[检查] 抽样对照失败 {cand[0]}: {e}")
    log(f"[检查] 抽样对照完成，实际对照 {checked} 个模板。")


def _fmt_arcmin(v: float) -> str:
    if not np.isfinite(v):
        return "nan"
    return f"{v:.2f}′ ({v / 60.0:.4f}°)"


def build_report(entries: List[Dict], directory: str) -> str:
    w_stats = _group_stats([e.get("fov_w_arcmin", float("nan")) for e in entries])
    h_stats = _group_stats([e.get("fov_h_arcmin", float("nan")) for e in entries])

    lines = []
    lines.append("=" * 80)
    lines.append("FITS文件检查报告")
    lines.append(f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"检查目录: {directory}")
    lines.append("=" * 80)
    lines.append("")

    # FOV 统计
    lines.append("【FOV统计信息】")
    lines.append("-" * 80)
    lines.append(f"样本数量: {len(entries)}")
    for label, st in (("宽度", w_stats), ("高度", h_stats)):
        if st is None:
            lines.append(f"{label}统计: 无有效数据")
            continue
        lines.append(f"{label}统计:")
        lines.append(f"  中位数: {_fmt_arcmin(st['median'])}")
        lines.append(f"  平均值: {_fmt_arcmin(st['mean'])}")
        lines.append(f"  四分位数: Q1={st['q1']:.2f}′, Q3={st['q3']:.2f}′")
        lines.append(f"  正常范围: [{st['lo']:.2f}′, {st['hi']:.2f}′]")
    lines.append(f"离群值检测方法: IQR (四分位距) × {IQR_K_WARN:g}")
    if len(entries) < MIN_GEO_SAMPLES:
        lines.append(f"注意: 样本数({len(entries)}) < {MIN_GEO_SAMPLES}，未做几何离群判定。")
    n_err = sum(len(e["errors"]) for e in entries)
    n_warn = sum(len(e["warnings"]) for e in entries)
    lines.append("")
    lines.append(f"发现 {n_err + n_warn} 个问题: 错误 {n_err}, 警告 {n_warn}")
    lines.append("")

    # 质量指标汇总
    lines.append("【质量指标汇总（中位数）】")
    lines.append("-" * 80)

    def _med(key):
        v = np.asarray([e.get(key, np.nan) for e in entries], dtype=float)
        v = v[np.isfinite(v)]
        return float(np.median(v)) if v.size else float("nan")

    lines.append(f"FWHM(像素): {_med('fwhm'):.2f}   椭圆度: {_med('ellipticity'):.3f}   "
                 f"5σ限制星等: {_med('lm5sig'):.2f}   背景RMS: {_med('bg_rms'):.2f}")
    lines.append("")

    # NaN/Inf
    lines.append("【数据异常（NaN/Inf）】")
    lines.append("-" * 80)
    nan_files = [e for e in entries if e.get("nonfinite", 0) > 0]
    lines.append(f"共 {len(nan_files)} 个文件包含非有限值")
    lines.append("")
    for e in nan_files[:50]:
        lines.append(f"文件: {e['path']}")
        lines.append(f"  HDU 0 包含 {e['nonfinite']} 个 NaN/Inf 值")
    if len(nan_files) > 50:
        lines.append(f"  ... 其余 {len(nan_files) - 50} 个略")
    lines.append("")

    # 汇总
    lines.append("【有问题的文件汇总】")
    lines.append("-" * 80)
    problems = [e for e in entries if e["errors"] or e["warnings"]]
    lines.append(f"共 {len(problems)} 个文件存在问题:")
    lines.append("")
    for i, e in enumerate(problems, 1):
        lines.append(f"{i}. {e['path']} ({len(e['errors'])}个错误, {len(e['warnings'])}个警告)")
        for msg in e["errors"]:
            lines.append(f"     [错误] {msg}")
        for msg in e["warnings"]:
            lines.append(f"     [警告] {msg}")
    if not problems:
        lines.append("（无）")
    lines.append("")
    lines.append("=" * 80)
    return "\n".join(lines)


def check_and_report(template_dir: str, report_path: str = None,
                     frame_dir: str = None, diff_sample: int = 0,
                     log: LogFn = _noop_log, status: StatusFn = _noop_status,
                     file_list: Optional[List[str]] = None) -> Dict:
    """检查模板，写出报告，返回汇总 dict。

    file_list 为 None 时检查 template_dir 下所有模板；否则只检查给定的文件（绝对路径）。
    """
    if file_list is None:
        files = sorted(f for f in glob.glob(os.path.join(template_dir, "*"))
                       if os.path.isfile(f) and is_template_file(os.path.basename(f)))
    else:
        files = [os.path.join(template_dir, f) if not os.path.isabs(f) else f for f in file_list]
        files = sorted(f for f in files if os.path.isfile(f))
    if not files:
        status("未找到要检查的模板文件", "orange")
        return {"files": [], "n_problems": 0}

    analyzer = FITSQualityAnalyzer()
    entries: List[Dict] = []
    for i, f in enumerate(files, 1):
        status(f"检查模板 ({i}/{len(files)}): {os.path.basename(f)}", "blue")
        try:
            entries.append(measure_template(f, analyzer))
        except Exception as e:
            log(f"[检查] 失败 {f}: {e}")

    # B 群体几何离群
    w_stats = _group_stats([e.get("fov_w_arcmin", float("nan")) for e in entries])
    h_stats = _group_stats([e.get("fov_h_arcmin", float("nan")) for e in entries])
    for e in entries:
        _flag_outlier(e, "fov_w_arcmin", w_stats, "FOV宽度")
        _flag_outlier(e, "fov_h_arcmin", h_stats, "FOV高度")
        if e.get("nonfinite", 0) > 0:
            e["errors"].append(f"含 {e['nonfinite']} 个 NaN/Inf 像素")

    # C 抽样对照
    _lightframe_compare(entries, frame_dir, diff_sample, analyzer, log)

    report = build_report(entries, os.path.abspath(template_dir))
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(report)
        write_csv(entries, os.path.splitext(report_path)[0] + ".csv")
        log(f"[检查] 报告已写出: {report_path}")

    problems = [e for e in entries if e["errors"] or e["warnings"]]
    n_err = sum(len(e["errors"]) for e in entries)
    n_warn = sum(len(e["warnings"]) for e in entries)
    status(f"检查完成：{len(files)} 个模板，{len(problems)} 个有问题（错误 {n_err}, 警告 {n_warn}）",
           "red" if n_err else ("orange" if n_warn else "green"))
    return {"files": entries, "n_problems": len(problems), "n_errors": n_err, "n_warnings": n_warn}


_CSV_COLS = ["name", "status", "shape", "finite_frac", "nonfinite", "bg_median", "bg_rms",
             "fov_w_arcmin", "fov_h_arcmin", "pixscale_arcsec", "fwhm", "ellipticity",
             "lm5sig", "n_sources", "ncombine", "n_errors", "n_warnings", "messages"]


def _entry_status(e: Dict) -> str:
    if e.get("errors"):
        return "错误"
    if e.get("warnings"):
        return "警告"
    return "正常"


def write_csv(entries: List[Dict], csv_path: str) -> None:
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(_CSV_COLS)
            for e in entries:
                w.writerow([
                    e.get("name"), _entry_status(e), e.get("shape"),
                    round(e.get("finite_frac", 0), 6), e.get("nonfinite"),
                    e.get("bg_median"), e.get("bg_rms"),
                    e.get("fov_w_arcmin"), e.get("fov_h_arcmin"), e.get("pixscale_arcsec"),
                    e.get("fwhm"), e.get("ellipticity"), e.get("lm5sig"), e.get("n_sources"),
                    e.get("ncombine"), len(e.get("errors", [])), len(e.get("warnings", [])),
                    "; ".join(e.get("errors", []) + e.get("warnings", [])),
                ])
    except Exception:
        pass


def save_results(entries: List[Dict], path: str) -> bool:
    """把检查结果按扩展名保存为 CSV 或 JSON。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".json":
            import json

            def _clean(v):
                if isinstance(v, np.floating):
                    return float(v) if np.isfinite(v) else None
                if isinstance(v, np.integer):
                    return int(v)
                if isinstance(v, (tuple, list)):
                    return [_clean(x) for x in v]
                if isinstance(v, float):
                    return v if np.isfinite(v) else None
                return v

            safe = [{k: _clean(v) for k, v in e.items()} for e in entries]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(safe, fh, ensure_ascii=False, indent=2)
        else:
            write_csv(entries, path)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="模板质量检查")
    ap.add_argument("template_dir", help="模板目录（含 *.fits）")
    ap.add_argument("--report", default=None, help="报告输出路径")
    ap.add_argument("--frames", default=None, help="观测帧目录（抽样对照用）")
    ap.add_argument("--diff", type=int, default=0, help="抽样对照的模板数（0=关闭）")
    args = ap.parse_args()
    check_and_report(args.template_dir, args.report, args.frames, args.diff,
                     log=lambda m: print(m), status=lambda t, c="black": print(f"[{c}] {t}"))


if __name__ == "__main__":
    main()
