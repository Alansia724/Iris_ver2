"""Foundation 节点 —— device_list 状态机 + 任务执行（指南 Foundation 节）。

foundation_id 同时作为仪器的 device_ip（路由地址）。
任务执行结果以 h5（内存字节）返回；任务完成不自动解锁。
"""

from __future__ import annotations

import io
import logging
import threading
import time

import h5py

log = logging.getLogger("iris_foundation.node")

ACCESSIBLE = "accessible"


def locked_state(sdk_id: str) -> str:
    return f"locked by {sdk_id}"


def state_owner(state: str) -> str | None:
    """从 device_state 提取持有者 sdk_id；accessible 返回 None。"""
    if state.startswith("locked by "):
        return state[len("locked by "):]
    return None


class TaskStateError(RuntimeError):
    """task 调用仪器与 device_state 不一致（指南：不一致时报错）。"""


class FoundationNode:
    """一个 Foundation 实例：连一台或多台仪器。"""

    def __init__(self, foundation_id: str):
        self.foundation_id = foundation_id     # 亦即 device_ip（路由地址）
        self._devices: dict[str, object] = {}  # device_id → 驱动实例
        self.device_state: dict[str, str] = {}  # device_id ↔ device_state
        self._mu = threading.Lock()

    # ---- 装配（驱动来自 ver1 devices/ 或测试假件） ----

    def attach_device(self, device_id: str, driver: object) -> None:
        """挂接仪器驱动实例（driver 提供被 task 调用的方法/属性）。"""
        with self._mu:
            self._devices[device_id] = driver
            self.device_state.setdefault(device_id, ACCESSIBLE)

    def device_list(self) -> dict:
        with self._mu:
            return dict(self.device_state)

    # ---- 占用 / 释放（状态转移只发生在这里） ----

    def occupy(self, device_id: str, sdk_id: str) -> dict:
        with self._mu:
            st = self.device_state.get(device_id)
            if st is None:
                return _err(f"no such device {device_id!r}")
            if st == ACCESSIBLE:                      # accessible → locked
                self.device_state[device_id] = locked_state(sdk_id)
                return _ok(self.device_state[device_id])
            return _err(f"device_state={st!r}，不得占用")

    def release(self, device_id: str, sdk_id: str) -> dict:
        with self._mu:
            st = self.device_state.get(device_id)
            if st is None:
                return _err(f"no such device {device_id!r}")
            if st == locked_state(sdk_id):            # 被自己占用 → 可释放
                self.device_state[device_id] = ACCESSIBLE
                return _ok(ACCESSIBLE)
            return _err(f"device_state={st!r}，非本 sdk 占有，不可释放")

    # ---- 任务执行（指南 Foundation 伪代码） ----

    def run_task(self, sdk_id: str, instructions: list) -> dict:
        """instructions 为拆分后（不编辑）的本节点指令序列，逐条：

        check state 一致（locked by 本 sdk_id）→ work → 记录结果；
        全部完成 → h5 data 返回；不自动解锁。
        """
        with self._mu:                                # ---- 一致性检查 ----
            for ins in instructions:
                dev = ins.get("device_id", "")
                st = self.device_state.get(dev)
                if st is None:
                    return _err(f"no such device {dev!r}")
                if st != locked_state(sdk_id):
                    return _err(f"device {dev!r} device_state={st!r}，"
                                f"与 task 提交方 {sdk_id!r} 不一致")
        results: list[dict] = []                      # ---- work ----
        for ins in instructions:
            dev_id = ins.get("device_id", "")
            fn = ins.get("call", "")
            args = tuple(ins.get("args") or ())
            kwargs = dict(ins.get("kwargs") or {})
            entry = {"device_ip": self.foundation_id, "device_id": dev_id,
                     "call": fn, "args": list(args)}
            try:
                with self._mu:
                    driver = self._devices[dev_id]
                entry["result"] = _plain(getattr(driver, fn)(*args, **kwargs))
                entry["ok"] = True
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
        data = _h5_bytes(self.foundation_id, sdk_id, results)
        return _ok({"data": data, "results": results})   # 不自动解锁


# ------------------------------------------------------------ 小工具

def _ok(value) -> dict:
    return {"ok": True, "value": value}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _plain(v):
    if isinstance(v, (int, float, str, bool, type(None))):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return repr(v)


def _h5_bytes(foundation_id: str, sdk_id: str, results: list) -> bytes:
    """执行结果序列化为 h5 字节（指南：return data，h5 格式）。"""
    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        f.attrs["schema"] = "iris-ver2/task-result"
        f.attrs["foundation_id"] = foundation_id
        f.attrs["sdk_id"] = sdk_id
        f.attrs["created"] = time.time()
        g = f.create_group("instructions")
        for i, r in enumerate(results):
            ds = g.create_group(f"i{i}")
            ds.attrs["device_id"] = r["device_id"]
            ds.attrs["call"] = r["call"]
            ds.attrs["ok"] = r["ok"]
            if r.get("ok"):
                import numpy as np
                v = r["result"]
                try:
                    if isinstance(v, str):
                        ds.create_dataset(
                            "result", data=v,
                            dtype=h5py.string_dtype(encoding="utf-8"))
                    else:
                        ds.create_dataset("result", data=np.asarray(v))
                except TypeError:
                    ds.create_dataset("result", data=str(v))
            else:
                ds.attrs["error"] = r.get("error", "")
    return buf.getvalue()
