"""Rig：运行期设备绑定（能力 → 设备）——docs/architecture.md §16。

平台的最底层事实有两个：**注册表说"有哪些设备"（声明），Rig 说"这台机器上
现在谁是谁"（绑定）**。消费方（测量/引擎/面板）只按能力提问，永不点名设备 id：

    rig.device("detector")      → 采集设备实例（换 CCD/CMOS 是改绑定，不是改代码）
    rig.topic("detector")       → 该设备的数据流主题（从 manifest.streams 派生）
    rig.device_of_topic(t)      → 这条流是谁发的（引擎/校验用）
    rig.axis_devices()          → 可扫描设备（有 apply=write/per_capture 参数的）

绑定解析顺序（§16.2）：

    1. 显式绑定（workspace ``bindings``：{"detector": "dvp"}）
    2. 在场（已实例化）且声明该能力的设备，按 dev_id 序
    3. 注册表声明该能力的设备，按 dev_id 序（设备不在场也认——报"未连接"
       比报"没有这种设备"准确）

三条规则都解析不出来 = 该能力缺失，消费方明确拒绝，不猜、不回退到硬编码。

Rig 同时是**设备会话的唯一实现**：连接/断开/加锁/事件都在这里做一次，
GUI（后台线程里调）与 CLI（同步调）走同一条路——设备生命周期知识不再分两份
（§15.6 运行时归并）。零 Qt、零设备依赖：只读注册表声明 + 设备实例表。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class Rig:
    """能力 → 设备的绑定（§16）。``devices``/``connected``/``bindings`` 都是
    调用方持有的**活字典引用**——设备连上、断开、换绑对 Rig 立即可见，
    不需要重建对象（GUI 的 BenchServices 与 CLI 各持一份）。
    """

    def __init__(self, devices: dict | None = None,
                 connected: dict | None = None,
                 bindings: dict | None = None,
                 hub=None):
        self._devices: dict = devices if devices is not None else {}
        self._connected: dict = connected if connected is not None else {}
        self._bindings: dict = bindings if bindings is not None else {}
        # 设备事件（§4.5）发到这个 hub；None = 不发布（只落日志/直接构造 Rig）
        self._hub = hub

    # ---------------------------------------------------------------- 会话
    # 设备的连接/断开/加锁只在这里实现一次：GUI（后台线程里调）与 CLI
    # （同步调）走同一条路——设备生命周期知识不再分两份（§15.6 运行时归并）。

    def connect(self, dev_id: str, purpose: str = "", timeout_s: float = 0.0,
                lock: bool = True):
        """连接设备：取锁 → 实例化（注册表缺省）→ connect → 发事件。

        失败自动还锁（没连上就不占着）。抛 ``device_lock.DeviceBusy``
        （被别的进程占着）与设备自身异常，由调用方决定怎么报给用户。
        ``lock=False`` 供远程接入用：硬件在对面，锁由对端持有（§17）。
        """
        from iris_foundation.core import device_lock, registry
        if lock:
            device_lock.acquire(dev_id, purpose=purpose, timeout_s=timeout_s)
        try:
            dev = self._devices.get(dev_id)
            if dev is None:
                dev = registry.create(dev_id)
                self._devices[dev_id] = dev
            dev.connect()
        except BaseException as exc:
            if lock:
                device_lock.release(dev_id)
            self._emit(dev_id, "error", "disconnected", f"连接失败：{exc}")
            raise
        self._connected[dev_id] = True
        self._emit(dev_id, "state", "ready", "已连接")
        return dev

    def disconnect(self, dev_id: str) -> bool:
        """断开设备并归还锁；断开失败**保留锁**（设备可能还开着，占着比
        放开安全，§17）并把异常上抛——调用方按失败处理。"""
        from iris_foundation.core import device_lock
        dev = self._devices.get(dev_id)
        if dev is not None:
            try:
                dev.disconnect()
            except BaseException:
                self._emit(dev_id, "error", "error", "断开出错（保留占用）")
                raise
        self._connected[dev_id] = False
        device_lock.release(dev_id)
        self._emit(dev_id, "state", "disconnected", "已断开")
        return True

    def _emit(self, dev_id: str, event: str, state: str,
              message: str = "") -> None:
        """设备事件：落日志 + 发 ``<dev>.event``（§4.5 的唯一生产者）。

        消费方（日志/界面状态/收尾判定）看同一条流，不再各自推断"设备怎么了"。
        """
        logger.info("设备事件 %s: %s/%s %s", dev_id, event, state, message)
        if self._hub is None:
            return
        from iris_foundation.core.contract import EventDesc, event_topic
        self._hub.publish(event_topic(dev_id), EventDesc(
            device=dev_id, event=event, state=state, message=message,
            t_utc=time.strftime("%Y-%m-%dT%H:%M:%S")).as_payload())

    # ---------------------------------------------------------------- 绑定

    @property
    def devices(self) -> dict:
        return self._devices

    @property
    def bindings(self) -> dict:
        return {k: v for k, v in self._bindings.items() if v}

    def set_bindings(self, bindings: dict | None) -> None:
        """整表替换显式绑定（workspace 载入/保存时调用）。"""
        self._bindings.clear()
        for cap, dev in (bindings or {}).items():
            if dev:
                self._bindings[str(cap)] = str(dev)

    def bind(self, capability: str, dev_id: str | None) -> None:
        """显式绑定一个能力；dev_id 为空 = 解除（回落到自动推导）。"""
        if dev_id:
            self._bindings[str(capability)] = str(dev_id)
        else:
            self._bindings.pop(str(capability), None)

    # ---------------------------------------------------------------- 解析

    def device_id(self, capability: str) -> str | None:
        """能力 → 设备 id（显式绑定 > 在场 > 声明；都没有 = None）。"""
        explicit = self._bindings.get(capability)
        if explicit:
            return explicit
        for dev_id in self._declared(capability):
            if self._devices.get(dev_id) is not None:
                return dev_id
        declared = self._declared(capability)
        return declared[0] if declared else None

    def device(self, capability: str):
        """能力 → 设备实例（未实例化/未连接 = None）。"""
        dev_id = self.device_id(capability)
        return self._devices.get(dev_id) if dev_id else None

    def connected(self, capability: str) -> bool:
        """该能力对应的设备是否已连接（无 connected 表 = 有实例即视为已连）。"""
        dev_id = self.device_id(capability)
        if not dev_id:
            return False
        if dev_id in self._connected:
            return bool(self._connected[dev_id])
        return self._devices.get(dev_id) is not None

    def _declared(self, capability: str) -> tuple[str, ...]:
        from iris_foundation.core import registry
        try:
            return tuple(sorted(m.dev_id for m in registry.by_capability(capability)))
        except Exception:
            logger.debug("能力查询失败（注册表不可用？）：%s", capability,
                         exc_info=True)
            return ()

    # ---------------------------------------------------------------- 数据流

    def topic(self, capability: str, kind: str = "frame") -> str | None:
        """能力 → 该设备某类流的主题（manifest.streams 派生，§4.4）。

        未绑定、或该设备未声明这类流 = None（不猜）。
        """
        dev_id = self.device_id(capability)
        if not dev_id:
            return None
        from iris_foundation.core import registry
        from iris_foundation.core.contract import stream_topic
        try:
            m = registry.manifest_for(dev_id)
        except Exception:
            m = None
        if m is None:
            return None
        for s in m.streams:
            if s.kind == kind:
                return stream_topic(dev_id, s.name)
        return None

    def device_of_topic(self, topic: str | None) -> str | None:
        """数据流主题 → 产生它的设备（引擎/校验用，§16.3）。"""
        from iris_foundation.core import registry
        return registry.device_of_topic(topic)

    def params(self, capability: str) -> dict:
        """能力 → 该设备的参数声明（驱动 params()，§4.3）。"""
        dev_id = self.device_id(capability)
        if not dev_id:
            return {}
        from iris_foundation.core import registry
        try:
            return dict(registry.declared_params(dev_id))
        except Exception:
            return {}

    def axis_devices(self) -> tuple[str, ...]:
        """可作扫描轴的设备：声明了 apply=write / per_capture 参数的设备。

        在场（已实例化）的排前面，各自按 dev_id 序——加设备即出现在扫描
        面板，不改 GUI 一行代码（§16.3）。远程池设备（ns/dev）不在注册表
        本地视图内，由调用方另行追加。
        """
        from iris_foundation.core import registry
        from iris_foundation.core.params import PARAM_CATALOG
        try:
            ids = [m.dev_id for m in registry.manifests()]
        except Exception:
            ids = []
        scannable = [d for d in ids if PARAM_CATALOG.get(d)]
        present = [d for d in scannable if self._devices.get(d) is not None]
        absent = [d for d in scannable if self._devices.get(d) is None]
        return tuple(sorted(present)) + tuple(sorted(absent))

    # ---------------------------------------------------------------- 需求检查

    def missing(self, capabilities) -> list[str]:
        """哪些能力**根本没人声明**（"没有这种设备"，与"没连"区分开）。"""
        return [c for c in (capabilities or ()) if not self._declared(c)]

    def check(self, capabilities) -> tuple[bool, str]:
        """能力需求检查：返回 (ok, 人话原因)。"""
        miss = self.missing(capabilities)
        if miss:
            return False, f"缺少能力：{'/'.join(miss)}（没有设备声明它）"
        ids = []
        for cap in (capabilities or ()):
            dev_id = self.device_id(cap)
            if not self.connected(cap):
                return False, f"{cap} 设备未连接（{dev_id}）"
            ids.append(dev_id)
        return True, ""
