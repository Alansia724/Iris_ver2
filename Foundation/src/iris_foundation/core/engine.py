"""ScanEngine：1D 参数扫描状态机（自 gui/scan_panel 迁入，GUI 与 CLI 共用）。

宿主事件循环驱动的编排引擎（**零 Qt**，docs/architecture.md §18）：
延时统一经注入的 ``after(ms, fn)``——GUI 传 ``QTimer.singleShot``，
无头宿主用引擎自带的 ``HeadlessLoop``（CLI 拿它当泵）。
  状态机：写参(如需) → 稳定延时 → 逐帧采集 → 写点(HDF5 增量) → 发布
  scan.point → 下一项；中止在点间隙生效；采帧等待带看门狗超时。

引擎经注入 services 调用方提供的异步管线（键按设备参数寻址）：
  capture:<dev>       采集（曝光随单帧显式下发，DVP 20µs 量化由设备层处理）
  write:<dev>.<key>   写参（写→稳定延时）；read:<dev>.<key> 回读记录
capture 服务必须与引擎同线程同步 hub.publish（DataHub 无锁）。

落盘：单文件 HDF5（schema v1，见 core.storage）
  /<参数名>     扫描轴（用户命名，n,）
  /<结果名>     每点监视器原始数据栈（用户命名，n*K, *payload.shape）
  /point_readback /point_time   (n,)
  attrs         schema_version/参数/输出监视器/数据主题/完成点数等，
                以及运行身份与溯源（run_id/操作者/设备清单/参数快照）

落盘经 core.run.RunRecorder 唯一落盘路径（命名/根 attrs/溯源/运行索引
一处收口，docs §15）；结束原因原样入档（reason attr：done / aborted /
error: …），由 RunRecorder 映射为 status=ok / aborted / failed。

实时发布：每帧发布 Topics.SCAN_POINT {"i","k","x","frame",…}，
扫描图监视器据此增量构建数据立方体。
"""

from __future__ import annotations

import heapq
import logging
import time
from pathlib import Path

import numpy as np

from iris_foundation.core import registry
from iris_foundation.core.device_services import WriteHandle
from iris_foundation.core.hub import DataHub
from iris_foundation.core.params import RESERVED_NAMES, ScanParam
from iris_foundation.core.run import (STATUS_ABORTED, STATUS_FAILED, STATUS_OK,
                                 RunRecorder)
from iris_foundation.core.topics import Dev, Topics

logger = logging.getLogger("iris_foundation.core.engine")


def status_of(reason: str | None) -> str:
    """引擎结束原因 → 运行状态（§15.2；未知原因一律 failed，不冒充成功）。"""
    if reason == "done":
        return STATUS_OK
    if reason == "aborted":
        return STATUS_ABORTED
    return STATUS_FAILED


class Signal:
    """极简信号（取代 pyqtSignal，让 core 不依赖 Qt）。

    只实现消费方用到的 connect/emit；一个订阅者抛异常不得影响其余订阅者
    （与 DataHub 的派发隔离同一纪律）。
    """

    def __init__(self):
        self._slots: list = []

    def connect(self, fn) -> None:
        if fn not in self._slots:
            self._slots.append(fn)

    def emit(self, *args) -> None:
        for fn in tuple(self._slots):
            try:
                fn(*args)
            except Exception:
                logger.exception("信号回调失败（已隔离，其余订阅者不受影响）")


