"""Andor Newton DU920 CCD 相机 —— 设备接口层（经典 Andor SDK2）。

纯 Python + ctypes，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

用法::

    from iris_foundation.devices.andor_camera import AndorCamera
    with AndorCamera() as cam:              # 连接时默认开制冷、目标 -80°C
        print(cam.head_model, cam.serial_number, cam.detector_size)
        frame = cam.acquire_frame(exposure=0.1)   # 返回 (Ny, Nx) int32 数组

注意:
  * 运行前关闭 Andor Solis（设备独占）。
  * SDK2 每个进程只支持一台相机；Initialize/ShutDown 成对调用。
  * 默认配置：开制冷目标 -80°C（连接后需数分钟降温，代码不等待稳定）、
    快门自动（曝光期间开合）、single 模式、1x1 全幅。
"""

import ctypes
import threading
import time
from ctypes import (POINTER, Structure, byref, c_char_p, c_float, c_int,
                    c_long, c_uint, c_ulong, create_string_buffer, windll)

import numpy as np

from iris_foundation.core.contract import DriverManifest, ParamDesc, StreamDesc
from iris_foundation.core.topics import Dev

# ---- Andor SDK2 返回码（与 ATMCD32D.H 一致）----
DRV_SUCCESS = 20002
DRV_ERROR_ACK = 20013
DRV_TEMP_OFF = 20034
DRV_TEMP_NOT_STABILIZED = 20035
DRV_TEMP_STABILIZED = 20036
DRV_TEMP_NOT_REACHED = 20037
DRV_TEMP_NOT_SUPPORTED = 20039
DRV_TEMP_DRIFT = 20040
DRV_ACQUIRING = 20072
DRV_IDLE = 20073
DRV_TEMPCYCLE = 20074
DRV_NOT_INITIALIZED = 20075

TEMP_STATUS_NAMES = {
    DRV_TEMP_OFF: "OFF",
    DRV_TEMP_NOT_STABILIZED: "NOT_STABILIZED",
    DRV_TEMP_STABILIZED: "STABILIZED",
    DRV_TEMP_NOT_REACHED: "NOT_REACHED",
    DRV_TEMP_NOT_SUPPORTED: "NOT_SUPPORTED",
    DRV_TEMP_DRIFT: "DRIFT",
}
STATUS_NAMES = {
    DRV_IDLE: "IDLE",
    DRV_TEMPCYCLE: "TEMPCYCLE",
    DRV_ACQUIRING: "ACQUIRING",
    DRV_ERROR_ACK: "ERROR_ACK",
}

# 本机 DLL 搜索顺序（与官方组件一致；版本须与已装驱动匹配）
DLL_CANDIDATES = [
    r"C:\Program Files\Andor SOLIS\atmcd64d_legacy.dll",
    r"C:\Program Files\Andor SDK\atmcd64d.dll",
]

ACQ_MODE_IDS = {"single": 1, "accumulate": 2, "kinetic": 3, "run_till_abort": 4}

# SetShutter/SetShutterEx 的 mode/extmode 参数：0=自动、1=开、2=关
SHUTTER_MODES = {"auto": 0, "open": 1, "closed": 2}

# GetCapabilities.ulFeatures 能力位（ATMCD32D.H）
AC_FEATURES_SHUTTER = 8
AC_FEATURES_SHUTTEREX = 16   # 可独立控制内部/外部快门 → 必须用 SetShutterEx（SDK 手册 NOTE）


class AndorCapabilities(ctypes.Structure):
    """GetCapabilities 输出结构（12×ULONG，本机 atmcd64d 的布局）。"""
    _fields_ = [
        ("ulSize", c_ulong), ("ulAcqModes", c_ulong), ("ulReadModes", c_ulong),
        ("ulTriggerModes", c_ulong), ("ulCameraType", c_ulong), ("ulPixelMode", c_ulong),
        ("ulSetFunctions", c_ulong), ("ulGetFunctions", c_ulong), ("ulFeatures", c_ulong),
        ("ulPCICard", c_ulong), ("ulEMGainCapability", c_ulong), ("ulFTReadModes", c_ulong),
    ]


class AndorCameraError(IOError):
    """Andor SDK2 调用失败。"""


def _err(retval: int) -> int:
    if retval == DRV_SUCCESS:
        return retval
    raise AndorCameraError(f"Andor SDK 错误码 {retval}")


