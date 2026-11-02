"""驱动注册表（docs/architecture.md §6）：扫描 devices 包的 DRIVER_MANIFEST。

- 发现：pkgutil 遍历 iris_foundation.devices 子模块，取 DRIVER_MANIFEST；
  外部插件 entry points 组 "labcontrol.drivers"（P4 前不实现，留组名）。
- 韧性：单个驱动 import 失败（缺可选 SDK）或合同校验不过 → 记日志跳过，
  不拖垮平台——可选硬件缺席是常态，不是异常。
- 汇总：manifests() 全表 / by_capability() 按能力寻址 / declared_params()
  取设备类 params() 声明（无需实例化，硬件未连也能生成 GUI/CLI 参数目录）。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from iris_foundation.core.contract import (
    CAPABILITY_PROTOCOLS, CONTRACT_VERSION, DeviceInfo, DriverManifest,
    MeasurementManifest, ParamDesc,
)

logger = logging.getLogger("iris_foundation.core.registry")

_DEVICE_PKG = "iris_foundation.devices"
_MEASUREMENT_PKG = "labcontrol.measurements"


def _validate(manifest: DriverManifest, mod) -> str | None:
    """合同校验；返回错误描述（None = 通过）。加载时用（拒载即停）。"""
    problems = diagnose(manifest, mod)
    return problems[0] if problems else None


#: ParamDesc 合法取值（§4.3；conformance 检查与加载校验同一份规则）
_PARAM_PTYPES = ("float", "int", "bool", "enum")
_PARAM_APPLIES = ("per_capture", "write")
_PARAM_ACCESS = ("ro", "rw")
_STREAM_KINDS = ("frame", "scalar", "spectrum", "status")


def diagnose(manifest: DriverManifest, mod) -> list[str]:
    """驱动合同完整诊断（§14 U2）：返回全部问题（空表 = 通过）。

    单一规则源：运行时拒载（_validate）与接入前 conformance 检查
    （scripts/verify_driver_conformance.py）消费同一函数，不会出现
    "检查器过了、注册表仍拒载"的分叉。无硬件可跑。
    """
    problems: list[str] = []
    if manifest.contract_version != CONTRACT_VERSION:
        problems.append(
            f"contract_version={manifest.contract_version}，平台要求 {CONTRACT_VERSION}")
    entry = getattr(mod, manifest.entry, None)
    if entry is None:
        problems.append(f"入口类 {manifest.entry!r} 不存在")
        return problems                      # 后续检查都要用到类
    for cap in manifest.capabilities:
        proto = CAPABILITY_PROTOCOLS.get(cap)
        if proto is None:
            problems.append(
                f"未知能力 {cap!r}（可选：{sorted(CAPABILITY_PROTOCOLS)}）")
        elif not isinstance(entry, proto):
            problems.append(f"声称能力 {cap!r} 但 {manifest.entry} 未实现其接口")
    # params() 自声明 schema（§4.3）
    fn = getattr(entry, "params", None)
    if fn is None:
        if manifest.capabilities:            # 有可扫能力却无参数声明，多为遗漏
            problems.append(f"{manifest.entry} 未声明 params()（能力驱动应自声明参数）")
    else:
        try:
            items = list(fn())
        except Exception as exc:
            problems.append(f"params() 调用失败: {exc!r}")
        else:
            seen: set[str] = set()
            for p in items:
                tag = f"param[{p.key}]"
                if p.key in seen:
                    problems.append(f"{tag}: key 重复")
                seen.add(p.key)
                if p.ptype not in _PARAM_PTYPES:
                    problems.append(f"{tag}: ptype={p.ptype!r}（可选 {_PARAM_PTYPES}）")
                if p.apply not in _PARAM_APPLIES:
                    problems.append(f"{tag}: apply={p.apply!r}（可选 {_PARAM_APPLIES}）")
                if p.access not in _PARAM_ACCESS:
                    problems.append(f"{tag}: access={p.access!r}（可选 {_PARAM_ACCESS}）")
                if p.vmin is not None and p.vmax is not None and p.vmin > p.vmax:
                    problems.append(f"{tag}: vmin({p.vmin}) > vmax({p.vmax})")
    # streams 声明（§4.4）
    names: set[str] = set()
    for s in manifest.streams:
        if s.name in names:
            problems.append(f"stream[{s.name}]: 名称重复")
        names.add(s.name)
        if s.kind not in _STREAM_KINDS:
            problems.append(
                f"stream[{s.name}]: kind={s.kind!r}（可选 {_STREAM_KINDS}）")
    return problems


def _scan() -> dict[str, DriverManifest]:
    table: dict[str, DriverManifest] = {}
    try:
        pkg = importlib.import_module(_DEVICE_PKG)
    except ImportError as exc:
        logger.warning("设备包不可用，注册表为空: %s (%s)", _DEVICE_PKG, exc)
        return table
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        mod_name = f"{_DEVICE_PKG}.{info.name}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:      # 缺可选 SDK/依赖：跳过不拖垮
            logger.warning("驱动模块导入失败，跳过: %s (%s)", mod_name, exc)
            continue
        manifest = getattr(mod, "DRIVER_MANIFEST", None)
        if manifest is None:          # transports 等共享模块
            continue
        problem = _validate(manifest, mod)
        if problem is not None:
            logger.error("驱动合同校验失败，拒绝加载: %s：%s", mod_name, problem)
            continue
        if manifest.dev_id in table:
            logger.error("dev_id 冲突，后者忽略: %s（%s 与 %s）", manifest.dev_id,
                         table[manifest.dev_id].driver, manifest.driver)
            continue
        table[manifest.dev_id] = manifest
    return table





# ------------------------------------------------------------ 设备源分流
#
# DeviceSource 抽象（docs/remote_platform_plan.md §5）：合同层（engine/
# build_scan_services/GUI）只经本模块的模块级函数取设备，函数内部按
# 活动源（本地注册表 / 远程主机）分流——合同层零改动、零网络感知。

class DeviceSource:
    """设备源接口：驱动发现 + 实例化的最小面（与模块级函数一一对应）。"""

    def manifests(self) -> tuple[DriverManifest, ...]:
        raise NotImplementedError

    def manifest_for(self, dev_id: str) -> DriverManifest | None:
        raise NotImplementedError

    def by_capability(self, capability: str) -> tuple[DriverManifest, ...]:
        return tuple(m for m in self.manifests() if capability in m.capabilities)

    def declared_params(self, dev_id: str) -> dict[str, ParamDesc]:
        raise NotImplementedError

    def create(self, dev_id: str, **kwargs):
        raise NotImplementedError

    def info_for(self, dev_id: str, serial_number: str | None = None) -> DeviceInfo:
        m = self.manifest_for(dev_id)
        if m is None:
            raise KeyError(f"未注册的设备: {dev_id!r}")
        return DeviceInfo(dev_id=m.dev_id, driver=m.driver, model=m.model,
                          serial_number=serial_number,
                          contract_version=m.contract_version,
                          capabilities=m.capabilities)


class LocalSource(DeviceSource):
    """本地驱动源（现状）：本进程扫描 iris_foundation.devices。"""

    def manifests(self) -> tuple[DriverManifest, ...]:
        return tuple(_table()[k] for k in sorted(_table()))

    def manifest_for(self, dev_id: str) -> DriverManifest | None:
        return _table().get(dev_id)

    def declared_params(self, dev_id: str) -> dict[str, ParamDesc]:
        cls = self.device_class(dev_id)
        fn = getattr(cls, "params", None)
        if fn is None:
            return {}
        return {p.key: p for p in fn()}

    def device_class(self, dev_id: str):
        """驱动的设备类（params() 声明挂在其上）。"""
        manifest = self.manifest_for(dev_id)
        if manifest is None:
            raise KeyError(f"未注册的设备: {dev_id!r}（已注册: {sorted(_table())}）")
        mod = importlib.import_module(f"{_DEVICE_PKG}.{manifest.driver}")
        return getattr(mod, manifest.entry)

    def create(self, dev_id: str, **kwargs):
        """实例化设备（transport 配置 setup/2 接入；P3 起 GUI/CLI 改走这里）。"""
        return self.device_class(dev_id)(**kwargs)


class RemoteSource(DeviceSource):
    """远程主机设备源（P-R1 回环 / P-R2 SSH 隧道共用，见 labcontrol.remote）。

    发现与参数声明来自服务端 list（manifest/ParamDesc 经协议序列化保真
    过网）；create() 返回 RemoteDevice stub——合同层视角与本地实例无差别。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7842, **host_kw):
        from labcontrol.remote.client import RemoteHost
        self._host_kw = dict(host_kw)
        self.host = RemoteHost(host, port, **self._host_kw)
        self._listing: dict | None = None

    def _listing_fresh(self) -> dict:
        self.host.connect()
        self._listing = self.host.list()
        return self._listing

    def manifests(self) -> tuple[DriverManifest, ...]:
        listing = self._listing_fresh()
        return tuple(listing["manifests"][k] for k in sorted(listing["manifests"]))

    def manifest_for(self, dev_id: str) -> DriverManifest | None:
        return self._listing_fresh()["manifests"].get(dev_id)

    def declared_params(self, dev_id: str) -> dict[str, ParamDesc]:
        params = self._listing_fresh()["params"].get(dev_id, ())
        return {p.key: p for p in params}

    def create(self, dev_id: str, **kwargs):
        """返回远程设备 stub（kwargs 忽略——实例化发生在服务端）。"""
        # P-R2 修复：先确保连接+反射表新鲜（此前依赖调用方先 manifests()，
        # run_experiment --host 直连 create 即 _sock None 崩溃，潜伏缺陷暴露）
        self._listing_fresh()
        return self.host.device(dev_id)

    def close(self) -> None:
        self.host.close()


