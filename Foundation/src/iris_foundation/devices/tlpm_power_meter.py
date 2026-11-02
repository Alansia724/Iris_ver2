"""Thorlabs PM100USB 光功率计驱动（能力 power_meter；参数 wavelength/avg_cnt）。

传输：Thorlabs 官方 IVI 驱动 ``TLPM_64.dll``（ctypes 直包，与 Andor 驱动同一
套路）。该 DLL 自带 USB 传输（SETUPAPI 直摸 TMC 接口），**不依赖 VISA**——
机器上即使只有 ThorlabsUSBDevice 驱动绑定也能用。DLL 原位加载（不复制）：
``C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Bin\\TLPM_64.dll``（随
Thorlabs Optical Power Monitor 软件安装）。

⚠ 设备独占：Thorlabs 官方软件（OPM）开着时 connect 会报资源错，先关它
（同 Andor Solis 纪律）。

签名依据：Thorlabs TLPM 编程手册（IVI 风格 C API）；返回码 0 = 成功，
负数 = 错误（经 TLPM_errorMessage 转可读文本）。
"""

from __future__ import annotations

import ctypes
import logging

from iris_foundation.core.contract import DriverManifest, ParamDesc

logger = logging.getLogger("iris_foundation.devices.tlpm_power_meter")

#: TLPM_64.dll 原位路径（Thorlabs OPM 软件安装点；不复制，避免版本漂移）
_DLL_PATH = (r"C:\Program Files\IVI Foundation\VISA\Win64\Bin"
             r"\TLPM_64.dll")

_BUFFER = 1024


class TlpmError(IOError):
    """TLPM 调用失败（负返回码 → 可读消息）。"""


