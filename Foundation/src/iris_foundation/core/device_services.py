"""通用设备写/读服务（P6，docs/architecture.md §4.3 写侧声明）。

从 PARAM_CATALOG（驱动 params() 声明）通用装配 write:<dev>.<key> /
read:<dev>.<key> 服务——GUI 与 CLI 共用，消灭逐设备手写（§12 #1/#3）。

约定：
  * writer(device, desc, value) → bool | WriteHandle
      同步设备返回 bool（现状语义不变）；异步设备（如 move_to(wait=False)、
      或 set_<key> 返回 Future 状对象）返回 WriteHandle，由引擎轮询。
  * reader(device, desc) → float | None（无 getter 或读取失败 = None）
"""

from __future__ import annotations

import logging
import time

from iris_foundation.core.contract import ParamDesc

logger = logging.getLogger("iris_foundation.core.device_services")


class WriteHandle:
    """异步写句柄：writer 发起后由调用方轮询 done（引擎在事件循环里做）。"""

    def __init__(self, value: float, check, timeout_s: float = 120.0,
                 poll_s: float = 0.05):
        self.value = value
        self._check = check            # () -> bool  完成/到位判定
        self._deadline = time.monotonic() + timeout_s
        self._poll_s = poll_s

    @property
    def done(self) -> bool:
        """完成（到位）= True；超时视为失败（done 恒 False，由引擎收错）。"""
        if self._check():
            return True
        return time.monotonic() > self._deadline

    @property
    def timed_out(self) -> bool:
        return time.monotonic() > self._deadline


def _to_bool_or_handle(result, value: float):
    """set_<key> 返回值归一：bool 原样；None→True（发出即收）；其余→True。

    异步语义留给专门的异步 setter（返回 WriteHandle 子类/带 .done 对象），
    现有驱动都是同步的，这里只做宽容归一。
    """
    if isinstance(result, WriteHandle):
        return result
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    return True


def writer(device, desc: ParamDesc, value: float):
    """按声明写参数：setter 解析（显式 > 能力/命名推导）→ 调用 → 回读校验。

    返回 bool（同步结果）或 WriteHandle（异步 setter）。读取失败/无 getter
    不影响写入结果（回读只做容差校验，readback_tol=None 跳过）。
    """
    setter = desc.resolve_setter(device)
    if setter is None:
        logger.warning("参数 %s.%s 无可用写侧（只读？），写入被忽略",
                       getattr(device, "dev_id", "?"), desc.key)
        return False
    method = getattr(device, setter)
    try:
        result = method(value)
    except Exception as exc:
        logger.warning("写参数失败 %s=%g: %s", desc.key, value, exc)
        return False
    ok = _to_bool_or_handle(result, value)
    if ok is False:
        return False
    if desc.readback_tol is not None and not isinstance(ok, WriteHandle):
        getter = desc.resolve_getter(device)
        if getter is not None:
            try:
                rb = float(getattr(device, getter))
                if abs(rb - float(value)) > desc.readback_tol:
                    logger.warning("回读偏差超容差: %s 请求 %g 回读 %g",
                                   desc.key, value, rb)
                    return False
            except Exception:
                logger.debug("回读失败（不阻断写入）: %s", desc.key, exc_info=True)
    return ok


def reader(device, desc: ParamDesc) -> float | None:
    """按声明读参数；失败/无 getter = None。"""
    getter = desc.resolve_getter(device)
    if getter is None:
        return None
    try:
        return float(getattr(device, getter))
    except Exception:
        logger.debug("读参数失败: %s", desc.key, exc_info=True)
        return None


def async_handle(device, desc: ParamDesc, value: float,
                 getter_poll: str | None = None,
                 timeout_s: float = 120.0) -> WriteHandle:
    """为"发出即忘"的运动/慢命令构造轮询句柄（GUI DeviceWorker 用）。

    到位判定 = getter_poll 指向的布尔属性（如 is_moving）翻 False；
    或目标值进入容差。timeout 保护。
    """
    if getter_poll is not None:
        def _check() -> bool:
            return not bool(getattr(device, getter_poll))
    elif desc.readback_tol is not None:
        def _check() -> bool:
            rb = reader(device, desc)
            return rb is not None and abs(rb - value) <= desc.readback_tol
    else:
        def _check() -> bool:
            return True
    return WriteHandle(value, _check, timeout_s=timeout_s)


def build_scan_services(devices: dict, on_error=None,
                       exempt: set = frozenset()) -> dict:
    """从注册表+参数目录通用装配 write/read 服务键（GUI/CLI 共用）。

    devices: {dev_id: 设备实例}（None 跳过）；capture:/exposure: 仍由
    调用方装配（采集管线 GUI 有 CameraWorker、CLI 直采，各不相同）。
    exempt: 豁免集 {(dev_id, key)}——调用方有特殊管线的参数（如 GUI 的
    dvp.gain 走帧间隙生效预览管线，直写会与采集恢复逻辑竞态）。
    """
    from iris_foundation.core.params import PARAM_CATALOG
    from iris_foundation.core.registry import declared_params
    services: dict = {}
    for dev_id, dev in devices.items():
        if dev is None:
            continue
        # 描述从注册表取（驱动类 params()，单一事实来源）；setter 解析看实例
        # （假件/代理无需自带 params()，只要有同名方法/属性即可）
        descs = declared_params(dev_id)
        for key, scan in PARAM_CATALOG.get(dev_id, {}).items():
            if scan.apply != "write":
                continue               # per_capture 参数随采集下发，无独立写服务
            desc = descs.get(key)
            if desc is None:
                continue
            if (dev_id, key) in exempt:
                continue               # 调用方豁免（特殊管线，理由见调用处）
            services[f"write:{dev_id}.{key}"] = (
                lambda value, d=dev, p=desc: writer(d, p, value))
            services[f"read:{dev_id}.{key}"] = (
                lambda d=dev, p=desc: reader(d, p))
    return services


#: GUI 写服务豁免：dvp.gain 走 CameraWorker 预览联动管线（帧间隙生效），
#: 直写设备会与"采集后恢复曝光/增益"逻辑竞态——仅 GUI 路径豁免，CLI 直写。
GUI_WRITE_EXEMPT = {("dvp", "gain")}