class AndorCamera:
    """Newton DU920（经典 Andor SDK2）的干净接口。"""

    def __init__(self, dll_path: str = None):
        self.dll_path = dll_path
        self._lib = None
        self._lock = threading.Lock()
        self._connected = False

        # 基本信息（connect 后填充）
        self.head_model: str = ""
        self.serial_number: int = 0
        self.detector_size: tuple = (0, 0)
        self.temperature_range: tuple = (0, 0)
        self.temperature: int = 0
        self.temperature_status: str = "?"
        self.cooler_on: bool = False
        self.temp_setpoint: int = -80
        self.exposure_time: float = 0.1
        self.acq_mode: str = "single"
        self.shutter_mode: str = "auto"  # 软件记录的命令状态（SDK2 无 GetShutter 读回）
        self._features: int = 0          # GetCapabilities.ulFeatures
        self._shutterex: bool = False    # AC_FEATURES_SHUTTEREX → 必须用 SetShutterEx
        self.hbin = self.vbin = 1
        self.Nx_ro = self.Ny_ro = 0
        self._buffer = None
        self.preamp_gain_index = 0       # 软件记账（本机型 GetPreAmpGain 不可信）

    # ------------------------------------------------------------- 生命周期

    def connect(self) -> None:
        """加载 DLL 并初始化相机（默认：不制冷、快门自动、single 全幅）。"""
        if self._connected:
            return
        path = self.dll_path or self._find_dll()
        lib = windll.LoadLibrary(path)
        with self._lock:
            _err(lib.Initialize(b""))
        self._lib = lib
        self._connected = True

        self.head_model = self._get_head_model()
        self.serial_number = self._get_serial_number()
        self.detector_size = self._get_detector()
        self._query_capabilities()

        # 默认配置：开启制冷、目标 -80°C（用户要求），快门自动、single 全幅
        self.set_readout_mode_image(hbin=1, vbin=1)
        self.set_acq_mode("single")
        self.set_exposure_time(0.1)
        self.set_shutter("auto")
        self.set_preamp_gain(0)          # 固定从 1× 起步：硬件档位不回读，状态必须确定
        self.temp_setpoint = -80
        self.set_temperature_setpoint(-80)
        self.set_cooler(True)

        self.temperature_range = self._get_temperature_range()
        self.temperature = self._get_temperature()

    def disconnect(self) -> None:
        """关闭相机并释放 SDK。"""
        if self._connected and self._lib is not None:
            try:
                with self._lock:
                    self._lib.ShutDown()
            finally:
                self._lib = None
                self._connected = False

    def __enter__(self) -> "AndorCamera":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def _find_dll(self) -> str:
        import os
        for p in DLL_CANDIDATES:
            if os.path.exists(p):
                return p
        raise AndorCameraError(
            "未找到相机 SDK DLL（查找过: " + "; ".join(DLL_CANDIDATES) + "）；"
            "请确认已安装 Andor SDK / Solis")

    # ------------------------------------------------------------- 基础查询

    def _get_head_model(self) -> str:
        buf = create_string_buffer(260)
        with self._lock:
            _err(self._lib.GetHeadModel(buf))
        return buf.value.decode(errors="replace").strip("\x00")

    def _get_serial_number(self) -> int:
        n = c_int()
        with self._lock:
            _err(self._lib.GetCameraSerialNumber(byref(n)))
        return n.value

    def _get_detector(self) -> tuple:
        x, y = c_int(), c_int()
        with self._lock:
            _err(self._lib.GetDetector(byref(x), byref(y)))
        return x.value, y.value

    @property
    def pixel_size(self) -> tuple[float, float]:
        """探测器像素尺寸 (x, y)，单位 µm（本机 DU920 实测 26.0 × 26.0）。"""
        x, y = c_float(), c_float()
        with self._lock:
            _err(self._lib.GetPixelSize(byref(x), byref(y)))
        return x.value, y.value

    def _query_capabilities(self) -> None:
        """读取 GetCapabilities 能力位（失败则置零，兼容旧 DLL）。"""
        try:
            caps = AndorCapabilities()
            caps.ulSize = ctypes.sizeof(AndorCapabilities)
            with self._lock:
                ret = self._lib.GetCapabilities(byref(caps))
            if ret == DRV_SUCCESS:
                self._features = caps.ulFeatures
                self._shutterex = bool(caps.ulFeatures & AC_FEATURES_SHUTTEREX)
        except Exception:
            self._features = 0
            self._shutterex = False

    def _get_temperature_range(self) -> tuple:
        lo, hi = c_int(), c_int()
        with self._lock:
            _err(self._lib.GetTemperatureRange(byref(lo), byref(hi)))
        return lo.value, hi.value

    # ------------------------------------------------------------- 配置

    def set_readout_mode_image(self, hbin: int = 1, vbin: int = 1) -> None:
        """图像读出模式（全幅）。"""
        Nx, Ny = self.detector_size
        with self._lock:
            _err(self._lib.SetReadMode(4))
            _err(self._lib.SetImage(c_int(hbin), c_int(vbin),
                                    c_int(1), c_int(Nx), c_int(1), c_int(Ny)))
        self.hbin, self.vbin = hbin, vbin
        self.Nx_ro = Nx // hbin
        self.Ny_ro = Ny // vbin
        self._buffer = np.zeros((self.Ny_ro, self.Nx_ro), dtype=np.int32)

    def set_acq_mode(self, mode: str) -> None:
        if mode not in ACQ_MODE_IDS:
            raise ValueError(f"未知采集模式 {mode}（可选: {list(ACQ_MODE_IDS)}）")
        with self._lock:
            _err(self._lib.SetAcquisitionMode(c_int(ACQ_MODE_IDS[mode])))
        self.acq_mode = mode

    def set_exposure_time(self, dt: float) -> float:
        with self._lock:
            _err(self._lib.SetExposureTime(c_float(dt)))
            exp, acc, kin = c_float(), c_float(), c_float()
            _err(self._lib.GetAcquisitionTimings(byref(exp), byref(acc), byref(kin)))
        self.exposure_time = exp.value
        return self.exposure_time

    # ---- 前置增益（与 Andor Solis 的 gain 同一旋钮）----

    #: SDK 增益档位语义（实测 600 nm、同一光路：1 : 1.9 : 4.1）
    PREAMP_GAIN_LABELS = ("1x", "2x", "4x")

    #: 本机型 GetPreAmpGain 恒返 20066 且把值写成 0（实测 0/1/2 三档都读 0），
    #: 但 SetPreAmpGain 有效（计数 1 : 1.5 : 2.6）。所以档位由软件记账。
    def get_preamp_gain(self) -> int:
        """当前前置增益档（0=1×, 1=2×, 2=4×）——软件记录，不回读硬件。"""
        return int(self.preamp_gain_index)

    def set_preamp_gain(self, index: int) -> int:
        """设置前置增益档（0=1×, 1=2×, 2=4×），返回生效档。

        这是让计数与 Andor Solis 对齐的旋钮：同一曝光下 4× 约为 1× 的 4 倍
        （实测 1 s 下 16320 vs 4263）。默认仍为 1×——强信号下 4× 会饱和。
        """
        lib = self._lib
        lib.SetPreAmpGain.argtypes = [c_int]
        with self._lock:
            _err(lib.SetPreAmpGain(c_int(int(index))))
        self.preamp_gain_index = int(index)
        return self.preamp_gain_index

    def set_shutter(self, mode: str | bool = "auto", ttl_polarity: int = 0) -> str:
        """设置快门模式并返回生效的模式（SDK2 无 GetShutter，状态为软件记录）。

        mode: "auto"（曝光期间自动开合，默认）| "open" | "closed"；
        兼容旧调用 set_shutter(True/False)（对应 open/closed）。
        ttl_polarity: 外部快门 TTL 极性（SDK 手册：0=低电平开快门，1=高电平开快门）。

        按 SDK 手册选择 API：本机 features=0x62078F 含 AC_FEATURES_SHUTTER(8)
        但无 AC_FEATURES_SHUTTEREX(16)，使用 SetShutter（typ=mode 语义）；
        若检测到 SHUTTEREX 能力则自动改用 SetShutterEx（mode=内部常开，
        extmode=外部模式，避免内部快门限速）。实测光谱仪报告无可用快门
        （ShamrockShutterIsPresent=0），命令可能无物理效果，暗场需外部挡光；
        若日后安装快门硬件，本 API 无需改动即可生效。
        """
        if isinstance(mode, bool):
            mode = "open" if mode else "closed"
        if mode not in SHUTTER_MODES:
            raise ValueError(f"未知快门模式 {mode}（可选: {list(SHUTTER_MODES)}）")
        if ttl_polarity not in (0, 1):
            raise ValueError(f"未知 TTL 极性 {ttl_polarity}（可选: 0=低电平开, 1=高电平开）")
        with self._lock:
            if self._shutterex:
                _err(self._lib.SetShutterEx(
                    c_int(ttl_polarity), c_int(1), c_int(0), c_int(0),
                    c_int(SHUTTER_MODES[mode])))
            else:
                _err(self._lib.SetShutter(
                    c_int(ttl_polarity), c_int(SHUTTER_MODES[mode]),
                    c_int(0), c_int(0)))
        self.shutter_mode = mode
        return self.shutter_mode

    def set_cooler(self, on: bool) -> None:
        with self._lock:
            if on:
                _err(self._lib.CoolerON())
            else:
                _err(self._lib.CoolerOFF())
        self.cooler_on = on

    def set_temperature_setpoint(self, t: int) -> None:
        with self._lock:
            _err(self._lib.SetTemperature(c_int(t)))
        self.temp_setpoint = t

    # ------------------------------------------------------------- 状态

    @property
    def status(self) -> str:
        s = c_int()
        with self._lock:
            _err(self._lib.GetStatus(byref(s)))
        return STATUS_NAMES.get(s.value, f"UNKNOWN({s.value})")

    def _get_temperature(self) -> int:
        t = c_int()
        with self._lock:
            retval = self._lib.GetTemperature(byref(t))
        if retval == DRV_ACQUIRING:
            raise AndorCameraError("相机正在采集，无法读取温度")
        if retval in (DRV_NOT_INITIALIZED, DRV_ERROR_ACK):
            _err(retval)
        self.temperature = t.value
        self.temperature_status = TEMP_STATUS_NAMES.get(retval, f"UNKNOWN({retval})")
        return self.temperature

    def read_temperature(self) -> int:
        return self._get_temperature()

    def read_status(self) -> str:
        """状态字（命令线程按名调用口；status 是 property 不便经队列）。"""
        return str(self.status)

    # ------------------------------------------------------------- 采集

    def acquire_frame(self, exposure: float = None, timeout: float = None) -> np.ndarray:
        """曝光一帧并返回 (Ny, Nx) int32 数组（副本）。

        exposure 为 None 时使用当前曝光时间。timeout 默认
        max(10s, 10×曝光时间+5s)。
        """
        if not self._connected:
            raise AndorCameraError("相机未连接，请先 connect()")
        if exposure is not None:
            self.set_exposure_time(exposure)
        timeout = timeout or max(10.0, 10.0 * self.exposure_time + 5.0)

        with self._lock:
            _err(self._lib.StartAcquisition())
        t0 = time.time()
        while self.status != "IDLE":
            if time.time() - t0 > timeout:
                with self._lock:
                    self._lib.AbortAcquisition()
                raise AndorCameraError(
                    f"采集超时（{timeout:.0f}s, exposure={self.exposure_time:.3f}s）")
            time.sleep(0.02)

        with self._lock:
            _err(self._lib.GetAcquiredData(
                self._buffer.ctypes.data_as(ctypes.POINTER(c_long)),
                c_uint(self._buffer.size)))
        return self._buffer.copy()

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（docs/architecture.md §4.3）——沿用原目录值（P3 迁入）。

        getter 显式指向 exposure_time：驱动没有同名 ``exposure`` 属性，
        不声明就读不到（§15.3 运行记录里的曝光快照靠它）。
        """
        return (ParamDesc(
            key="exposure", ptype="float", label="Exposure (s)", unit="s",
            apply="per_capture", vmin=1e-6, vmax=100.0, access="rw",
            decimals=4, is_exposure=True, getter="exposure_time"),)


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="andor_camera", dev_id=Dev.CAMERA, entry="AndorCamera",
    display_name="CCD", model="Newton DU920",
    capabilities=("detector",), default_transport="native",
    page="labcontrol.gui.pages.ccd_page",
    streams=(StreamDesc("frame", "frame"),
             StreamDesc("temp", "scalar"),
             StreamDesc("status", "status")),
    connect_timeout_ms=20000, disconnect_timeout_ms=15000)