class MultiRemoteSource(DeviceSource):
    """多远程主机并存聚合源（P-R3）。

    dev_id 命名空间："<ns>/<dev>"（ns 缺省 = 服务端主机名首段，GUI 模型树
    同款前缀）。每主机一条独立 RemoteHost 连接——一台离线只影响自己的
    调用，绝不阻塞其余主机（无全局锁/无全局阻塞；离线主机的 list 会抛
    ConnectionError，调用方按主机粒度兜底）。
    """

    def __init__(self) -> None:
        self._hosts: dict[str, "object"] = {}   # ns -> RemoteSource

    # ---- 主机管理 ----

    def attach(self, ns: str, source) -> None:
        """挂一个已连接的 RemoteSource（或裸 RemoteHost，自动适配）。"""
        ns = _ns_clean(ns)
        if ns in self._hosts and self._hosts[ns] is not source:
            raise KeyError(f"命名空间冲突: {ns!r} 已被占用")
        if not hasattr(source, "manifest_for"):
            source = _RemoteHostSource(source)
        self._hosts[ns] = source

    def detach(self, ns: str, close: bool = True) -> None:
        src = self._hosts.pop(_ns_clean(ns), None)
        if src is not None and close:
            try:
                src.close()
            except Exception:
                pass

    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted(self._hosts))

    def source_for(self, ns: str):
        return self._hosts[_ns_clean(ns)]

    @staticmethod
    def split(dev_id: str) -> tuple[str, str]:
        """'pc-1/Newton' → ('pc-1', 'Newton')；无前缀报错（多主机态必须带 ns）。"""
        ns, _, dev = dev_id.partition("/")
        if not dev:
            raise KeyError(
                f"多主机模式下设备须带主机命名空间前缀 '<host>/{dev_id!r}'")
        return ns, dev

    # ---- DeviceSource 接口（按 ns 路由；跨主机组合由调用方拼 services）----

    def manifests(self) -> tuple[DriverManifest, ...]:
        import dataclasses
        out = []
        for ns in self.namespaces():
            try:
                listing = self._hosts[ns].host.list()
            except Exception:
                continue          # 一台离线不阻塞其余主机的枚举（连接级隔离）
            for dev_id, m in listing["manifests"].items():
                out.append(dataclasses.replace(m, dev_id=f"{ns}/{dev_id}"))
        return tuple(sorted(out, key=lambda m: m.dev_id))

    def manifest_for(self, dev_id: str) -> DriverManifest | None:
        import dataclasses
        ns, dev = self.split(dev_id)
        src = self._hosts.get(_ns_clean(ns))
        if src is None:
            return None
        m = src.manifest_for(dev)
        return None if m is None else dataclasses.replace(m, dev_id=dev_id)

    def declared_params(self, dev_id: str) -> dict[str, ParamDesc]:
        ns, dev = self.split(dev_id)
        return self.source_for(ns).declared_params(dev)

    def create(self, dev_id: str, **kwargs):
        ns, dev = self.split(dev_id)
        return self.source_for(ns).create(dev, **kwargs)


