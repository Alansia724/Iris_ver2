"""Andor Shamrock 750 单色仪 —— 设备接口层（ShamrockCIF SDK）。

纯 Python + ctypes，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

用法::

    from iris_foundation.devices.andor_spec import AndorShamrock
    with AndorShamrock() as spec:
        print(spec.serial_number, spec.num_gratings, spec.gratings)
        spec.wavelength = 550.0
        print(spec.wavelength)

注意:
  * 运行前关闭 Andor Solis（设备独占）。
  * Flipper（双入口/双出口翻转镜）：早期实测 SR-2381 不支持，但
    双入口双出口机型存在——改为运行时探测（ShamrockFlipperIsPresent），
    无 Flipper 的机器读取降级 False / 写入报 NOT_AVAILABLE，不阻断连接。
"""

from ctypes import byref, c_float, c_int, create_string_buffer, windll

import numpy as np

from iris_foundation.core.contract import DriverManifest, ParamDesc, StreamDesc
from iris_foundation.core.topics import Dev

SHAMROCK_SUCCESS = 20202
SHAMROCK_COMMUNICATION_ERROR = 20201
SHAMROCK_NOT_INITIALIZED = 20275
SHAMROCK_NOT_AVAILABLE = 20292

# 本机安装位置（勿复制 DLL 进项目，版本须与已装驱动匹配）
SHAMROCK_DLL_DIR = r"C:\Program Files\Andor SDK\Shamrock64"
SHAMROCK_CIF_DLL = SHAMROCK_DLL_DIR + r"\ShamrockCIF.dll"


class AndorShamrockError(IOError):
    """Shamrock SDK 调用失败。"""


def _err(retval: int) -> int:
    if retval == SHAMROCK_SUCCESS:
        return retval
    raise AndorShamrockError(f"Shamrock SDK 错误码 {retval}")


