"""DvpCamera —— Do3think M3S230-H-O2C USB3 Vision 相机（GenTL / harvesters）。

接口模式与 AndorCamera 一致（connect / disconnect / acquire_frame /
exposure_time / with 支持），供 agent 脚本、Jupyter、GUI 共用。

SDK：标准 GenTL 传输层 ``DVPCameraTL64.cti``（安装于 DVP2 x64），
经 Python ``harvesters`` 消费；节点控制（曝光/增益/像素格式）走 GenICam 标准。

用法::

    from iris_foundation.devices.dvp_camera import DvpCamera
    with DvpCamera() as cam:
        print(cam.model, cam.serial_number, cam.detector_size)
        cam.exposure_time = 0.05
        frame = cam.acquire_frame()   # (H, W, 3) uint8 RGB

注意:
  * 运行前关闭 BasedCam3 / 其他占用相机的软件（USB3 设备独占）。
  * 曝光时间统一以秒为单位（内部转 GenICam 微秒）。
  * 本机 DVP GenTL 的 IFGetDeviceInfo 只有部分实现：serial/version 等
    信息读不了（读 ``DeviceInfo.serial_number`` 会抛
    ``InvalidParameterException``），序列号改从 ``user_defined_name``
    获取；这不影响打开与采集（已真机验证）。
  * acquire_frame 临时改动的曝光会在结束时恢复原值（设备状态改动恢复原状）。
  * 采集调用不保证线程安全，请只在单一采集线程中使用（GUI 中由 worker 承担）。
"""

from __future__ import annotations

import threading

import numpy as np

from iris_foundation.core.contract import DriverManifest, ParamDesc, StreamDesc
from iris_foundation.core.topics import Dev

# 本机安装位置（勿复制 DLL 进项目）
CTI_PATH = r"C:\Program Files (x86)\Do3think\DVP2 x64\DVPCameraTL64.cti"

_patched_flush = False


def _patch_dvp_flush() -> None:
    """DVP GenTL 的 DSFlushQueue 在空队列时返回 GC_ERR_INVALID_HANDLE，
    harvesters 清理（destroy/reset）会因此抛 InvalidHandleException 刷屏。
    将 flush_buffer_queue 包成容错版（只影响本进程 harvesters 的清理路径）。"""
    global _patched_flush
    if _patched_flush:
        return
    from genicam import gentl as _gentl
    orig = _gentl.DataStream.flush_buffer_queue

    def _patched(self, operation):
        try:
            return orig(self, operation)
        except Exception:
            return None

    _gentl.DataStream.flush_buffer_queue = _patched
    _patched_flush = True


class DvpCameraError(IOError):
    """DVP/GenTL 调用失败。"""