class _RemoteHostSource(DeviceSource):
    """裸 RemoteHost 的最小 DeviceSource 适配（GUI 池直接持有时挂聚合源）。"""

    def __init__(self, host) -> None:
        self.host = host

    def _listing(self) -> dict:
        self.host.connect()
        return self.host.list()

    def manifests(self) -> tuple[DriverManifest, ...]:
        return tuple(self._listing()["manifests"].values())

    def manifest_for(self, dev_id: str) -> DriverManifest | None:
        return self._listing()["manifests"].get(dev_id)

    def declared_params(self, dev_id: str) -> dict[str, ParamDesc]:
        params = self._listing()["params"].get(dev_id, ())
        return {p.key: p for p in params}

    def create(self, dev_id: str, **kwargs):
        self._listing()
        return self.host.device(dev_id)

    def close(self) -> None:
        self.host.close()


def _ns_clean(ns: str) -> str:
    """命名空间规整：小写、去尾部 .local./端口噪声（与 mDNS hostname 对齐）。"""
    return str(ns).strip().rstrip(".").split(".")[0].lower()


#: 活动设备源（模块级函数全部经此分流；默认 = 现状本地源）
_ACTIVE_SOURCE: DeviceSource = LocalSource()


def active_source() -> DeviceSource:
    return _ACTIVE_SOURCE