class HeadlessLoop:
    """无头事件循环：延时回调队列 + 泵（CLI 与直接构造引擎的调用方用）。

    GUI 宿主不必用它——把 ``QTimer.singleShot`` 直接传给 ``after`` 即可
    （签名同为 after(ms, fn)），所以这里不为 GUI 建类、也不造调度框架。
    延时按真实时钟走：写参后的稳定延时必须真等，不能压成“立即”。
    """

    def __init__(self):
        self._q: list = []          # [(到期单调时刻, 序号, 回调)]
        self._n = 0

    def after(self, ms: int, fn) -> None:
        heapq.heappush(self._q,
                       (time.monotonic() + max(0, int(ms)) / 1000.0,
                        self._n, fn))
        self._n += 1

    def wait_until(self, cond, timeout_s: float = 3600.0,
                   tick_s: float = 0.005) -> bool:
        """泵到 cond() 成立；超时返回 False。"""
        deadline = time.monotonic() + timeout_s
        while not cond():
            now = time.monotonic()
            if now > deadline:
                return False
            if self._q and self._q[0][0] <= now:
                _, _, fn = heapq.heappop(self._q)
                try:
                    fn()
                except Exception:
                    logger.exception("无头循环回调失败（已隔离）")
            elif self._q:
                time.sleep(min(max(tick_s, self._q[0][0] - now), 0.05))
            else:
                time.sleep(tick_s)
        return True


