"""New Focus 8742 Picomotor 控制器 —— 设备接口层（USB 虚拟串口 / RS-232）。

纯 Python + pyserial，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

协议来源：New Focus 8742 User's Manual「Remote Command Set」（Newport 官方
手册）+ pylablib Picomotor8742 实现交叉核对（github.com/AlexShkarin/pyLabLib,
devices/Newport/picomotor.py）。串口 **19200, 8, N, 1**（USB CDC 波特率无实效）；
命令帧 ASCII，写以 ``\\n`` 结尾；**只有查询命令（以 ``?`` 结尾）才回帧**，
应答以 ``\\n`` 结尾。命令 = 轴号前缀 + 命令字，多台控制器 RS-485 菊链时加
地址前缀 ``N>``（应答原样带回该前缀）：

=================  ======================  ==============================
用途               主机 → 控制器           控制器 → 主机
=================  ======================  ==============================
识别               ``*IDN?``               ``NEWFOCUS,8742-xxxx,…``
相对运动           ``1PR+500``             （无应答，步数有符号）
绝对运动           ``1PA-200``             （无应答，位置寄存器值）
位置查询           ``1TP?``                ``+123``（32 位有符号整数）
运动完成查询       ``1MD?``                ``1``=完成 ``0``=运动中
速度/加速度        ``1VA1000`` ``1AC100``  查询 ``1VA?`` ``1AC?``
设零（不运动）     ``1DH0``                当前点定义为参考 0
停止本轴/全部      ``1ST`` / ``AB``        ``ST`` 平滑停，``AB`` 急停
电机类型           ``1QM?``                ``0/1/2/3`` 无/未知/微型/标准
菊链地址扫描       ``SC?``                 位掩码 bit N = 地址 N 在线
=================  ======================  ==============================

多轴模型：一台 8742 带 4 通道；**6 根电机 = 两台 8742（本机实测：两台均走
原生 USB，VID_104D/PID_4000，设备树显示 "Picomotor Controller"，非 COM 口；
RS-232 串口为回退路径）**。驱动把探测到的每台控制器（unit）× 地址 × 通道
铺平成轴表，**每台 4 个通道全铺**（两台 = 8 个口；本机 6 口接电机，2 口空置
——空置口照样建轴，只是动了也不走），轴名 ``"1".."N"``（unit 顺序 = USB
枚举序；同口多地址按地址顺序）。每根电机是一个 :class:`PicomotorAxis`（实现平台 MotorAxis 能力协议）；
入口类 :class:`Picomotor` 把默认轴（第一根）委托为合同的 position/move_to/…
入口，保证 GUI/CLI 的通用写读服务直接可用，其余轴经 ``.axis("2")`` 访问。

开环注意：8742 没有编码器，``TP`` 是**驱动器记账位置**（丢步不感知）；
picomotor 单步 ≈ 30 nm（标准电机），常量 ``STEPS_PER_UM`` 仅作粗换算参考，
标定请用真机实测。

用法::

    from iris_foundation.devices.picomotor import Picomotor
    with Picomotor() as pm:              # 自动探测 8742 串口 + 菊链地址
        pm.axis_names                    # ('1', '2', … '6')
        ax = pm.axis("1")
        ax.position                      # TP? 回读（驱动器步数）
        ax.move_by(100, wait=True)       # PR+100
        ax.move_to(0, wait=True)         # PA0
        ax.set_zero()                    # DH0 当前点设为 0
        pm.move_by(50)                   # 默认轴（第 1 根）
"""

from __future__ import annotations

import re
import threading
import time

import serial
from serial.tools import list_ports

from iris_foundation.core.contract import DriverManifest, ParamDesc
from iris_foundation.core.topics import Dev
from iris_foundation.devices.transports import SerialTransport

# 8742 RS-232 固定 19200 8N1（手册 Remote Command Set 章）；USB CDC 忽略波特率
BAUDRATE = 19200

# 命令帧结尾（USB/串口实测均接受 CR+LF）；应答以 \n 结尾
WRITE_END = "\r\n"
REPLY_END = b"\n"

# 8742 原生 USB（pyusb）：两台控制器以 VID_104D&PID_4000 出现（非 COM 口！）
USB_VID = 0x104D
USB_PID = 0x4000
USB_WRITE_EP = 0x02
USB_READ_EP = 0x81

# 识别串：*IDN? → "New_Focus 8742 v3.04 …"（真机实测，含下划线/空格）
IDN_VENDOR = "NEWFOCUS"
IDN_MODEL = "8742"

# 位置寄存器为 32 位有符号；留 1 步裕量作参数声明边界
POS_LIMIT = 2_000_000_000