class DvpCamera:
    """Do3think USB3 Vision 相机的干净接口（harvesters/GenTL）。"""

    def __init__(self, cti_path: str = CTI_PATH):
        self.cti_path = cti_path
        self._lock = threading.Lock()
        self._harvester = None
        self._ia = None          # harvesters ImageAcquirer
        self._connected = False

        self.model: str = ""
        self.vendor: str = ""
        self.serial_number: str = ""
        self.detector_size: tuple = (0, 0)   # (W, H)，与 AndorCamera 一致
        self.pixel_format: str = ""
        self.exposure_time: float = 0.05     # 秒（connect 后为设备回读值）
        # 相机实际参数范围（connect 时从节点读回；0 = 未知，此时不 clamp）
        self.exposure_min: float = 0.0
        self.exposure_max: float = 0.0
        self.exposure_increment: float = 0.0   # 秒（本机 20µs，写入必须为其整数倍）
        self.gain_min: float = 0.0
        self.gain_max: float = 0.0

    # ------------------------------------------------------------- 生命周期

    def connect(self) -> None:
        if self._connected:
            return
        _patch_dvp_flush()
        try:
            from harvesters.core import Harvester
            h = Harvester()
            h.add_file(self.cti_path)
            h.update()
            if not h.device_info_list:
                h.reset()
                raise DvpCameraError(
                    "未找到 DVP 相机：检查 USB 连接与驱动；"
                    "若 BasedCam3 正在运行请先关闭它（设备独占）")
            dev = h.device_info_list[0]
            self._harvester = h
            self._ia = h.create(0)
            self.model = str(self._ia.device.model)
            try:
                self.vendor = str(self._ia.device.vendor)
            except Exception:
                self.vendor = ""
            # DVP GenTL 不支持 serial_number 的 GetInfo（读会抛异常），
            # 序列号从 user_defined_name 取（display_name 的 @ 后缀兜底）。
            try:
                self.serial_number = str(dev.user_defined_name or "")
            except Exception:
                self.serial_number = ""
            if not self.serial_number:
                dn = str(dev.display_name)
                self.serial_number = dn.split("@", 1)[1] if "@" in dn else dn
            nm = self._ia.remote_device.node_map
            self.detector_size = (int(self._node(nm, "Width").value),
                                  int(self._node(nm, "Height").value))
            self.pixel_format = str(self._node(nm, "PixelFormat").value)
            # 只读回当前曝光，不写设备（保持设备状态不变）
            self.exposure_time = self._read_exposure(nm)
            self._read_parameter_ranges(nm)
            self._connected = True
        except DvpCameraError:
            self._cleanup()
            raise
        except Exception as exc:
            self._cleanup()
            raise DvpCameraError(
                f"DVP 相机连接失败：{exc}\n"
                "提示：USB3 相机为独占设备——请先关闭 BasedCam3 与本程序的其他窗口；"
                "若仍失败请拔插相机 USB 后重试") from exc

    def disconnect(self) -> None:
        self._cleanup()

    def reconnect(self) -> None:
        """重建 GenTL 采集会话（disconnect + connect）。

        DVP 相机在采集运行中写入曝光/增益后，其 GenTL 会话可能进入坏状态
        （预览持续失败），此时需要完全重建才能恢复；参数在重建后由调用方
        重新写入。
        """
        self._cleanup()
        self.connect()

    def _cleanup(self) -> None:
        if self._ia is not None:
            try:
                self._ia.stop()
            except Exception:
                pass
            try:
                self._ia.destroy()
            except Exception:
                pass
            self._ia = None
        if self._harvester is not None:
            try:
                self._harvester.reset()
            except Exception:
                pass
            self._harvester = None
        self._connected = False

    def __enter__(self) -> "DvpCamera":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------- 节点访问

    @staticmethod
    def _node(nm, name: str):
        """按名称取 GenICam 节点（兼容 get_node / 属性两种访问）。"""
        try:
            return nm.get_node(name)
        except Exception:
            return getattr(nm, name)

    def _read_exposure(self, nm=None) -> float:
        """读回曝光时间（秒）。ExposureTime 单位 µs；ExposureTimeAbs 为秒。"""
        nm = nm or self._ia.remote_device.node_map
        for name, is_us in (("ExposureTime", True), ("ExposureTimeAbs", False)):
            try:
                node = self._node(nm, name)
                return float(node.value) * (1e-6 if is_us else 1.0)
            except Exception:
                continue
        return self.exposure_time

    def _read_parameter_ranges(self, nm=None) -> None:
        """读回曝光/增益节点的 min/max（秒/dB），读不到保持 0（不 clamp）。"""
        nm = nm or self._ia.remote_device.node_map
        for name, scale in (("ExposureTime", 1e-6), ("ExposureTimeAbs", 1.0)):
            try:
                node = self._node(nm, name)
                self.exposure_min = float(node.min) * scale
                self.exposure_max = float(node.max) * scale
                self.exposure_increment = float(node.inc) * scale
                break
            except Exception:
                continue
        try:
            node = self._node(nm, "Gain")
            self.gain_min = float(node.min)
            self.gain_max = float(node.max)
        except Exception:
            pass

    # ------------------------------------------------------------- 配置

    def quantize_exposure(self, dt: float) -> float:
        """把曝光值（秒）限制到相机范围并量化到步进（本机 20µs）。

        GUI 用此值做显示/下发，保证界面值与设备实际值一致。
        """
        dt = float(dt)
        if self.exposure_min > 0:
            dt = max(dt, self.exposure_min)
        if self.exposure_max > 0:
            dt = min(dt, self.exposure_max)
        if self.exposure_increment > 0 and self.exposure_min > 0:
            dt = self.exposure_min + round(
                (dt - self.exposure_min) / self.exposure_increment) * self.exposure_increment
        return dt

    def set_exposure_time(self, dt: float) -> float:
        """设置曝光（秒），返回 SDK 回读的实际值。

        超相机范围的值会被限制到边界（clamp：成员范围 + 写入时实时查询节点
        范围兜底），不会抛异常。
        """
        if not self._connected:
            raise DvpCameraError("相机未连接，请先 connect()")
        dt = float(dt)
        if self.exposure_min > 0:
            dt = max(dt, self.exposure_min)
        if self.exposure_max > 0:
            dt = min(dt, self.exposure_max)
        with self._lock:
            nm = self._ia.remote_device.node_map
            node, is_us, last_exc = None, True, None
            for name, us in (("ExposureTime", True), ("ExposureTimeAbs", False)):
                try:
                    node = self._node(nm, name)
                    v = int(round(dt * (1e6 if us else 1.0)))
                    # 写入前实时 clamp + 量化到节点步进
                    # （本机 ExposureTime: min 20µs, inc 20µs，非整数倍写入
                    #   会抛 OutOfRangeException——这是此前“不支持节点”的根因）
                    try:
                        mn, mx, inc = float(node.min), float(node.max), float(node.inc)
                    except Exception:
                        mn = mx = inc = 0.0
                    if mn:
                        v = max(v, int(mn))
                    if mx:
                        v = min(v, int(mx))
                    if inc and mn:
                        v = int(mn) + int(round((v - int(mn)) / inc)) * int(inc)
                        if mx:
                            v = min(v, int(mx))
                    node.value = v
                    is_us = us
                    break
                except Exception as exc:
                    last_exc = exc
                    node = None
            if node is None:
                raise DvpCameraError(
                    f"曝光写入失败（ExposureTime 不可用）：{last_exc}")
            self.exposure_time = float(node.value) * (1e-6 if is_us else 1.0)
            return self.exposure_time

    @property
    def gain(self) -> float:
        """当前增益（dB，GenICam Gain 节点回读）。"""
        if not self._connected:
            raise DvpCameraError("相机未连接，请先 connect()")
        with self._lock:
            nm = self._ia.remote_device.node_map
            return float(self._node(nm, "Gain").value)

    def set_gain(self, g: float) -> float:
        """设置增益，返回回读值（超范围自动 clamp，含实时节点范围兜底）。"""
        if not self._connected:
            raise DvpCameraError("相机未连接，请先 connect()")
        g = float(g)
        if self.gain_min > 0:
            g = max(g, self.gain_min)
        if self.gain_max > 0:
            g = min(g, self.gain_max)
        with self._lock:
            nm = self._ia.remote_device.node_map
            node = self._node(nm, "Gain")
            try:
                mn, mx = float(node.min), float(node.max)
            except Exception:
                mn = mx = 0.0
            v = g
            if mn:
                v = max(v, mn)
            if mx:
                v = min(v, mx)
            node.value = v
            return float(node.value)

    # ------------------------------------------------------------- 采集

    def acquire_frame(self, exposure: float | None = None,
                      timeout: float = 10.0) -> np.ndarray:
        """采一帧并返回 (H, W, 3) uint8 RGB（Bayer 源做 2×2 解码）。

        exposure 为 None 时使用当前曝光；否则临时设置并在结束时恢复原值。
        timeout 为帧等待超时（秒）。
        """
        if not self._connected:
            raise DvpCameraError("相机未连接，请先 connect()")
        prev = self.exposure_time
        try:
            if exposure is not None:
                self.set_exposure_time(exposure)
            self._ia.start(run_as_thread=True)
            try:
                with self._ia.fetch(timeout=timeout) as buf:
                    comp = buf.payload.components[0]
                    raw = np.asarray(comp.data)
                    frame = self._to_rgb(raw)
            finally:
                try:
                    self._ia.stop()
                except Exception:
                    pass
        finally:
            if exposure is not None:
                try:
                    self.set_exposure_time(prev)
                except Exception:
                    pass
        return frame

    def _to_rgb(self, raw: np.ndarray) -> np.ndarray:
        """像素格式 → (H, W, 3) uint8 RGB。"""
        pf = self.pixel_format
        W, H = self.detector_size
        if pf == "BGR8Packed":                 # 本机默认（实测 1920×1200×3 平铺）
            img = raw.reshape((H, W, 3))
            return img[:, :, ::-1].copy()      # BGR → RGB
        if pf == "RGB8Packed":
            return raw.reshape((H, W, 3)).copy()
        if pf in ("Mono8",):
            return np.repeat(raw.reshape((H, W))[..., None], 3, axis=2)
        if pf.startswith("Mono"):              # Mono12/14/16：高 8 位转 uint8
            img = raw.reshape((H, W))
            shift = max(0, img.itemsize * 8 - 8)
            img8 = (img >> shift).astype(np.uint8)
            return np.repeat(img8[..., None], 3, axis=2)
        if "Bayer" in pf:                      # 2×2 RGGB 采样解码（预览足够）
            h, w = raw.shape
            h2, w2 = (h // 2) * 2, (w // 2) * 2
            b = raw[:h2, :w2].astype(np.uint16)
            rgb = np.empty((h2 // 2, w2 // 2, 3), dtype=np.uint8)
            rgb[..., 0] = b[0::2, 0::2]                        # R
            rgb[..., 1] = (b[0::2, 1::2] + b[1::2, 0::2]) // 2  # G
            rgb[..., 2] = b[1::2, 1::2]                        # B
            return rgb
        raise DvpCameraError(
            f"不支持的像素格式 {pf}（原始 shape {raw.shape}）")

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（docs/architecture.md §4.3）——沿用原目录值（P3 迁入）。"""
        return (ParamDesc(
                    key="exposure", ptype="float", label="Exposure (s)", unit="s",
                    apply="per_capture", vmin=20e-6, vmax=21.0, access="rw",
                    decimals=5, is_exposure=True),
                ParamDesc(
                    key="gain", ptype="float", label="Gain", unit="",
                    apply="write", vmin=1.0, vmax=100.0, access="rw",
                    decimals=2, settle_ms=100,
                    setter="set_gain", readback_tol=0.01))


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="dvp_camera", dev_id=Dev.DVP, entry="DvpCamera",
    display_name="CMOS", model="Do3think M3S230",
    capabilities=("detector",), default_transport="gentl",
    page="labcontrol.gui.pages.cmos_page",
    streams=(StreamDesc("frame", "frame"),),
    supports_reconnect=True,
    connect_timeout_ms=20000, disconnect_timeout_ms=15000)