class ScanEngine:
    """事件循环状态机：按 ScanParam 编排采集，输出=所选监视器的原始数据。"""

    def __init__(self, hub: DataHub, param: ScanParam, param_dev: str,
                 param_name: str, axis: np.ndarray, frames_per_point: int,
                 settle_ms: int, path: Path, services: dict,
                 out_topic: str, out_monitor: str, result_name: str,
                 base_exposure: float, param_display: str = "",
                 capture_dev: str | None = None,
                 recorder: RunRecorder | None = None,
                 after=None):
        # 信号（原 pyqtSignal；实例级持有，不依赖 Qt）
        self.point_done = Signal()      # (点序号, 参数值)
        self.progress = Signal()
        self.logged = Signal()
        self.finished = Signal()        # done / aborted / error: …
        self._hub = hub
        self._param = param
        self._param_dev = param_dev
        self._param_name = param_name
        self._param_display = param_display   # 原始名（未 sanitize，显示用）
        self._axis = np.asarray(axis, dtype=float)
        self._frames_pp = int(frames_per_point)
        self._settle_ms = int(settle_ms)
        # 落盘经唯一路径（§15）：给了 recorder 就用它的文件与名字（path 以
        # recorder 为准，两处各自定名即漂移）；没给则 start() 就地补一个。
        self._run = recorder
        self._path = Path(recorder.path) if recorder is not None else Path(path)
        self._svc = services
        self._topic = out_topic                  # 输出监视器的数据主题
        self._out_monitor = out_monitor          # 输出监视器显示名（attrs）
        self._result_name = result_name
        # 采集设备：显式传入优先；否则按流主题归属反查（registry 从驱动
        # manifest.streams 派生，§16.3——新驱动声明了流即认得，不再靠手写表）
        self._capture_dev = (capture_dev or registry.device_of_topic(out_topic)
                             or Dev.CAMERA)
        self._base_exposure = float(base_exposure)
        self._h5 = None
        self._i = 0
        self._k = 0
        self._state = "idle"         # idle/writing/waiting_frame/finished
        self._finish_reason: str | None = None
        self._abort = False
        self._write_handle: WriteHandle | None = None   # 异步写轮询（P6）
        self._t0 = time.time()
        # 延时调度：宿主注入优先（GUI 传 QTimer.singleShot）；否则自带无头泵
        self.loop = HeadlessLoop()
        self._after = after or self.loop.after
        # 看门狗：代次计数代替 QTimer.stop（旧代次的到期回调自行作废）
        self._wd_gen = 0

    # ---- 生命周期 ----

    def start(self) -> None:
        self._dedupe_names()   # 数据集名防撞（GUI/CLI 共用的唯一实现）
        if self._run is None:
            # 无注入（直接构造引擎的测试/嵌入式调用方）：仍走唯一落盘路径
            self._run = RunRecorder(path=self._path, kind="scan",
                                    source=self._topic, measurement="scan")
            self._path = self._run.path
        self._h5 = self._run.open()
        a = self._h5.attrs
        a["scan"] = f"{self._param_dev}.{self._param.key}"
        a["param_name"] = self._param_name
        a["result_name"] = self._result_name
        a["result_monitor"] = self._out_monitor
        a["result_topic"] = self._topic
        a["points"] = len(self._axis)
        a["frames_per_point"] = self._frames_pp
        a["settle_ms"] = self._settle_ms
        self._h5.create_dataset(self._param_name, data=self._axis)
        n = len(self._axis)
        self._h5.create_dataset("point_time", data=np.full(n, np.nan))
        self._h5.create_dataset("point_readback", data=np.full(n, np.nan))
        self._hub.subscribe(self._topic, self._on_payload)
        self.logged.emit(f"扫描开始：{self._param_dev}.{self._param.key}，"
                         f"{n} 点 × {self._frames_pp} 帧 → {self._path.name}；"
                         f"输出={self._out_monitor}（{self._topic} 原始数据）")
        self._begin_point()

    def _dedupe_names(self) -> None:
        """数据集名防撞：撞保留名（point_time 等）或轴/结果同名 → 加 _ 后缀。

        必须在打开 HDF5 前完成（撞名会在建数据集时直接报错、扫描失败）；
        GUI 面板与 CLI 都经这里，不再各自防撞。
        """
        renamed = False
        if self._param_name in RESERVED_NAMES:
            self._param_name += "_"
            renamed = True
        if (self._result_name in RESERVED_NAMES
                or self._result_name == self._param_name):
            self._result_name += "_"
            renamed = True
        if renamed:
            self.logged.emit(f"数据集名冲突，已自动调整："
                             f"轴={self._param_name} 结果={self._result_name}")

    def close(self) -> None:
        """安全收尾（无论何种原因结束）：退订、记录结束事实、关文件。"""
        self._disarm_watchdog()
        try:
            self._hub.unsubscribe(self._topic, self._on_payload)
        except Exception:
            logger.debug("扫描退订失败（引擎收尾）", exc_info=True)
        if self._run is not None:
            # 未走 _finish 的直接 close 视为中止（未完成的运行不冒充成功）
            reason = self._finish_reason or "aborted"
            self._run.finish(status_of(reason), reason=reason,
                             completed_points=self._i)
        self._h5 = None

    def abort(self) -> None:
        self._abort = True
        if self._state == "waiting_frame":
            self._finish("aborted")   # 帧等待是被动回调，可立即安全收尾

    # ---- 状态机 ----

    def _begin_point(self) -> None:
        if self._abort:
            self._finish("aborted")
            return
        if self._i >= len(self._axis):
            self._finish("done")
            return
        value = float(self._axis[self._i])
        if self._param.apply == "write":
            self._state = "writing"
            write = self._svc.get(f"write:{self._param_dev}.{self._param.key}")
            if write is None:
                self._finish(f"error: 写入服务不可用 "
                             f"({self._param_dev}.{self._param.key})")
                return
            result = write(value)
            # 同步路径：bool（现状语义）；异步：WriteHandle → 事件循环轮询
            if isinstance(result, WriteHandle):
                self._write_handle = result
                self._after(0, self._poll_write)
                return
            self._write_handle = None
            if not bool(result):
                self._finish(f"error: 写入 {self._param.label}={value:g} 失败"
                             "（设备未连接或拒绝）")
                return
            self._after(self._settle_ms, self._after_settle)
        else:
            self._after_settle()

    def _poll_write(self) -> None:
        """异步写参轮询（P6）：到位 → settle；超时 → 失败收尾。"""
        if self._state != "writing" or self._write_handle is None:
            return
        if self._abort:
            self._write_handle = None
            self._finish("aborted")
            return
        if self._write_handle.done:
            if self._write_handle.timed_out:
                value = self._write_handle.value
                self._write_handle = None
                self._finish(f"error: 写入 {self._param.label}={value:g} 超时")
                return
            self._write_handle = None
            self._after(self._settle_ms, self._after_settle)
            return
        self._after(int(self._write_handle._poll_s * 1000), self._poll_write)

    def _after_settle(self) -> None:
        if self._abort:
            self._finish("aborted")
            return
        read = self._svc.get(f"read:{self._param_dev}.{self._param.key}")
        if read is not None:
            try:
                rb = read()
            except Exception:
                logger.debug("扫描参数回读失败（%s.%s）", self._param_dev,
                             self._param.key, exc_info=True)
                rb = None
            if rb is not None:
                self._h5["point_readback"][self._i] = float(rb)
        self._k = 0
        self._request_frame(self._capture_exposure())

    def _capture_exposure(self) -> float:
        # 曝光参数：轴值即曝光（随单帧显式下发）；其余参数：采集设备当前曝光
        if self._param.is_exposure:
            return float(self._axis[self._i])
        return self._base_exposure

    def _request_frame(self, exposure: float) -> None:
        self._state = "waiting_frame"
        capture = self._svc.get(f"capture:{self._capture_dev}")
        if capture is None:
            self._finish("error: 采集服务不可用")
            return
        # 看门狗：曝光的 4 倍 + 8s 兜底（含读出/传输）
        self._arm_watchdog(max(10_000, exposure * 4000 + 8000))
        capture(exposure)

    def _on_payload(self, payload) -> None:
        if self._state != "waiting_frame" or self._h5 is None:
            return        # 杂散数据（如残留预览），忽略
        arr = np.asarray(payload)
        idx = self._i * self._frames_pp + self._k
        if self._result_name not in self._h5:
            shape = (len(self._axis) * self._frames_pp, *arr.shape)
            self._h5.create_dataset(self._result_name, shape=shape,
                                    dtype=arr.dtype,
                                    compression="gzip", compression_opts=4)
        self._h5[self._result_name][idx] = arr
        self._k += 1
        # 每帧发布原始数据：扫描图 监视器据此增量构建数据立方体
        self._hub.publish(Topics.SCAN_POINT, {
            "i": self._i, "k": self._k - 1, "x": float(self._axis[self._i]),
            "frame": arr, "x_label": self._param_name,
            "x_display": self._param_display or self._param_name,
            "x_unit": self._param.unit, "frames_pp": self._frames_pp,
            "total": len(self._axis), "result_name": self._result_name,
        })
        if self._k < self._frames_pp:
            self._after(0, lambda: self._request_frame(self._capture_exposure()))
            return
        # ---- 本点完成：写点时间戳 ----
        self._disarm_watchdog()
        self._h5["point_time"][self._i] = time.time() - self._t0
        self._h5.flush()
        done = self._i + 1
        self.point_done.emit(self._i, float(self._axis[self._i]))
        self.progress.emit(done, len(self._axis))
        self._i = done
        self._state = "idle"
        self._after(0, self._begin_point)

    def _arm_watchdog(self, ms: int) -> None:
        """布防看门狗：代次 +1，旧代次到期回调自行作废（取代 QTimer.stop）。"""
        self._wd_gen += 1
        gen = self._wd_gen
        self._after(int(ms), lambda: self._on_watchdog(gen))

    def _disarm_watchdog(self) -> None:
        self._wd_gen += 1

    def _on_watchdog(self, gen: int) -> None:
        if gen != self._wd_gen:
            return              # 已停用/已被新点重布（旧定时器作废）
        if self._state == "waiting_frame":
            self._finish("error: 采集超时（看门狗）——检查设备连接/曝光时间")

    def _finish(self, reason: str) -> None:
        if self._state == "finished":
            return
        self._state = "finished"
        self._finish_reason = reason
        self.close()
        self.logged.emit({"done": "扫描完成", "aborted": "已中止（保留已完成数据）"}.get(
            reason, reason))
        self.finished.emit(reason)
