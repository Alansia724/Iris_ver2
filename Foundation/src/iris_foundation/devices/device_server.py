"""进程外 device server（P5 样式件，docs/architecture.md §8）。

把一台设备搬进独立进程（DLL 崩溃隔离 / 将来另一台电脑）的**可跑模板**——
验证合同的三条预留规则成立：合同层零 Qt（本模块不 import GUI）、命令串行
（每连接一次一条，owner 线程纪律）、载荷可 pickle（ndarray 帧直接过界）。
不是产线组件：触发条件（第二台电脑/需隔离的 DLL/多人共享）出现前，
GUI/CLI 不接入本模块。

本机两进程演示::

    终端 A:  python -m iris_foundation.devices.device_server --demo --port 55701
    终端 B:  python -c "from iris_foundation.devices.device_server import DeviceProxy; \
             p = DeviceProxy(('127.0.0.1', 55701), b'labcontrol-demo'); \
             print(p.info()); print(p.call('acquire_frame', 0.01)); p.close()"

协议：multiprocessing.connection（标准库；authkey 认证；载荷 pickle——
§8 规则 3：把函数调用换成消息传递，合同零改动）。
安全边界：pickle 不设防——**只绑 127.0.0.1**；跨机使用先读
docs/add_device_guide.md 的安全一节（SSH 隧道或等真做 P5 时换带认证
加密的传输）。

本模块无 DRIVER_MANIFEST：注册表扫描会跳过它，不会注册成设备。
"""

from __future__ import annotations

import argparse


# ---------------------------------------------------------------- 服务端

def _info_of(device) -> dict:
    """设备自描述（§4/§6）：类名 + params() 声明（ParamData 可 pickle）。"""
    params_fn = getattr(type(device), "params", None)
    return {"class": type(device).__name__,
            "params": list(params_fn()) if params_fn is not None else []}


def _serve_connection(conn, device) -> bool:
    """一条连接的命令循环；返回 True = 收到 shutdown（服务退出）。"""
    while True:
        try:
            cmd = conn.recv()
        except EOFError:
            return False            # 客户端掉线，回 accept 等下一个
        op = cmd.get("op")
        try:
            if op == "call":
                res = getattr(device, cmd["method"])(*cmd.get("args", ()),
                                                     **cmd.get("kwargs", {}))
                reply = ("ok", res)
            elif op == "get":
                reply = ("ok", getattr(device, cmd["name"]))
            elif op == "set":
                setattr(device, cmd["name"], cmd["value"])
                reply = ("ok", None)
            elif op == "info":
                reply = ("ok", _info_of(device))
            elif op == "shutdown":
                conn.send(("ok", None))
                return True
            else:
                raise ValueError(f"未知操作 {op!r}")
        except Exception as exc:    # 异常穿进程边界：类型名+消息
            reply = ("err", f"{type(exc).__name__}: {exc}")
        conn.send(reply)


def serve(device, address, authkey: bytes, ready=None) -> None:
    """托管一台设备实例：accept → 串行执行命令 → shutdown 退出并断开设备。"""
    from multiprocessing.connection import Listener
    listener = Listener(address, authkey=authkey)
    try:
        if ready is not None:
            ready()
        while True:
            conn = listener.accept()
            try:
                if _serve_connection(conn, device):
                    break
            finally:
                conn.close()
    finally:
        listener.close()
        teardown = getattr(device, "disconnect", None)
        if teardown is not None:
            try:
                teardown()
            except Exception:
                pass


# ---------------------------------------------------------------- 客户端

class DeviceProxy:
    """设备代理（模板级显式 API，不做 __getattr__ 自动转发魔法）。

    call/get/set/info/close 五个动词；ndarray 等可 pickle 载荷原样过界。
    """

    def __init__(self, address, authkey: bytes):
        from multiprocessing.connection import Client
        self._conn = Client(address, authkey=authkey)

    def call(self, method: str, *args, **kwargs):
        """调用设备方法并取回返回值（如 acquire_frame → ndarray 帧）。"""
        return self._rpc({"op": "call", "method": method,
                          "args": args, "kwargs": kwargs})

    def get(self, name: str):
        """读设备属性（如 position / wavelength）。"""
        return self._rpc({"op": "get", "name": name})

    def set(self, name: str, value):
        """写设备属性。"""
        return self._rpc({"op": "set", "name": name, "value": value})

    def info(self) -> dict:
        """设备自描述（类名 + params() 声明）。"""
        return self._rpc({"op": "info"})

    def close(self) -> None:
        """通知服务端退出并关闭连接。"""
        self._rpc({"op": "shutdown"})
        self._conn.close()

    def _rpc(self, cmd: dict):
        self._conn.send(cmd)
        status, payload = self._conn.recv()
        if status == "err":
            raise RuntimeError(f"设备端异常: {payload}")
        return payload


# ---------------------------------------------------------------- 演示设备（自检用，不进注册表）

class DemoStage:
    """演示/自检假设备：一台"运动轴 + 采集"合体，无硬件冒烟 P5 全链路。"""

    def __init__(self):
        self._pos = 0.0
        self._connected = False

    def connect(self) -> str:
        self._connected = True
        return "demo-stage"

    def disconnect(self) -> None:
        self._connected = False

    @property
    def position(self) -> float:
        return self._pos

    @position.setter
    def position(self, value: float) -> None:
        self._pos = float(value)

    @property
    def is_moving(self) -> bool:
        return False

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        self._pos = float(pos)
        return self._pos

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        self._pos += float(delta)
        return self._pos

    def stop(self, all_axes: bool = False) -> None:
        pass

    def acquire_frame(self, exposure: float = 0.01):
        """返回 ndarray（证明 §8 规则 3：可 pickle 载荷过进程边界）。"""
        import numpy as np
        scale = max(float(exposure), 1.0)
        return (np.arange(16, dtype=np.uint16).reshape(4, 4) * int(scale))

    @classmethod
    def params(cls):
        from iris_foundation.core.contract import ParamDesc
        return (ParamDesc(key="position", ptype="float", label="Position",
                          unit="mm", apply="write", vmin=-1000.0, vmax=1000.0),)


# ---------------------------------------------------------------- CLI

def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="labcontrol 进程外 device server（P5 样式件）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dev", help="注册表里的 dev_id（如 attenuator）")
    group.add_argument("--demo", action="store_true",
                       help="托管演示假设备（无硬件自检）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="只绑本机（默认）；跨机见 docs/add_device_guide.md")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--authkey", default="labcontrol-demo",
                        help="连接认证口令（生产用 python -c \"import secrets;"
                        "print(secrets.token_hex(16))\" 生成）")
    args = parser.parse_args(argv)

    if args.demo:
        device = DemoStage()
    else:
        from iris_foundation.core import registry
        device = registry.create(args.dev)     # transport 配置随 P5 实装接入
    connect = getattr(device, "connect", None)
    if connect is not None:
        connect()

    def _ready():
        print(f"device server: {type(device).__name__} "
              f"listening on {args.host}:{args.port}", flush=True)

    try:
        serve(device, (args.host, args.port), args.authkey.encode("utf-8"),
              ready=_ready)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
