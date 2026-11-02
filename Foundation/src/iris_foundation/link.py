"""Foundation ↔ Service 连接 —— 注册 / 长轮询指令 / 回传任务结果。

Foundation 主动拨号 Service（Service 是核）：注册 device_list 后轮询
待处理指令（占用/释放/任务），任务完成后回传 h5 data。
"""

from __future__ import annotations

import logging
import threading
import time

import msgpack
import zmq

from iris_foundation.node import FoundationNode

log = logging.getLogger("iris_foundation.link")

DEFAULT_SERVICE_FACE = "tcp://127.0.0.1:7851"   # Service 的 Foundation 面


class FoundationLink:
    def __init__(self, node: FoundationNode,
                 service_face: str = DEFAULT_SERVICE_FACE,
                 poll_interval_s: float = 0.4):
        self.node = node
        self.service_face = service_face
        self.poll_interval_s = poll_interval_s
        self._ctx = zmq.Context.instance()
        self._running = False

    # ---- RPC 基元 ----

    def _call(self, payload: dict) -> dict:
        s = self._ctx.socket(zmq.REQ)
        try:
            s.setsockopt(zmq.LINGER, 0)
            s.setsockopt(zmq.RCVTIMEO, 10_000)
            s.connect(self.service_face)
            s.send(msgpack.packb(payload, use_bin_type=True))
            return msgpack.unpackb(s.recv(), raw=False)
        finally:
            s.close()

    # ---- 生命周期 ----

    def start(self) -> None:
        r = self._call({"op": "register",
                        "foundation_id": self.node.foundation_id,
                        "device_list": self.node.device_list()})
        if not r.get("ok"):
            raise ConnectionError(f"foundation 注册失败: {r}")
        log.info("foundation %s 注册成功 devices=%s",
                 self.node.foundation_id, self.node.device_list())
        self._running = True
        threading.Thread(target=self._loop, name="fnd-link",
                         daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                r = self._call({"op": "poll",
                                "foundation_id": self.node.foundation_id})
            except zmq.ZMQError:
                time.sleep(1.0)
                continue
            cmd = r.get("cmd")
            if cmd:
                self._dispatch(cmd)
            else:
                time.sleep(self.poll_interval_s)

    # ---- 指令分发：占用 / 释放 / 任务 ----

    def _dispatch(self, cmd: dict) -> None:
        op = cmd.get("op", "")
        reply: dict = {"ok": False, "error": f"unknown op {op!r}"}
        if op == "occupy":
            reply = self.node.occupy(cmd["device_id"], cmd["sdk_id"])
        elif op == "release":
            reply = self.node.release(cmd["device_id"], cmd["sdk_id"])
        elif op == "task":
            reply = self.node.run_task(cmd["sdk_id"], cmd["instructions"])
            # task 完成不自动解锁（指南）；data 随结果回传
        self._call({"op": "reply", "foundation_id": self.node.foundation_id,
                    "req_id": cmd.get("req_id", ""), "reply": reply,
                    "device_list": self.node.device_list()})
