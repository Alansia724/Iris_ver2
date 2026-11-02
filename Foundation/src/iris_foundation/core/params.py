"""扫描参数目录与数据描述（自 gui/scan_panel 迁入，GUI 与 CLI 共用）。

设计原则——引擎对"扫什么/看什么"零特判，一切由数据描述：
  ScanParam      一个可扫描参数（应用模式 + 数值范围），纯数据
  PARAM_CATALOG  设备 → {参数键: ScanParam} 目录

目录是**注册表汇总视图**（docs/architecture.md §4.3）：条目全部来自各驱动
params() 自声明——单一事实来源在驱动。原手写中央表已随 P2/P3 全量迁移删除
（attenuator P2；camera/spec/dvp P3）。未声明 params() 的驱动无扫描参数。
"""

from __future__ import annotations

from dataclasses import dataclass

from iris_foundation.core.contract import ParamDesc
from iris_foundation.core.registry import declared_params, manifests

# HDF5 保留数据集名（用户命名不得占用）
RESERVED_NAMES = {"point_time", "point_readback"}


@dataclass(frozen=True)
class ScanParam:
    """一个可扫描参数：应用模式 + 数值范围。目录数据，引擎无特判。"""

    key: str
    label: str                     # 显示名（含单位）
    unit: str
    apply: str                     # per_capture=曝光值随采集下发 / write=写参→稳定
    vmin: float
    vmax: float
    decimals: int = 3
    settle_ms: int = 200           # apply="write" 时的默认稳定延时
    is_exposure: bool = False      # per_capture 且为曝光：轴值即采集曝光

    @classmethod
    def from_desc(cls, d: ParamDesc) -> "ScanParam":
        """合同 ParamDesc → 目录条目（字段一一对应；量程由声明方保证给出）。"""
        return cls(key=d.key, label=d.label, unit=d.unit, apply=d.apply,
                   vmin=d.vmin, vmax=d.vmax, decimals=d.decimals,
                   settle_ms=d.settle_ms, is_exposure=d.is_exposure)


def _build_catalog() -> dict[str, dict[str, ScanParam]]:
    catalog: dict[str, dict[str, ScanParam]] = {}
    for m in manifests():
        declared = declared_params(m.dev_id)
        if declared:
            catalog[m.dev_id] = {k: ScanParam.from_desc(d)
                                 for k, d in declared.items()}
    return catalog


# 设备 → {参数键: ScanParam}。全部条目来自驱动 params() 自声明（见上）。
PARAM_CATALOG: dict[str, dict[str, ScanParam]] = _build_catalog()
