"""设备合同 v1 —— 接入 labcontrol 的设备必须会说的五样东西。

规范来源：docs/architecture.md §4/§5；实现对其的任何偏离先改文档再改代码。

设计约束（§3/§8）：
  * 全平台最底层：仅标准库，不 import Qt/numpy/厂商 SDK——进程内 GUI、
    无头 CLI、将来的进程外 device server 共用同一份定义。
  * 全部纯数据（frozen dataclass）+ typing.Protocol，可 pickle（§8 规则 3）。

五要素：身份 DeviceInfo / 生命周期 DeviceState / 参数 ParamDesc /
数据流 StreamDesc / 事件 EventDesc；外加能力接口与 DriverManifest。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

#: 合同主版本。注册表加载驱动时校验：不符 → 拒绝加载（§4.1）。
CONTRACT_VERSION = 1


# ------------------------------------------------------------------ 1. 身份

@dataclass(frozen=True)
class DeviceInfo:
    """设备身份（§4.1）。静态部分来自 manifest；serial_number 连接后补全。"""

    dev_id: str                       # workspace 内唯一实例 id，如 "camera"
    driver: str                       # 驱动名 = 模块名 "andor_camera"
    model: str                        # "Newton DU920"
    serial_number: str | None = None
    contract_version: int = CONTRACT_VERSION
    capabilities: tuple[str, ...] = ()


# -------------------------------------------------------------- 2. 生命周期

class DeviceState(str, Enum):
    """设备生命周期状态机（§4.2）：任何状态可 → error / disconnected。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"


# ------------------------------------------------------------------ 3. 参数

@dataclass(frozen=True)
class ParamDesc:
    """一个可扫描/可写参数的自描述（§4.3）。字段与 core.params.ScanParam 对应。"""

    key: str                          # "exposure" / "wavelength" / "position"
    ptype: str                        # "float" | "int" | "bool" | "enum"
    label: str                        # 显示名（含单位）
    unit: str                         # "s" / "nm" / "pulse"
    apply: str                        # "per_capture"（曝光随采集下发）| "write"（写参→稳定）
    vmin: float | None                # 量程下限（None = 驱动不设限）
    vmax: float | None                # 量程上限
    access: str = "rw"                # "ro" | "rw"
    decimals: int = 3                 # GUI 小数位
    settle_ms: int = 200              # apply="write" 的稳定延时
    is_exposure: bool = False         # per_capture 且为曝光：轴值即采集曝光
    setter: str | None = None         # 写侧（P6）：None=自动推导（见下）
    getter: str | None = None         # 读侧：None=同名属性（无则不可回读）
    readback_tol: float | None = None # 回读容差（None=不校验回读）

    def resolve_setter(self, device) -> str | None:
        """写侧解析（P6）：显式声明优先，否则按能力/命名约定推导。

        推导顺序：motor_axis 能力且 key="position" → "move_to"；
        set_<key> 方法存在 → "set_<key>"；同名可写属性 → 属性名；无 → None
        （参数只读，目录消费方应隐藏其可扫性）。
        """
        if self.setter is not None:
            return self.setter
        if self.key == "position" and isinstance(device, MotorAxis):
            return "move_to"
        name = f"set_{self.key}"
        if callable(getattr(device, name, None)):
            return name
        attr = getattr(type(device), self.key, None)
        if isinstance(attr, property) and attr.fset is not None:
            return self.key
        return None

    def resolve_getter(self, device) -> str | None:
        """读侧解析：显式声明优先，否则同名属性。

        不能用 ``hasattr``：属性 getter 会真的去读硬件（如 TdlmAttenuator.position
        在串口未打开时抛错），而 hasattr 只吞 AttributeError——设备错误会被当成
        “有没有这个属性”抛出去，直接打断扫描启动（2026-10-21 实测）。
        这里只查类/实例表，绝不触发 getter。
        """
        if self.getter is not None:
            return self.getter
        if hasattr(type(device), self.key) or self.key in getattr(device, "__dict__", {}):
            return self.key
        return None


# ---------------------------------------------------------------- 4. 数据流

@dataclass(frozen=True)
class StreamDesc:
    """一条数据流的自描述（§4.4）。topic 命名 <dev_id>.<name>。"""

    name: str                         # "frame" / "temp" / "status" / ...
    kind: str                         # "frame" | "scalar" | "spectrum" | "status"
    dtype: str = ""                   # "uint16" / "float64" / ...（空 = 运行时定）
    shape: tuple[int, ...] | None = None    # 帧尺寸；标量/变长为 None
    unit: str = ""

    def topic(self, dev_id: str) -> str:
        return stream_topic(dev_id, self.name)


def stream_topic(dev_id: str, name: str) -> str:
    """数据流主题规则（§4.4）：<dev_id>.<stream_name>（camera.frame 即此式）。"""
    return f"{dev_id}.{name}"


# ------------------------------------------------------------------ 5. 事件