def set_source(source: DeviceSource) -> None:
    """切换活动设备源（GUI/CLI 连接远程主机时调用；contract 层零感知）。"""
    global _ACTIVE_SOURCE
    _ACTIVE_SOURCE = source


def use_remote(host: str = "127.0.0.1", port: int = 7842, **host_kw) -> RemoteSource:
    """切到远程主机并返回源（配 set_source(LocalSource()) 切回本地）。"""
    src = RemoteSource(host, port, **host_kw)
    set_source(src)
    return src


def use_local() -> None:
    set_source(LocalSource())


def use_multi_remote() -> MultiRemoteSource:
    """切到多主机聚合源并返回（P-R3；配 use_local() 切回本地）。"""
    src = MultiRemoteSource()
    set_source(src)
    return src


_TABLE: dict[str, DriverManifest] | None = None


def _table() -> dict[str, DriverManifest]:
    global _TABLE
    if _TABLE is None:
        _TABLE = _scan()
    return _TABLE


def manifests() -> tuple[DriverManifest, ...]:
    """全部可用设备（按 dev_id 排序，输出稳定）——经活动设备源分流。"""
    return _ACTIVE_SOURCE.manifests()


def manifest_for(dev_id: str) -> DriverManifest | None:
    return _ACTIVE_SOURCE.manifest_for(dev_id)


def by_capability(capability: str) -> tuple[DriverManifest, ...]:
    """声明了某能力的设备（测量按能力寻址设备，§5）。"""
    return _ACTIVE_SOURCE.by_capability(capability)


def device_class(dev_id: str):
    """驱动的设备类（仅本地源有意义；远程源 stub 由 create 生成）。"""
    src = _ACTIVE_SOURCE
    if isinstance(src, LocalSource):
        return src.device_class(dev_id)
    raise KeyError(f"远程设备源无 device_class: {dev_id!r}")