# 粗换算参考（仅显示用，勿当标定值）：标准 picomotor 电机 ≈ 30 nm/步
STEPS_PER_UM = 33_333.0

# *IDN? 探测超时（无应答的口快速跳过）
PROBE_TIMEOUT_S = 0.8

_INT_RE = re.compile(r"[-+]?\d+")


def _idn_matches(idn: str) -> bool:
    """识别串判定：去符号归一后同含 NEWFOCUS 与 8742（容忍 New_Focus 空格）。"""
    norm = re.sub(r"[^A-Z0-9]", "", idn.upper())
    return IDN_VENDOR in norm and IDN_MODEL in norm


class UsbTransport:
    """8742 原生 USB 传输（pyusb/libusb）——字节层，与 SerialTransport 同接口。

    8742 的 USB 口是**原生 USB 端点**（VID_104D/PID_4000，Windows 设备树里
    显示 "Picomotor Controller"，不是 COM 口）：写端点 0x02，读端点 0x81。
    pyusb 懒加载：未装时仅 open() 抛 ImportError，平台其余部分不受影响。
    """

    def __init__(self, index: int = 0, timeout_ms: int = 2000):
        self.index = index
        self.timeout_ms = timeout_ms
        self._dev = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def open(self) -> None:
        if self._dev is not None:
            return
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise ImportError(
                "8742 原生 USB 需要 pyusb：conda activate Iris && "
                "pip install pyusb") from exc
        devs = list(usb.core.find(find_all=True, idVendor=USB_VID, idProduct=USB_PID))
        if self.index >= len(devs):
            raise PicomotorError(
                f"第 {self.index + 1} 台 8742 USB 控制器不存在"
                f"（在线 {len(devs)} 台）")
        dev = devs[self.index]
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass                      # Windows 无内核占用，忽略
        dev.set_configuration()
        self._dev = dev
        self._usb_util = usb.util

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._usb_util.dispose_resources(self._dev)
            finally:
                self._dev = None

    def drain(self) -> None:
        """清空接收端点（读到超时为止，丢弃）。"""
        if self._dev is None:
            return
        while True:
            try:
                self._dev.read(USB_READ_EP, 256, 50)
            except Exception:
                return

    def query_frame(self, request: bytes, reply_end: bytes,
                    timeout_s: float = 2.0, chunk: int = 256) -> bytes:
        """写一帧并读到 ``reply_end`` 终止符；超时抛 TimeoutError。"""
        if self._dev is None:
            raise IOError("USB 未打开（先 open）")
        self.write_frame(request)
        buf = bytearray()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and reply_end not in buf:
            try:
                buf += self._dev.read(USB_READ_EP, chunk,
                                      max(10, int((deadline - time.monotonic())
                                                  * 1000)))
            except Exception:
                continue                # 端点暂时无数据：到 deadline 再判
        if reply_end not in buf:
            raise TimeoutError(f"等待应答超时: {request!r}")
        return bytes(buf.split(reply_end)[0])

    def write_frame(self, frame: bytes) -> None:
        """写一帧（无应答路径与查询共用；8742 对非查询命令不回帧）。"""
        if self._dev is None:
            raise IOError("USB 未打开（先 open）")
        self._dev.write(USB_WRITE_EP, frame, self.timeout_ms)


class PicomotorError(IOError):
    """8742 通信失败 / 应答无法解析。"""