class AndorShamrock:
    """Andor Shamrock 单色仪（ShamrockCIF 接口）的干净封装。"""

    def __init__(self, dll_dir: str = SHAMROCK_DLL_DIR):
        self.dll_dir = dll_dir
        self.dev_id = 0
        self.serial_number: str = ""
        self.num_gratings: int = 0
        self.turret: int = 0
        self.gratings: dict = {}          # {grating_id: "600g/mm 500"}
        self.focal_length: float = 0.0
        self._lib = None
        self._connected = False

    # ------------------------------------------------------------- 生命周期

    def connect(self) -> None:
        """加载 SDK 并初始化设备；无设备时报明确错误（失败也关 SDK，防句柄泄漏）。"""
        if self._connected:
            return
        lib = windll.LoadLibrary(SHAMROCK_CIF_DLL)
        try:
            _err(lib.ShamrockInitialize(b""))
            n = c_int()
            _err(lib.ShamrockGetNumberDevices(byref(n)))
            if n.value < 1:
                raise AndorShamrockError(
                    "未找到 Shamrock 设备：检查 USB 连接与驱动；"
                    "若 Andor Solis 正在运行请先关闭它")
            self._lib = lib
            self._connected = True

            buf = create_string_buffer(64)
            _err(lib.ShamrockGetSerialNumber(self.dev_id, buf))
            self.serial_number = buf.value.decode(errors="replace")

            n_g = c_int()
            _err(lib.ShamrockGetNumberGratings(self.dev_id, byref(n_g)))
            self.num_gratings = n_g.value
            self.gratings = {
                i: self._grating_info(i) for i in range(1, self.num_gratings + 1)
            }

            turret = c_int()
            _err(lib.ShamrockGetTurret(self.dev_id, byref(turret)))
            self.turret = turret.value
        except Exception:
            # Initialize 成功后的任何失败都必须 Close，否则 SDK 句柄泄漏，
            # 重连要再吃一轮 ~70s Initialize（或报 not initialized）
            self._connected = False
            self._lib = None
            try:
                lib.ShamrockClose()
            except Exception:
                pass
            raise

    def disconnect(self) -> None:
        """关闭设备并释放 SDK。"""
        if self._connected and self._lib is not None:
            try:
                self._lib.ShamrockClose()
            finally:
                self._lib = None
                self._connected = False

    def __enter__(self) -> "AndorShamrock":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------- 查询

    def _grating_info(self, grating_id: int) -> str:
        """返回光栅描述，如 '600g/mm 500'。"""
        lines = c_float()
        blaze = create_string_buffer(64)
        home = c_int()
        offset = c_int()
        _err(self._lib.ShamrockGetGratingInfo(
            self.dev_id, grating_id, byref(lines), blaze, byref(home), byref(offset)))
        return f"{lines.value:g}g/mm {blaze.value.decode(errors='replace')}"

    @property
    def wavelength(self) -> float:
        wl = c_float()
        _err(self._lib.ShamrockGetWavelength(self.dev_id, byref(wl)))
        return wl.value

    @wavelength.setter
    def wavelength(self, wl: float) -> None:
        _err(self._lib.ShamrockSetWavelength(self.dev_id, c_float(wl)))

    def get_wavelength(self) -> float:
        return self.wavelength

    def set_wavelength(self, wl: float) -> None:
        self.wavelength = wl

    @property
    def grating_id(self) -> int:
        """当前光栅号（1 起）。"""
        g = c_int()
        _err(self._lib.ShamrockGetGrating(self.dev_id, byref(g)))
        return g.value

    def set_grating(self, grating_id: int) -> None:
        """切换光栅（1 起）。"""
        _err(self._lib.ShamrockSetGrating(self.dev_id, c_int(grating_id)))

    @property
    def shutter_present(self) -> bool | None:
        """光谱仪是否安装快门；查询失败返回 None（本机实测 False）。"""
        p = c_int()
        try:
            _err(self._lib.ShamrockShutterIsPresent(self.dev_id, byref(p)))
            return bool(p.value)
        except Exception:
            return None

    # ------------------------------------------------------------- Flipper（双入口/双出口）

    # SDK 常量（ShamrockCIF.h）：flipper 索引与 port 是两个不同参数轴
    _FLIPPER_INPUT = 1            # SHAMROCK_INPUT_FLIPPER
    _FLIPPER_OUTPUT = 2           # SHAMROCK_OUTPUT_FLIPPER
    PORT_DIRECT = 0               # SHAMROCK_DIRECT_PORT（主光路直通）
    PORT_SIDE = 1                 # SHAMROCK_SIDE_PORT（侧面口）

    @property
    def entrance_flipper_present(self) -> bool:
        """入口 Flipper 是否安装；无或查询失败一律 False（不阻断连接）。"""
        return self._flipper_present(self._FLIPPER_INPUT)

    @property
    def exit_flipper_present(self) -> bool:
        """出口 Flipper 是否安装；无或查询失败一律 False。"""
        return self._flipper_present(self._FLIPPER_OUTPUT)

    def _flipper_present(self, flipper: int) -> bool:
        if not self._connected:
            return False
        p = c_int()
        try:
            _err(self._lib.ShamrockFlipperMirrorIsPresent(
                self.dev_id, c_int(flipper), byref(p)))
            return bool(p.value)
        except Exception:
            return False

    def _flipper_port(self, flipper: int) -> int:
        """读 Flipper 当前导向的 port（0=直通 1=侧口）。

        用 GetFlipperMirror（语义端口接口）；Position 变体是原始电机
        步数（本机回读 -400 未初始化），不适合做双档控制。
        """
        port = c_int()
        _err(self._lib.ShamrockGetFlipperMirror(
            self.dev_id, c_int(flipper), byref(port)))
        return port.value

    def _set_flipper_port(self, flipper: int, port: int) -> None:
        _err(self._lib.ShamrockSetFlipperMirror(
            self.dev_id, c_int(flipper), c_int(port)))

    def flipper_raw_position(self, flipper: int) -> int:
        """原始电机步数（诊断用；<0 = 未归位，此时写操作会被固件拒绝）。"""
        pos = c_int()
        _err(self._lib.ShamrockGetFlipperMirrorPosition(
            self.dev_id, c_int(flipper), byref(pos)))
        return pos.value

    def home_flipper(self, flipper: int) -> None:
        """归位/初始化 flipper 电机（上电后首次控制前必须执行；
        物理动作，镜子会回到 home 口）。"""
        _err(self._lib.ShamrockFlipperMirrorReset(self.dev_id, c_int(flipper)))

    @property
    def entrance_flipper(self) -> int:
        """入口 Flipper 导向：0 = 直通轴向，1 = 侧面口。"""
        return self._flipper_port(self._FLIPPER_INPUT)

    @entrance_flipper.setter
    def entrance_flipper(self, port: int) -> None:
        self._set_flipper_port(self._FLIPPER_INPUT, int(port))

    @property
    def exit_flipper(self) -> int:
        """出口 Flipper 导向：0 = 直通轴向，1 = 侧面口。"""
        return self._flipper_port(self._FLIPPER_OUTPUT)

    @exit_flipper.setter
    def exit_flipper(self, port: int) -> None:
        self._set_flipper_port(self._FLIPPER_OUTPUT, int(port))

    # 兼容旧别名（agent 脚本语义化调用；get_* 供命令线程按名调用）
    def set_entrance_flipper(self, port: int) -> None:
        self.entrance_flipper = port

    def set_exit_flipper(self, port: int) -> None:
        self.exit_flipper = port

    def get_entrance_flipper(self) -> int:
        return self.entrance_flipper

    def get_exit_flipper(self) -> int:
        return self.exit_flipper

    # ------------------------------------------------------------- 波长标定

    @property
    def pixel_width(self) -> float:
        """标定用探测器像素宽度（µm）。本机相机实测 26.0 µm。"""
        w = c_float()
        _err(self._lib.ShamrockGetPixelWidth(self.dev_id, byref(w)))
        return w.value

    @pixel_width.setter
    def pixel_width(self, width: float) -> None:
        _err(self._lib.ShamrockSetPixelWidth(self.dev_id, c_float(width)))

    @property
    def number_pixels(self) -> int:
        """标定用探测器水平像素数（本机 1024）。"""
        n = c_int()
        _err(self._lib.ShamrockGetNumberPixels(self.dev_id, byref(n)))
        return n.value

    @number_pixels.setter
    def number_pixels(self, n: int) -> None:
        _err(self._lib.ShamrockSetNumberPixels(self.dev_id, c_int(n)))

    def calibration_wavelengths(self, n_pixels: int | None = None) -> np.ndarray:
        """返回每像素对应的波长（nm）数组，长度 n_pixels（默认 1024）。

        SDK: ShamrockGetCalibration —— 与 Solis 显示的波长轴同一份数据，
        随当前光栅/波长/探测器配置变化；实测为单调数组（增或减）。
        """
        n = n_pixels or 1024
        arr = (c_float * n)()
        _err(self._lib.ShamrockGetCalibration(self.dev_id, arr, n))
        return np.array(arr)

    @property
    def calibration_coefficients(self) -> tuple[float, float, float, float]:
        """像素→波长多项式系数 (A, B, C, D)：λ = A + B·x + C·x² + D·x³。"""
        A, B, C, D = c_float(), c_float(), c_float(), c_float()
        _err(self._lib.ShamrockGetPixelCalibrationCoefficients(
            self.dev_id, byref(A), byref(B), byref(C), byref(D)))
        return A.value, B.value, C.value, D.value

    def configure_calibration(self, pixel_width_um: float, n_pixels: int) -> None:
        """设置探测器几何参数（µm 像素宽、像素数）——GetCalibration 的前置条件。

        实测：这些参数不跨进程持久（新进程读回 0/0，此时 GetCalibration 返回全 0），
        每次连接后必须先设置再读校准。像素宽取相机 GetPixelSize 实测值。
        """
        _err(self._lib.ShamrockSetPixelWidth(self.dev_id, c_float(pixel_width_um)))
        _err(self._lib.ShamrockSetNumberPixels(self.dev_id, c_int(n_pixels)))

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（docs/architecture.md §4.3）——沿用原目录值（P3 迁入）。"""
        return (ParamDesc(
            key="wavelength", ptype="float", label="Wavelength (nm)", unit="nm",
            apply="write", vmin=0.0, vmax=5000.0, access="rw",
            decimals=2, settle_ms=1500,
            setter="set_wavelength", readback_tol=0.05),)


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="andor_spec", dev_id=Dev.SPEC, entry="AndorShamrock",
    display_name="Spectrograph", model="Shamrock 750",
    capabilities=("spectrometer",), default_transport="native",
    page="labcontrol.gui.pages.spec_page",
    streams=(StreamDesc("calibration", "spectrum"),),
    # ShamrockInitialize 固有耗时 ~70s（FTDI USB 枚举/握手，实测恒定，
    # 与相机是否连接无关）；看门狗必须容得下，只提示不中止轮询。
    connect_timeout_ms=180000)
