"""Service 守护进程 —— 两个 RPC 面（msgpack over zmq）。

  7850 面向 SDK   ：register / login / occupy / release / submit_task
                    / get_data / device_states
  7851 面向 FND   ：register / poll / reply

用法：python -m iris_service [--sdk-port 7850] [--fnd-port 7851]
"""

from __future__ import annotations

import argparse
import logging
import threading

import msgpack
import zmq

from iris_service.core import (AccountError, RateLimitError, ServiceCore,
                               StateError)

log = logging.getLogger("iris_service")

SDK_PORT = 7850
FND_PORT = 7851


class ServiceDaemon:
    def __init__(self, sdk_port: int = SDK_PORT, fnd_port: int = FND_PORT,
                 host: str = "127.0.0.1"):
        self.core = ServiceCore()
        self.sdk_port = sdk_port
        self.fnd_port = fnd_port
        self.host = host          # 跨机部署绑 Tailscale IP（如 100.x.y.z）
        self._ctx = zmq.Context()
        self._running = False

    def start(self) -> None:
        for port, handler, name in (
                (self.sdk_port, self._handle_sdk, "svc-sdk"),
                (self.fnd_port, self._handle_fnd, "svc-fnd")):
            s = self._ctx.socket(zmq.REP)
            s.bind(f"tcp://{self.host}:{port}")
            threading.Thread(target=self._serve, args=(s, handler),
                             name=name, daemon=True).start()
        self._running = True
        log.info("Service: %s sdk-face=:%d fnd-face=:%d",
                 self.host, self.sdk_port, self.fnd_port)

    def stop(self) -> None:
        self._running = False       # socket 由守护线程随进程退场（避免跨线程 close 竞态）

    def _serve(self, sock, handler) -> None:
        while True:
            try:
                raw = sock.recv()
            except zmq.ZMQError:
                break
            try:
                reply = handler(msgpack.unpackb(raw, raw=False))
            except (AccountError, StateError, RateLimitError) as exc:
                reply = {"ok": False, "err_type": type(exc).__name__,
                         "error": str(exc)}     # 业务/限流错：结构化透传
            except Exception as exc:
                reply = {"ok": False, "err_type": type(exc).__name__,
                         "error": str(exc)}
            sock.send(msgpack.packb(reply, use_bin_type=True))

    # ---- SDK 面 ----

    def _handle_sdk(self, q: dict) -> dict:
        core = self.core
        op = q.get("op", "")
        if op == "hello":
            return {"ok": True, "node": "service",
                    "device_state_list": core.sdk_device_states()}
        if op == "register":
            core.accounts.register(q.get("sdk_id", ""), q.get("secret", ""))
            return {"ok": True}
        if op == "login":
            return {"ok": True,
                    "token": core.accounts.login(q.get("sdk_id", ""),
                                                 q.get("secret", ""))}
        if op == "device_states":
            return {"ok": True, "device_state_list": core.sdk_device_states()}
        # 以下操作需鉴权
        core.accounts.verify(q.get("sdk_id", ""), q.get("token", ""))
        if op == "occupy":
            return {"ok": True, **core.sdk_occupy(
                q["sdk_id"], q["device_ip"], q["device_id"])}
        if op == "release":
            return {"ok": True, **core.sdk_release(
                q["sdk_id"], q["device_ip"], q["device_id"])}
        if op == "submit_task":
            return {"ok": True, **core.sdk_submit_task(
                q["sdk_id"], q.get("task") or {})}
        if op == "task_info":                     # 任务内容可读（不含 h5）
            return {"ok": True,
                    "task": core.sdk_task_info(q["sdk_id"],
                                                q.get("task_id", ""))}
        if op == "task_list":                     # 本账号任务清单
            return {"ok": True,
                    "tasks": core.sdk_task_list(q["sdk_id"])}
        if op == "get_data":
            return {"ok": True, **core.sdk_get_data(
                q["sdk_id"], q.get("task_id", ""))}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # ---- Foundation 面 ----

    def _handle_fnd(self, q: dict) -> dict:
        core = self.core
        op = q.get("op", "")
        if op == "register":
            return core.fnd_register(q.get("foundation_id", ""),
                                     q.get("device_list") or {})
        if op == "poll":
            return core.fnd_poll(q.get("foundation_id", ""))
        if op == "reply":
            return core.fnd_reply(q.get("foundation_id", ""),
                                  q.get("req_id", ""),
                                  q.get("reply") or {},
                                  q.get("device_list") or {})
        return {"ok": False, "error": f"unknown op {op!r}"}


def main(argv=None) -> int:
    import logging.handlers
    from pathlib import Path
    # 文件日志：pythonw/计划任务运行时 stderr 不可见——审计留痕落盘
    log_dir = Path.home() / "iris_ver2" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[
                            logging.StreamHandler(),
                            logging.handlers.RotatingFileHandler(
                                log_dir / "service.log", maxBytes=2_000_000,
                                backupCount=3, encoding="utf-8"),
                        ])
    ap = argparse.ArgumentParser(description="IRIS ver2 Service（核）")
    ap.add_argument("--sdk-port", type=int, default=SDK_PORT)
    ap.add_argument("--fnd-port", type=int, default=FND_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址：跨机部署用 Tailscale IP（如 100.78.179.41）；"
                         "0.0.0.0 = 全网卡（注意安全）")
    args = ap.parse_args(argv)
    ServiceDaemon(args.sdk_port, args.fnd_port, args.host).start()
    print(f"iris-service ver2: {args.host} sdk-face=:{args.sdk_port} "
          f"fnd-face=:{args.fnd_port}（Ctrl-C 退出）", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