class PicomotorAxis:
    """一根 Picomotor（某控制器某通道）——实现平台 MotorAxis 能力协议。

    单位为**步**（驱动器原生单位，无换算假设）；position 是驱动器记账值
    （开环，丢步不感知），断电不保持，上电为 0。
    """

    def __init__(self, ctrl: "Picomotor", unit: int, addr: int, ch: int,
                 name: str):
        self._ctrl = ctrl
        self.unit = unit          # SerialTransport 序号（探测顺序）
        self.addr = addr          # RS-485 菊链地址（1~31，单台 = 1）
        self.ch = ch              # 控制器通道 1~4
        self.name = name          # 轴名 "1".."N"

    # ------------------------------------------------------------- 帧收发

    def _cmd(self, op: str, value: int | None = None, query: bool = False) -> str:
        return self._ctrl._axis_cmd(self.unit, self.addr, self.ch, op,
                                    value, query=query)

    # ------------------------------------------------------------- 查询

    @property
    def position(self) -> int:
        """当前位置（步，``TP?``——驱动器记账值，非编码器测量）。"""
        return int(self._ctrl._parse_int(self._cmd("TP", query=True)))

    @property
    def is_moving(self) -> bool:
        """运动中（``MD?``：1=完成 0=运动中，取反即本属性）。"""
        return int(self._ctrl._parse_int(self._cmd("MD", query=True))) == 0

    def wait_until_still(self, timeout: float = 60.0, poll_s: float = 0.05) -> None:
        """轮询 ``MD?`` 直到运动完成；超时抛错。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_moving:
                return
            time.sleep(poll_s)
        raise PicomotorError(f"等待轴 {self.name} 运动完成超时（{timeout:g}s）")

    def get_motor_type(self) -> int:
        """电机类型（``QM?``：0 无 / 1 未知 / 2 微型 / 3 标准）。"""
        return int(self._ctrl._parse_int(self._cmd("QM", query=True)))

    def get_velocity(self) -> int:
        """最大速度（``VA?``，步/s，1~2000）。"""
        return int(self._ctrl._parse_int(self._cmd("VA", query=True)))

    def get_acceleration(self) -> int:
        """加速度（``AC?``，步/s²，1~200000）。"""
        return int(self._ctrl._parse_int(self._cmd("AC", query=True)))

    # ------------------------------------------------------------- 运动控制

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> int:
        """绝对运动 ``PA``（目标 = 位置寄存器值，步）。``wait=True`` 等 MD。"""
        pos = int(round(float(pos)))
        if not -POS_LIMIT <= pos <= POS_LIMIT:
            raise PicomotorError(f"位置 {pos} 步超出 ±{POS_LIMIT}，拒绝发送")
        self._cmd("PA", pos)
        if wait:
            self.wait_until_still(timeout=timeout)
        return pos

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> int:
        """相对运动 ``PR``（步数有符号）。"""
        delta = int(round(float(delta)))
        self._cmd("PR", delta)
        if wait:
            self.wait_until_still(timeout=timeout)
        return self.position

    def jog(self, direction: int) -> None:
        """连续运动 ``MV+/MV-``（直到 stop）。"""
        self._cmd("MV", 1 if direction >= 0 else -1)

    def set_zero(self) -> None:
        """当前点定义为参考 0（``DH0``，不运动；位置寄存器清零）。"""
        self._cmd("DH", 0)

    def set_velocity(self, steps_per_s: int) -> int:
        """最大速度 ``VA``（1~2000 步/s）。"""
        v = int(steps_per_s)
        if not 1 <= v <= 2000:
            raise ValueError(f"速度范围 1~2000 步/s: {v}")
        self._cmd("VA", v)
        return v

    def set_acceleration(self, steps_per_s2: int) -> int:
        """加速度 ``AC``（1~200000 步/s²）。"""
        a = int(steps_per_s2)
        if not 1 <= a <= 200_000:
            raise ValueError(f"加速度范围 1~200000 步/s²: {a}")
        self._cmd("AC", a)
        return a

    def stop(self, all_axes: bool = False) -> None:
        """平滑停本轴（``ST``）；``all_axes=True`` 时急停全部（``AB``）。"""
        if all_axes:
            self._ctrl._raw_cmd(self.unit, self.addr, "AB")
        else:
            self._cmd("ST")

    def __repr__(self) -> str:
        return (f"<PicomotorAxis {self.name}: unit{self.unit}"
                f" addr{self.addr} ch{self.ch}>")


class Picomotor:
    """New Focus 8742 Picomotor 控制器组（1~N 台，USB/串口 + RS-485 菊链）。

    Parameters
    ----------
    port : str | None
        串口名（如 ``"COM5"``）。``None`` = connect() 时逐口发 ``*IDN?``
        探测，应答含 NEWFOCUS/8742 即认定（可多口，每口一个 unit）。
    channels : tuple[int, ...]
        每台控制器暴露的通道（默认 (1, 2, 3, 4) = 8742 全部 4 个口；两台
        = 8 个口，本机 6 口接电机、2 口空置。空置口照样建轴对象，
        只是动了也不走）。
    addrs : tuple[int, ...] | None
        强制指定菊链地址表（跳过 ``SC?`` 自动扫描）。``None`` = 自动。
    transport : SerialTransport | None
        注入传输（无硬件冒烟/测试用，单 unit）。
    """

    def __init__(self, port: str | None = None,
                 channels: tuple[int, ...] = (1, 2, 3, 4),
                 addrs: tuple[int, ...] | None = None,
                 transport: SerialTransport | None = None):
        self.port = port
        self.channels = tuple(int(c) for c in channels)
        self._forced_addrs = None if addrs is None else tuple(int(a) for a in addrs)
        self._transports: list[SerialTransport] = []
        self._injected = transport
        self._unit_addrs: list[tuple[int, ...]] = []   # 每 unit 的菊链地址表
        self.axes: dict[str, PicomotorAxis] = {}
        self._default: PicomotorAxis | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（§4.3）——默认轴位置（步）。"""
        return (ParamDesc(
            key="position", ptype="int", label="Position (step)", unit="step",
            apply="write", vmin=-float(POS_LIMIT), vmax=float(POS_LIMIT),
            access="rw", decimals=0, settle_ms=0),)

    # ------------------------------------------------------------- 生命周期

    def connect(self, port: str | None = None) -> None:
        """探测/打开传输并联络（``*IDN?``），扫菊链地址，铺轴表。

        优先原生 USB（VID_104D/PID_4000，在线几台收几台）；无 USB 设备或
        pyusb 缺席时回退逐 COM 口探测（发 ``*IDN?``，归一化匹配 NEWFOCUS/8742）。
        随后每台问 ``SC?`` 菊链地址位掩码（失败按单台地址 1 处理，不阻断）。
        """
        if self._transports:
            return
        if self._injected is not None:      # 测试注入路径（单 unit）
            if not self._injected.is_open:
                self._injected.open()
            self._adopt([self._injected], [self._forced_addrs or (1,)])
            return
        found: list = []
        if port is None:                    # 原生 USB 优先
            try:
                import usb.core
                n_usb = len(list(usb.core.find(find_all=True,
                                               idVendor=USB_VID, idProduct=USB_PID)))
            except ImportError:
                n_usb = 0
            for i in range(n_usb):
                tp = UsbTransport(index=i)
                try:
                    tp.open()
                    idn = self._ask(tp, None, "*IDN?")
                except (PicomotorError, TimeoutError, Exception):
                    tp.close()
                    continue
                if _idn_matches(idn):
                    found.append(tp)
        if not found:                       # 串口回退（RS-232 / 虚拟串口）
            candidates = [port] if port else [
                p.device for p in sorted(list_ports.comports(),
                                         key=lambda p: p.device)]
            for cand in candidates:
                tp = SerialTransport(cand, BAUDRATE,
                                     read_timeout_s=PROBE_TIMEOUT_S,
                                     write_timeout_s=PROBE_TIMEOUT_S)
                try:
                    tp.open()
                except serial.SerialException:
                    continue
                try:
                    idn = self._ask(tp, None, "*IDN?")
                except (PicomotorError, TimeoutError, serial.SerialException):
                    tp.close()
                    continue
                if _idn_matches(idn):
                    tp.close()              # 探测口超时短，换正式超时重开
                    tp = SerialTransport(cand, BAUDRATE)
                    tp.open()
                    found.append(tp)
                else:
                    tp.close()
            if not found:
                raise PicomotorError(
                    "未找到 8742 Picomotor 控制器（USB: VID_104D/PID_4000 无设备；"
                    "串口 *IDN? 无 NEWFOCUS 应答）。"
                    f"已尝试串口: {', '.join(candidates) or '（无）'}；"
                    "若 New Focus Picomotor Application 正在运行请先关闭"
                    "（独占设备）")
        self.port = port or "USB"           # USB 路径无串口名
        addrs_per_unit = [self._scan_addrs(tp) for tp in found]
        self._adopt(found, addrs_per_unit)

    def _adopt(self, transports, addrs_per_unit) -> None:
        self._transports = list(transports)
        self._unit_addrs = [list(a) for a in addrs_per_unit]
        self.axes = {}
        n = 0
        for u, addrs in enumerate(self._unit_addrs):
            for addr in addrs:
                for ch in self.channels:
                    n += 1
                    self.axes[str(n)] = PicomotorAxis(self, u, addr, ch, str(n))
        self._default = self.axes.get("1")

    def _scan_addrs(self, tp: SerialTransport) -> tuple[int, ...]:
        """``SC?`` 位掩码 → 在线地址表；查询失败按单台 (1,) 处理。"""
        if self._forced_addrs is not None:
            return self._forced_addrs
        try:
            mask = int(self._parse_int(self._ask(tp, None, "SC?")))
        except (PicomotorError, TimeoutError):
            return (1,)
        addrs = tuple(a for a in range(1, 32) if mask & (1 << a))
        return addrs or (1,)

    def disconnect(self) -> None:
        for tp in self._transports:
            try:
                tp.close()
            finally:
                pass
        self._transports = []
        self._unit_addrs = []
        self.axes = {}
        self._default = None

    def __enter__(self) -> "Picomotor":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------- 帧收发

    @staticmethod
    def _frame(addr: int | None, body: str) -> str:
        prefix = f"{addr}>" if addr is not None and addr > 1 else ""
        return prefix + body

    def _ask(self, tp: SerialTransport, addr: int | None, body: str,
             timeout_s: float = 2.0) -> str:
        """发查询帧并收一帧应答（去掉地址回显前缀）。"""
        with self._lock:
            frame = self._frame(addr, body)
            try:
                raw = tp.query_frame(frame.encode("ascii") + WRITE_END.encode(),
                                     REPLY_END, timeout_s=timeout_s)
            except TimeoutError as exc:
                raise PicomotorError(f"等待应答超时: {frame!r}") from exc
            reply = raw.decode("ascii", errors="replace").strip("\r\n\x00 ")
            hdr = f"{addr}>"
            if addr is not None and addr > 1 and reply.startswith(hdr):
                reply = reply[len(hdr):]
            return reply

    def _raw_cmd(self, unit: int, addr: int | None, body: str) -> None:
        """发非查询命令（8742 对非查询不回帧，写后清缓冲防残包）。"""
        tp = self._transports[unit]
        tp.write_frame((self._frame(addr, body) + WRITE_END).encode("ascii"))
        time.sleep(0.02)
        tp.drain()

    def _axis_cmd(self, unit: int, addr: int, ch: int, op: str,
                  value: int | None = None, query: bool = False) -> str:
        """拼轴帧 ``[N>]chOP[value][?]``；查询回原文，命令发后清缓冲。"""
        body = f"{ch}{op}" + ("" if value is None else
                              ("+" if op == "MV" and value > 0 else "")
                              + str(int(value))) + ("?" if query else "")
        tp = self._transports[unit]
        if query:
            return self._ask(tp, addr, body)
        self._raw_cmd(unit, addr, body)
        return ""

    @staticmethod
    def _parse_int(reply: str) -> int:
        m = _INT_RE.search(reply)
        if m is None:
            raise PicomotorError(f"应答无法解析为整数: {reply!r}")
        return int(m.group())

    # ------------------------------------------------------------- 轴访问

    def axis(self, name: str) -> PicomotorAxis:
        """按名取轴（``pm.axis("3")``）。"""
        ax = self.axes.get(str(name))
        if ax is None:
            raise KeyError(f"无此轴: {name!r}（可用: {sorted(self.axes)}）")
        return ax

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.axes, key=int))

    def idn(self, unit: int = 0) -> str:
        """``*IDN?`` 识别串（第 unit 台控制器）。"""
        return self._ask(self._transports[unit], None, "*IDN?")

    # --------------------------------------------- MotorAxis 合同（默认轴委托）

    @property
    def position(self) -> int:
        """默认轴（第 1 根）位置（步）——GUI/CLI 通用读服务走这里。"""
        return self._default.position

    @property
    def is_moving(self) -> bool:
        return self._default.is_moving

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> int:
        """默认轴绝对运动（参数目录 position 的写侧，经合同自动推导）。"""
        return self._default.move_to(pos, wait=wait, timeout=timeout)

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> int:
        return self._default.move_by(delta, wait=wait, timeout=timeout)

    def stop(self, all_axes: bool = False) -> None:
        self._default.stop(all_axes=all_axes)

    # ------------------------------------------------------------- 多轴便捷（GUI 页/脚本用）

    def axis_call(self, name: str, op: str, *args, **kwargs):
        """按轴名调用轴方法（``pm.axis_call("4", "move_by", 100)``）。"""
        return getattr(self.axis(name), op)(*args, **kwargs)

    def axis_positions(self) -> dict[str, int]:
        """全部轴位置快照（GUI 轮询用；一次串行会话读完，免逐轴排队）。"""
        return {n: ax.position for n, ax in self.axes.items()}

    def axis_snapshot(self) -> dict:
        """全部轴完整状态（位置/运动/电机类型/拓扑），连接刷新用。"""
        return {n: {"position": ax.position, "moving": ax.is_moving,
                    "motor_type": ax.get_motor_type(),
                    "unit": ax.unit, "addr": ax.addr, "ch": ax.ch}
                for n, ax in self.axes.items()}

    def stop_all(self) -> None:
        """急停全部控制器全部通道（逐 unit 发 ``AB``）。"""
        for u, addrs in enumerate(self._unit_addrs):
            for addr in addrs:
                self._raw_cmd(u, addr, "AB")


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="picomotor", dev_id=Dev.PICOMOTOR, entry="Picomotor",
    display_name="Picomotor", model="New Focus 8742 ×2",
    capabilities=("motor_axis",), default_transport="native",
    page="labcontrol.gui.pages.picomotor_page",
    connect_timeout_ms=30000, disconnect_timeout_ms=15000)