@dataclass(frozen=True)
class EventDesc:
    """设备事件信封（§4.5）：状态迁移/告警/错误 → <dev_id>.event 主题。"""

    device: str
    event: str                        # "state" | "warning" | "error"
    state: str                        # DeviceState 值或厂商状态文本
    message: str = ""
    t_utc: str = ""                   # ISO8601，由发布方填

    def as_payload(self) -> dict:
        return {"device": self.device, "event": self.event, "state": self.state,
                "message": self.message, "t_utc": self.t_utc}


def event_topic(dev_id: str) -> str:
    """设备事件主题（§4.5）：<dev_id>.event。"""
    return f"{dev_id}.event"


def make_envelope(device: str, stream: str, data, unit: str = "") -> dict:
    """标准信封（§4.4，P6 启用策略：新驱动一律使用；老流冻结）。

    双时钟：t_mono 单调钟（算间隔不受系统改时影响），t_utc 入 HDF5 attrs。
    """
    import time
    return {"t_mono": time.monotonic(),
            "t_utc": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "device": device, "stream": stream, "data": data, "unit": unit}


# --------------------------------------------------------------- 驱动自声明

@dataclass(frozen=True)
class DriverManifest:
    """驱动自声明（§6）：每个驱动模块尾部一条；纯数据，entry 用类名字符串。

    dev_id 是 setup/1 过渡期的默认实例 id；setup/2 起实例 id 由 workspace
    指定，同一驱动可多实例（如两台同型相机）。
    """

    driver: str                       # 驱动名 = 模块名 "tdlm_attenuator"
    dev_id: str                       # 默认实例 id "attenuator"
    entry: str                        # 设备类名 "TdlmAttenuator"（registry 按需取）
    model: str                        # "TDLMON 3-axis X"
    display_name: str = ""            # 左栏短名（P3 起 GUI 从这里取）
    capabilities: tuple[str, ...] = ()
    contract_version: int = CONTRACT_VERSION
    page: str = ""                    # gui 页面模块全名（§14；空 = 基座通用自动页）。
                                      # 懒 import：core/CLI 永不加载 Qt。
    default_transport: str = "native"     # "serial" | "native" | "gentl" | "visa"
    supports_reconnect: bool = False    # 行为位：采集会话可重建自愈（如 DVP）
    connect_timeout_ms: int = 20000
    disconnect_timeout_ms: int = 15000
    streams: tuple[StreamDesc, ...] = ()  # 数据流声明（GUI 帧流绑定由此派生）


# ------------------------------------------------------------- 能力接口（§5）

@runtime_checkable
class Detector(Protocol):
    """采集设备。方法名沿用两台现有相机，零改动即符合。"""

    def acquire_frame(self, exposure_s: float | None = None):
        """采集一帧；exposure_s=None 用设备当前曝光。"""
        ...


@runtime_checkable
class Spectrometer(Protocol):
    """单色仪：波长读写。"""

    @property
    def wavelength(self) -> float: ...

    @wavelength.setter
    def wavelength(self, value: float) -> None: ...


@runtime_checkable
class MotorAxis(Protocol):
    """运动轴：位置回读 + 运动控制（TDLMON 现有方法名）。"""

    @property
    def position(self) -> float: ...

    @property
    def is_moving(self) -> bool: ...

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> float: ...

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> float: ...

    def stop(self, all_axes: bool = False) -> None: ...


@runtime_checkable
class PowerMeter(Protocol):
    """光功率计：读功率（W）。首个实现者：模板 templates/scpi_power_meter.py。"""

    def power(self) -> float: ...


#: 能力名 → Protocol（registry 加载时结构化校验：声明了就得真实现）
CAPABILITY_PROTOCOLS: dict[str, type] = {
    "detector": Detector,
    "spectrometer": Spectrometer,
    "motor_axis": MotorAxis,
    "power_meter": PowerMeter,
}


# ------------------------------------------------- 测量单元自声明（§14.2）

@dataclass(frozen=True)
class MeasurementManifest:
    """测量单元自声明（docs/architecture.md §14.2）。

    纯数据，零 Qt——CLI/agent 只读这层即可发现实验；视图实现（module 内的
    entry 类）由 gui 层按需懒加载。requires 声明能力需求（不点名具体设备，
    换设备实验零改动）；panel 为可选 gui 面板模块全名（空 = 自动表单）。
    """

    mid: str                          # 实验 id "scan"（workspace/CLI/HDF5 用）
    display_name: str                 # "Scan"
    category: str                     # "acquisition" | "orchestration"
    entry: str                        # 测量类名 "ScanMeasurement"
    module: str                       # 实现模块全名（gui 层懒加载目标）
    requires: tuple[str, ...] = ()    # 能力需求（§5 能力名）
    panel: str = ""                   # 可选 gui 面板模块全名；空 = 自动表单
    contract_version: int = CONTRACT_VERSION
