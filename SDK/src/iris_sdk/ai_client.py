"""SDK —— AI 协作接口（IRIS ver2 实现指南 SDK 节）。

先把 AI 接口打通（GUI 面板之后再说）。每个 SDK 对应一个账号（sdk_id）。

两种输出 to Service：
  ① 占用仪器请求（accessible → 请求占用；locked by others → 不得占用；
     被自己占用 → 可请求释放）
  ② task.json（仅当所需仪器都被自己占用时允许提交）

指南中的 task 书写形：
    To device_ip X, device_id A,
        调试函数;
    To device_ip Y, device_id B,
        调试函数;
对应 task.json 的 instructions 列表（本接口用 make_task 构造）：
    [{"device_ip": "X", "device_id": "A", "call": "调试函数", "args": [...]}, ...]
"""

from __future__ import annotations

import json
from pathlib import Path

import msgpack
import zmq

DEFAULT_SERVICE = "tcp://127.0.0.1:7850"


class SdkError(RuntimeError):
    pass


class AiClient:
    """AI/脚本侧的 SDK 客户端：账号 → 占用 → 提交 task.json → 收 data(h5)。"""

    def __init__(self, sdk_id: str, secret: str,
                 service: str = DEFAULT_SERVICE, timeout_ms: int = 60_000):
        self.sdk_id = sdk_id
        self.service = service
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self.token: str | None = None
        self._register_and_login(secret)

    # ---- RPC 基元 ----

    def _call(self, payload: dict) -> dict:
        s = self._ctx.socket(zmq.REQ)
        try:
            s.setsockopt(zmq.LINGER, 0)
            s.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
            s.connect(self.service)
            s.send(msgpack.packb(payload, use_bin_type=True))
            return msgpack.unpackb(s.recv(), raw=False)
        finally:
            s.close()

    def _auth_call(self, payload: dict) -> dict:
        if self.token is None:
            raise SdkError("未登录")
        r = self._call({**payload, "sdk_id": self.sdk_id, "token": self.token})
        if not r.get("ok"):
            raise SdkError(f"{r.get('err_type')}: {r.get('error')}")
        return r

    # ---- 账号 ----

    def _register_and_login(self, secret: str) -> None:
        r = self._call({"op": "register", "sdk_id": self.sdk_id,
                        "secret": secret})
        if not r.get("ok") and "已存在" not in str(r.get("error", "")):
            raise SdkError(f"register: {r.get('error')}")
        r = self._call({"op": "login", "sdk_id": self.sdk_id,
                        "secret": secret})
        if not r.get("ok"):
            raise SdkError(f"login: {r.get('error')}")
        self.token = r["token"]

    # ---- ① 占用仪器请求 ----

    def occupy(self, device_ip: str, device_id: str) -> str:
        """device_state=accessible 时请求占用 → locked by sdk_id。"""
        r = self._auth_call({"op": "occupy", "device_ip": device_ip,
                             "device_id": device_id})
        return r["device_state"]

    def release(self, device_ip: str, device_id: str) -> str:
        """被自己占用时请求释放 → accessible（唯一解锁途径）。"""
        r = self._auth_call({"op": "release", "device_ip": device_ip,
                             "device_id": device_id})
        return r["device_state"]

    def device_states(self) -> dict:
        """汇报 device_state_list：{foundation_id: {device_id: state}}。"""
        return self._auth_call({"op": "device_states"})["device_state_list"]

    # ---- ② task.json ----

    def submit_task_file(self, path: str | Path) -> dict:
        return self.submit_task(json.loads(Path(path).read_text("utf-8")))

    def submit_task(self, task: dict) -> dict:
        """仅当所需仪器都被自己占用时允许提交；返回 task_id。"""
        r = self._auth_call({"op": "submit_task", "task": task})
        return r

    def get_data(self, task_id: str, save_dir: str | Path | None = None
                 ) -> dict:
        """回报 data（h5 字节）；save_dir 给定则按 foundation 落盘。"""
        r = self._auth_call({"op": "get_data", "task_id": task_id})
        if save_dir is not None:
            p = Path(save_dir)
            p.mkdir(parents=True, exist_ok=True)
            for fid, blob in r.get("data", {}).items():
                (p / f"{task_id}.{fid}.h5").write_bytes(blob)
        return r


# ------------------------------------------------------------ task 构造

def make_task(instructions: list, task_id: str = "") -> dict:
    """构造 task.json 内容。instructions 每条：

    {"device_ip": <foundation_id>, "device_id": <id>,
     "call": <调试函数名>, "args": [...], "kwargs": {...}}
    """
    return {"task_id": task_id or None, "instructions": instructions}