def declared_params(dev_id: str) -> dict[str, ParamDesc]:
    """设备参数声明；未声明返回空表——经活动设备源分流。"""
    return _ACTIVE_SOURCE.declared_params(dev_id)


def create(dev_id: str, **kwargs):
    """实例化/代理设备（本地=驱动实例；远程=透明 stub）——经活动源分流。"""
    return _ACTIVE_SOURCE.create(dev_id, **kwargs)


def info_for(dev_id: str, serial_number: str | None = None) -> DeviceInfo:
    """由 manifest 拼 DeviceInfo（连接后可带 serial_number 重建）。"""
    return _ACTIVE_SOURCE.info_for(dev_id, serial_number)


def device_of_topic(topic: str | None) -> str | None:
    """数据流主题 → 产生它的设备（§4.4 命名规则派生，不维护第二张表）。

    新驱动声明了 streams 就自动认得（“加设备零改动”）；
    注册表不可用（远程源离线等）= None，不抛。
    """
    if not topic:
        return None
    try:
        for m in manifests():
            for s in m.streams:
                if s.topic(m.dev_id) == topic:
                    return m.dev_id
    except Exception:
        logger.debug("主题归属查询失败（注册表不可用）：%s", topic, exc_info=True)
    return None


# ---------------------------------------------------------------- 测量注册表（§14.2）

_MEASUREMENTS: dict[str, MeasurementManifest] | None = None

#: 测量 manifest 合法取值（诊断用，与驱动同一纪律）
_MEASUREMENT_CATEGORIES = ("acquisition", "orchestration")


def _validate_measurement(m: MeasurementManifest) -> list[str]:
    problems: list[str] = []
    if m.contract_version != CONTRACT_VERSION:
        problems.append(
            f"contract_version={m.contract_version}，平台要求 {CONTRACT_VERSION}")
    if m.category not in _MEASUREMENT_CATEGORIES:
        problems.append(
            f"category={m.category!r}（可选 {_MEASUREMENT_CATEGORIES}）")
    for cap in m.requires:
        if cap not in CAPABILITY_PROTOCOLS:
            problems.append(
                f"未知能力需求 {cap!r}（可选：{sorted(CAPABILITY_PROTOCOLS)}）")
    return problems


def _scan_measurements() -> dict[str, MeasurementManifest]:
    table: dict[str, MeasurementManifest] = {}
    try:
        pkg = importlib.import_module(_MEASUREMENT_PKG)
    except ImportError as exc:
        logger.warning("测量包不可用，测量注册表为空: %s (%s)",
                       _MEASUREMENT_PKG, exc)
        return table
    for info in sorted(pkgutil.iter_modules(pkg.__path__),
                       key=lambda m: m.name):
        mod_name = f"{_MEASUREMENT_PKG}.{info.name}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:      # 声明模块损坏：跳过不拖垮平台
            logger.warning("测量声明模块导入失败，跳过: %s (%s)", mod_name, exc)
            continue
        manifest = getattr(mod, "MEASUREMENT_MANIFEST", None)
        if manifest is None:
            continue
        problems = _validate_measurement(manifest)
        if problems:
            logger.error("测量合同校验失败，拒绝加载: %s：%s",
                         mod_name, "；".join(problems))
            continue
        if manifest.mid in table:
            logger.error("mid 冲突，后者忽略: %s", manifest.mid)
            continue
        table[manifest.mid] = manifest
    return table


def _measurement_table() -> dict[str, MeasurementManifest]:
    global _MEASUREMENTS
    if _MEASUREMENTS is None:
        _MEASUREMENTS = _scan_measurements()
    return _MEASUREMENTS


def measurement_manifests() -> tuple[MeasurementManifest, ...]:
    """全部已注册测量单元（按 mid 排序，输出稳定）。"""
    t = _measurement_table()
    return tuple(t[k] for k in sorted(t))


def measurement_manifest(mid: str) -> MeasurementManifest | None:
    return _measurement_table().get(mid)