class TlpmPowerMeter:
    """Thorlabs PM100USB（能力 power_meter；可扫参数 wavelength / avg_cnt）。

    Parameters
    ----------
    serial : str
        设备序列号过滤（如 "P2001666"）；空串 = 取第一台找到的。
        台面上多台功率计时用它区分。
    dll_path : str
        TLPM_64.dll 路径（测试注入假 DLL 用，默认官方原位）。
    """

    def __init__(self, serial: str = "", dll_path: str = _DLL_PATH):
        self._serial = serial
        self._dll_path = dll_path
        self._dll = None
        self._h: int | None = None
        self.idn: str = ""
        self.sensor: str = ""          # 传感器型号（如 S121C）
        self.resource: str = ""        # 连接的资源串（诊断用）

    # ------------------------------------------------------------ DLL 绑定

    def _bind(self):
        """加载 DLL 并钉死函数签名。

        签名唯一依据：TLPM.h（Thorlabs IVI 驱动自带头文件，
        ``C:\\Program Files\\IVI Foundation\\VISA\\Win64\\include\\TLPM.h``）。
        猜签名会在设备会话上访问违例——先读头文件再写绑定。
        """
        dll = ctypes.CDLL(self._dll_path)
        p_u32 = ctypes.POINTER(ctypes.c_uint32)
        p_i16 = ctypes.POINTER(ctypes.c_int16)
        p_f64 = ctypes.POINTER(ctypes.c_double)
        p_h = ctypes.POINTER(ctypes.c_void_p)
        cbuf = ctypes.c_char_p
        dll.TLPM_findRsrc.argtypes = [ctypes.c_void_p, p_u32]
        dll.TLPM_getRsrcName.argtypes = [ctypes.c_void_p, ctypes.c_uint32, cbuf]
        dll.TLPM_init.argtypes = [ctypes.c_char_p, ctypes.c_int16,
                                  ctypes.c_int16, p_h]
        dll.TLPM_close.argtypes = [ctypes.c_void_p]
        # (vi, manufacturer[], deviceName[], serialNumber[], firmwareRev[])
        dll.TLPM_identificationQuery.argtypes = [ctypes.c_void_p] + [cbuf] * 4
        # (vi, ViPReal64 power)
        dll.TLPM_measPower.argtypes = [ctypes.c_void_p, p_f64]
        # (vi, ViInt16 attribute, ViPReal64 value)；attribute ∈ TLPM_ATTR_*
        dll.TLPM_getWavelength.argtypes = [ctypes.c_void_p, ctypes.c_int16, p_f64]
        dll.TLPM_setWavelength.argtypes = [ctypes.c_void_p, ctypes.c_double]
        # (vi, ViPInt16 averageCount)
        dll.TLPM_getAvgCnt.argtypes = [ctypes.c_void_p, p_i16]
        dll.TLPM_setAvgCnt.argtypes = [ctypes.c_void_p, ctypes.c_int16]
        # (vi, name[], snr[], message[], ViPInt16 type, ViPInt16 stype, ViPInt16 flags)
        dll.TLPM_getSensorInfo.argtypes = ([ctypes.c_void_p] + [cbuf] * 3
                                           + [p_i16] * 3)
        dll.TLPM_errorMessage.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          cbuf]
        return dll

    def _check(self, rc: int) -> int:
        """返回码 0 = OK；负数 → TlpmError（带 TLPM_errorMessage 文本）。"""
        if rc == 0:
            return rc
        buf = ctypes.create_string_buffer(_BUFFER)
        self._dll.TLPM_errorMessage(
            ctypes.c_void_p(self._h) if self._h else None, rc, buf)
        raise TlpmError(f"TLPM rc={rc}: {buf.value.decode(errors='replace').strip()}")

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> str:
        """枚举 → 打开设备 → 握手。返回 IDN 串。"""
        self._dll = self._bind()
        h = None
        count = ctypes.c_uint32()
        self._check(self._dll.TLPM_findRsrc(h, ctypes.byref(count)))
        if count.value == 0:
            raise TlpmError("未找到 TLPM 设备（检查 USB / 官方软件是否已关闭）")
        picked = None
        for i in range(count.value):
            name = ctypes.create_string_buffer(_BUFFER)
            self._check(self._dll.TLPM_getRsrcName(h, i, name))
            res = name.value.decode(errors="replace")
            if not self._serial or self._serial in res:
                picked = res
                break
        if picked is None:
            raise TlpmError(
                f"找到 {count.value} 台 TLPM 设备，但没有序列号匹配 "
                f"{self._serial!r}")
        # 打开：idQuery=True（握手），reset=False（不动设备当前配置）
        sh = ctypes.c_void_p()
        self._check(self._dll.TLPM_init(picked.encode(), True, False,
                                        ctypes.byref(sh)))
        self._h = sh.value
        self.resource = picked
        # 身份：四缓冲区签名（厂商/型号/序列号/固件版本）
        mfr = ctypes.create_string_buffer(256)
        dev = ctypes.create_string_buffer(256)
        serno = ctypes.create_string_buffer(256)
        fw = ctypes.create_string_buffer(256)
        self._check(self._dll.TLPM_identificationQuery(
            ctypes.c_void_p(self._h), mfr, dev, serno, fw))
        self.idn = " ".join(x.value.decode(errors="replace").strip()
                            for x in (mfr, dev, serno, fw) if x.value)
        # 传感器：name/snr/message + type/stype/flags
        name = ctypes.create_string_buffer(256)
        snr = ctypes.create_string_buffer(256)
        msg = ctypes.create_string_buffer(_BUFFER)
        t16 = ctypes.c_int16()
        st16 = ctypes.c_int16()
        fl16 = ctypes.c_int16()
        self._check(self._dll.TLPM_getSensorInfo(
            ctypes.c_void_p(self._h), name, snr, msg,
            ctypes.byref(t16), ctypes.byref(st16), ctypes.byref(fl16)))
        self.sensor = (name.value.decode(errors="replace").strip()
                       or snr.value.decode(errors="replace").strip())
        logger.info("TLPM 已连接: %s（传感器 %s，%s）",
                    self.idn, self.sensor, picked)
        return self.idn

    def disconnect(self) -> None:
        if self._h is not None:
            try:
                self._dll.TLPM_close(ctypes.c_void_p(self._h))
            finally:
                self._h = None

    def __enter__(self) -> "TlpmPowerMeter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------ 能力：power_meter

    def power(self) -> float:
        """当前功率读数（W；受平均次数/量程/波长补偿影响）。"""
        v = ctypes.c_double()
        self._check(self._dll.TLPM_measPower(ctypes.c_void_p(self._h),
                                             ctypes.byref(v)))
        return v.value

    # ------------------------------------------------------------ 可扫参数

    @property
    def wavelength(self) -> float:
        """探测器波长补偿（nm）——按入射激光波长设置，影响读数标定。

        attribute = TLPM_ATTR_SET_VAL(0)：当前设定值（MIN/MAX 可查量程）。
        """
        v = ctypes.c_double()
        self._check(self._dll.TLPM_getWavelength(ctypes.c_void_p(self._h),
                                                 0, ctypes.byref(v)))
        return v.value

    @wavelength.setter
    def wavelength(self, nm: float) -> None:
        self.set_wavelength(nm)

    # 显式方法包装：services.call_async / 通用写参服务按方法名寻址
    # （§4.3 写侧声明 setter=/getter= 指向这些名字）

    def set_wavelength(self, nm: float) -> None:
        self._check(self._dll.TLPM_setWavelength(ctypes.c_void_p(self._h),
                                                 float(nm)))

    def get_wavelength(self) -> float:
        return self.wavelength

    def set_avg_cnt(self, n: int) -> None:
        self._check(self._dll.TLPM_setAvgCnt(ctypes.c_void_p(self._h), int(n)))

    def get_avg_cnt(self) -> int:
        return self.avg_cnt

    @property
    def avg_cnt(self) -> int:
        """平均点数（读数平滑；1 = 无平均）。ViInt16。"""
        v = ctypes.c_int16()
        self._check(self._dll.TLPM_getAvgCnt(ctypes.c_void_p(self._h),
                                             ctypes.byref(v)))
        return v.value

    @avg_cnt.setter
    def avg_cnt(self, n: int) -> None:
        self._check(self._dll.TLPM_setAvgCnt(ctypes.c_void_p(self._h), int(n)))

    def wavelength_limits(self) -> tuple[float, float]:
        """传感器支持的波长补偿范围（nm），TLPM_ATTR_MIN/MAX_VAL 查询。

        这是设备的硬事实；params() 里的静态量程是它的保守包络。
        """
        lo = ctypes.c_double()
        hi = ctypes.c_double()
        self._check(self._dll.TLPM_getWavelength(
            ctypes.c_void_p(self._h), 1, ctypes.byref(lo)))   # TLPM_ATTR_MIN_VAL
        self._check(self._dll.TLPM_getWavelength(
            ctypes.c_void_p(self._h), 2, ctypes.byref(hi)))   # TLPM_ATTR_MAX_VAL
        return lo.value, hi.value

    # ------------------------------------------------------------ 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（§4.3）。

        量程：wavelength 200–1100 nm 是 PM100D 系固件可设包络（当前传感器
        S121C 实际 400–1100，真机可经 wavelength_limits() 查询，固件再按
        传感器收紧）；avg_cnt 1–10000 照手册。写侧声明指向显式方法包装；
        wavelength 设定是精确写，readback_tol 收紧到 0.05 nm。
        硬事实只写此处一份。
        """
        return (
            ParamDesc(key="wavelength", ptype="float", label="Wavelength (nm)",
                      unit="nm", apply="write", vmin=200.0, vmax=1100.0,
                      access="rw", decimals=1, settle_ms=50,
                      setter="set_wavelength", getter="get_wavelength",
                      readback_tol=0.05),
            ParamDesc(key="avg_cnt", ptype="int", label="Averaging (pts)",
                      unit="pt", apply="write", vmin=1.0, vmax=10000.0,
                      access="rw", decimals=0, settle_ms=50,
                      setter="set_avg_cnt", getter="get_avg_cnt",
                      readback_tol=0.5),
        )


# ---- 平台合同自声明（docs/architecture.md §6 / §14）----
DRIVER_MANIFEST = DriverManifest(
    driver="tlpm_power_meter", dev_id="power_meter", entry="TlpmPowerMeter",
    display_name="PowerMeter", model="Thorlabs PM100USB",
    capabilities=("power_meter",), default_transport="native",
    page="labcontrol.gui.pages.power_meter_page",
    connect_timeout_ms=10000)
