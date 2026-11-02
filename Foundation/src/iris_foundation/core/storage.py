"""HDF5 统一落盘 —— 数据格式的唯一事实来源。

历史教训：曾有四套互不兼容的落盘约定（旧 ScopeFoundry 单帧测量的 spectrum、
image_monitor 的 frame+attrs、run_experiment 单帧、扫描引擎），同一实验
数据读法取决于"哪个版本写的"。本模块收敛全部约定，新数据一律带
schema_version attrs 供读取端分派。

统一约定（schema v1）：
- 帧数据集一律名 "frame"：多帧 (n, H, W[, 3])；单帧不带堆叠轴 (H, W[, 3])
  （读取端 np.atleast_3d 统一处理），gzip4 逐帧 flush
- 根 attrs 必有 schema_version / source / created_at
- 波长标定数据集一律名 "wavelength"（unit=nm + description），且只经
  fetch_wavelength() 获取（configure+读校准的坑收敛在唯一入口）
- 文件名唯一化只经 unique_path()

旧数据不迁移：无 schema_version 的文件按历史格式读。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1
WL_DESCRIPTION = "每列像素中心波长"


# ---------------------------------------------------------------- 命名与路径

def sanitize_name(name: str) -> str:
    """HDF5 数据集名清理：非法字符换下划线（中文/字母/数字/连字符保留）。"""
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name.strip()) or "unnamed"


def unique_path(path: Path) -> Path:
    """文件名去重：已存在则加 _1/_2/… 后缀（防同秒连点覆盖）。"""
    if not path.exists():
        return path
    for i in range(1, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    return path


# ---------------------------------------------------------------- 波长标定

def fetch_wavelength(spec, cam=None) -> np.ndarray | None:
    """读取每像素波长标定（唯一入口，含已知坑的容错）。

    Shamrock 的 PixelWidth/NumberPixels 不跨进程持久（实测新进程为 0，
    此时 GetCalibration 静默返回全 0——错误数据比报错更危险），因此：
    1. 每次必须先 configure_calibration(pixel_size, n) 再读；
    2. 读回全 0 视为"无标定"返回 None（调用方回退像素轴/跳过写入）。

    spec: AndorShamrock（已连接）；cam: AndorCamera（可选，未连接时用
    DU920 默认几何 26µm×1024px）。
    """
    if cam is not None:
        try:
            px, _ = cam.pixel_size
            n = cam.detector_size[0]
        except Exception:
            px, n = 26.0, 1024
    else:
        px, n = 26.0, 1024   # DU920 默认（相机未连接时）
    try:
        spec.configure_calibration(px, n)
        wl = np.asarray(spec.calibration_wavelengths(n), dtype=float)
    except Exception:
        return None
    if wl is None or wl.size == 0 or not np.any(wl):
        return None          # 全 0 = 固件参数未配置成功
    return wl


def write_wavelength(group, wl: np.ndarray) -> None:
    """把波长标定写进 HDF5 组（覆盖同名旧数据集）。"""
    ds = group.require_dataset(
        "wavelength", shape=wl.shape, dtype=float,
        data=np.asarray(wl, dtype=float), compression="gzip",
        compression_opts=4)
    ds.attrs["unit"] = "nm"
    ds.attrs["description"] = WL_DESCRIPTION


# ---------------------------------------------------------------- 帧栈写入

def init_frame_file(f, source: str, **attrs) -> None:
    """帧文件根 attrs 统一入口（schema_version/source/created_at + 自定义）。"""
    f.attrs["schema_version"] = SCHEMA_VERSION
    f.attrs["source"] = source
    f.attrs["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    for key, value in attrs.items():
        f.attrs[key] = value


class FrameStackWriter:
    """帧栈增量写入：(n, *frame.shape) 数据集，逐帧 flush（中断安全）。

    用法::
        with h5py.File(path, "w") as f:
            init_frame_file(f, source, frames=10)
            writer = FrameStackWriter(f, expected=10)
            for frame in frames:
                writer.append(frame)     # 首帧建数据集，每帧 flush
            writer.attrs["saved"] = writer.count
    """

    def __init__(self, group, expected: int):
        self._group = group
        self._expected = int(expected)
        self._ds = None
        self.count = 0

    def append(self, frame: np.ndarray) -> None:
        arr = np.asarray(frame)
        if self._ds is None:
            if self._expected == 1:
                # 单帧不带堆叠轴（与 GUI 手动保存的历史行为一致）
                self._ds = self._group.create_dataset(
                    "frame", data=arr, compression="gzip", compression_opts=4)
            else:
                self._ds = self._group.create_dataset(
                    "frame", shape=(self._expected, *arr.shape), dtype=arr.dtype,
                    compression="gzip", compression_opts=4)
        if self._expected == 1:
            self._ds[...] = arr      # 单帧：整写
        else:
            self._ds[self.count] = arr
        self.count += 1
        self._group.file.flush()

    @property
    def attrs(self):
        return self._group.attrs


class ScalarSeriesWriter:
    """标量数列增量写入：数据集 "value" (n,) float64 + "t" (n,) 相对秒。

    与 FrameStackWriter 同纪律（schema v1 族：init_*_file 统一根 attrs、
    唯一化只经 unique_path）；单标量 = expected=1，不带堆叠轴。
    """

    def __init__(self, group, expected: int, value_name: str = "value"):
        self._group = group
        self._expected = int(expected)
        self._name = value_name
        self._ds = None
        self._t0 = None
        self.count = 0

    def append(self, value: float, t_mono: float | None = None) -> None:
        import time as _time
        val = float(value)
        if self._ds is None:
            n = self._expected
            self._ds = self._group.create_dataset(
                self._name, shape=(n,) if n > 1 else (), dtype="f8")
        if self._t0 is None:
            self._t0 = _time.monotonic() if t_mono is None else t_mono
        t = (t_mono if t_mono is not None else _time.monotonic()) - self._t0
        if self._expected == 1:
            self._ds[()] = val
        else:
            self._ds[self.count] = val
        self._group.file.flush()
        self.count += 1

    @property
    def attrs(self):
        return self._group.attrs
