"""Newport CONEX-CC 控制器 —— 设备接口层（USB 虚拟串口 / RS-232）。

纯 Python + pyserial，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

协议来源：CONEX-CC Command Interface Manual（本机
``C:/Users/W/Desktop/Devices/newport conex CC/``）+ 2026-08 真机实测
（COM5 = SR50CC 旋转台，COM6 = MFA-CC 执行器，固件 2.0.1）。串口
**921600, 8, N, 1**；命令帧 = ``地址 + 命令字 [参数]`` 以 ``\\r\\n`` 结尾；
**每条命令都回帧**，应答 = 命令回显 + 返回值（``1TP`` → ``1TP-0``），
以 ``\\r\\n`` 结尾。查询命令惯例带 ``?``（``ID?``/``RS?``），部分不带
（``TP``/``TE``——实测两者都回帧，本驱动统一按"发必收"处理）：

=================  ====================  ==============================
用途               主机 → 控制器         控制器 → 主机
=================  ====================  ==============================
识别               ``1VE``               ``1VE CONEX-CC 2.0.1``
型号序列号         ``1ID?``              ``1IDSR50CC_PN:…_UD:…``
位置查询           ``1TP``               ``1TP12.3456``（mm）
绝对运动           ``1PA10.5``           （本机实测不即回帧；错误经 ``TE`` 查询）
相对运动           ``1PR-2``             （同上）
回零（Home）       ``1OR``               （同上）
停止               ``1ST``               （同上）
状态寄存器         ``1RS?``              ``1RS000032``（位掩码）
错误码             ``1TE``               ``@`` = 无错，其余见手册
软限位             ``1SL?`` / ``1SR?``   负/正限位（mm）
=================  ====================  ==============================

多台模型：CONEX-CC 面板有 **两个 USB 口（预留菊花链）**，本机两台各自
占用一个 COM 口（地址都是 1）。驱动把探测到的每台控制器铺平成轴表，
轴名 ``"1".."N"``（探测顺序 = COM 口名字典序）；每台是一个
:class:`ConexCcAxis`（实现平台 MotorAxis 能力协议），入口类
:class:`ConexCc` 把默认轴（第一台）委托为合同的 position/move_to/…
入口，其余轴经 ``.axis("2")`` 访问。同一串口多地址（真菊花链）后继
再加：本驱动按单地址 = 1 处理。

编码器事实：CONEX-CC 带增量编码器，``TP`` 是**真实测量位置**（断电
不丢参考，区别于 Picomotor 的开环记账）；但**上电后未回零前绝对位置
不可信**——``connect`` 不自动 Home（真实运动，由使用者显式触发），
未回零时运动命令照发（固件按当前坐标系执行），文档层提示即可。

用法::

    from iris_foundation.devices.conex_cc import ConexCc
    with ConexCc() as cc:                # 自动探测 CONEX 串口（921600）
        cc.axis_names                    # ('1', '2')
        ax = cc.axis("1")                # SR50CC 旋转台
        ax.position                      # TP 回读（mm）
        ax.move_to(10.0, wait=True)      # PA10（阻塞到停稳）
        ax.move_by(-2.0, wait=False)     # PR-2（立即返回）
        ax.home()                        # OR 回零
        ax.stop()                        # ST 停止
        cc.move_to(5.0)                  # 默认轴（第 1 台）
"""

from __future__ import annotations

import logging
import re
import threading
import time

import serial
from serial.tools import list_ports

logger = logging.getLogger("iris_foundation.devices.conex_cc")

from iris_foundation.core.contract import DriverManifest, ParamDesc
from iris_foundation.core.topics import Dev
from iris_foundation.devices.transports import SerialTransport

# CONEX-CC RS-232 固定 921600 8N1（手册默认；真机实测一致）
BAUDRATE = 921600

# 命令帧结尾（手册：CR+LF）；**应答实测只以 CR 结尾**（2026-08 真机抓包：
# b'1TP0.499\r'，无 LF）——终止符必须用 \r，用 \n 会等到超时且偶发错位
WRITE_END = "\r\n"
REPLY_END = b"\r"

# 识别串：1VE → "CONEX-CC x.y.z"
IDN_TOKEN = "CONEX"

# 探测超时（无应答的口快速跳过）
PROBE_TIMEOUT_S = 0.8

# 到位判据：连续 N 次轮询位置变化 < STABLE_TOL_MM 视为停稳
STABLE_TOL_MM = 0.001
STABLE_COUNT = 3

# 参数声明边界（mm；真机软限位连接后由 SL?/SR? 读出并单独强制）
PARAM_LIMIT_MM = 1000.0

#: 同一串口最多探几个地址（CONEX-CC 支持菊花链，地址从 1 起）
MAX_ADDR_PER_PORT = 3
#: 地址探测超时：不存在的地址不应答，短超时避免连接变慢
PROBE_TIMEOUT_S = 0.3

#: 旋转台型号前缀（Newport 家族：PR50CC / SR50CC / RV…）；其余当平移台。
#: 注：SR50 也是旋转台（驱动旧注释写成“平移台”是错的，2026-10-22 真机确认）。
ROTARY_PREFIXES = ("PR", "SR", "RV")

# 扫描写侧到位判据：位置连续 N 次不变（停稳）且残余在容差内
ARRIVE_STABLE_N = 4
ARRIVE_TOL_MM = 0.05      # 平移台停稳后残余（mm）
ARRIVE_TOL_DEG = 0.1      # 旋转台停稳后残余（°）
STABLE_TOL_DEG = 0.002

#: 运动命令（PA/PR/OR/ST）不即回帧：只等这么久就清缓冲（不阻塞扫描）
MOTION_REPLY_WAIT_S = 0.05


def _motion_handle(axis, target: float, tol: float, stable_tol: float):
    """发绝对运动并返回 WriteHandle：停稳且残余 ≤ tol 才算到位。

    只看瞬时距离会在运动中误判，或在 50 ms 轮询下整段跳过目标带
    （K10CR1 2026-10-21 实测教训）；先要求位置连续 ARRIVE_STABLE_N 次
    不变，再校验残余。非阻塞，引擎在 GUI 事件循环里轮询。
    """
    from iris_foundation.core.device_services import WriteHandle

    start = None
    try:
        start = axis.position
    except Exception:                                  # noqa: BLE001
        pass
    axis.move_to(target, wait=False)
    name = getattr(axis, "name", "?")
    logger.info("CONEX 轴 %s (%s)：%.4f → 目标 %.4f，限位 [%.3f, %.3f]",
                name, getattr(axis, "model", "?"),
                start if start is not None else float("nan"), target,
                getattr(axis, "pos_min", float("nan")),
                getattr(axis, "pos_max", float("nan")))
    err_fn = getattr(axis, "last_error", None)   # 假件/代理可能没这个方法
    if err_fn is not None:                       # 控制器拒动时只有 TE 会说原因
        try:
            err = err_fn()
        except Exception as exc:                       # noqa: BLE001
            logger.warning("CONEX 轴 %s 读错误码失败: %s", name, exc)
        else:
            if err not in ("@", "", "0"):
                logger.warning("CONEX 轴 %s 运动可能被拒：TE=%r", name, err)
    state = {"last": None, "stable": 0, "logged": False, "t": time.monotonic()}

    def _arrived() -> bool:
        try:
            pos = axis.position
        except Exception as exc:                       # noqa: BLE001
            logger.warning("CONEX 轴 %s 位置读取失败: %s", name, exc)
            return False
        prev, state["last"] = state["last"], pos
        state["stable"] = state["stable"] + 1 if (
            prev is not None and abs(pos - prev) < stable_tol) else 0
        now = time.monotonic()
        if now - state["t"] >= 5.0:        # 等位期间每 5 s 报一次：看得见是否在动
            state["t"] = now
            logger.info("CONEX 轴 %s 等待到位：当前 %.4f（目标 %.4f）",
                        name, pos, target)
        ok = state["stable"] >= ARRIVE_STABLE_N and abs(pos - target) <= tol
        if ok and not state["logged"]:
            state["logged"] = True
            logger.info("CONEX 轴 %s 到位：%.4f（目标 %.4f，残余 %.4f）",
                        name, pos, target, abs(pos - target))
        return ok

    return WriteHandle(target, _arrived, timeout_s=120.0)

_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


class ConexCcError(IOError):
    """CONEX-CC 通信失败 / 应答无法解析 / 控制器报错。"""


class ConexCcAxis:
    """一台 CONEX-CC 控制器（一个轴，单位：mm）——实现平台 MotorAxis 能力协议。

    position 是编码器测量值（``TP``）；软限位在 connect 时从固件读出
    （``SL?``/``SR?``），运动命令越界在驱动层先拒绝。
    """

    def __init__(self, ctrl: "ConexCc", unit: int, name: str):
        self._ctrl = ctrl
        self.unit = unit          # SerialTransport 序号（探测顺序）
        self.name = name          # 轴名 "1".."N"
        self.addr = 1             # 串口地址（本驱动固定 1；真菊花链后继扩展）
        self.model = ""           # connect 后由 ID? 填（如 "SR50CC"）
        self.serial_no = ""       # UD 字段
        self.pos_min = -PARAM_LIMIT_MM   # connect 后由 SL? 覆盖
        self.pos_max = PARAM_LIMIT_MM    # connect 后由 SR? 覆盖

    # ------------------------------------------------------------- 帧收发

    def _cmd(self, op: str, value: float | int | str | None = None,
             query: bool = False) -> str:
        return self._ctrl._axis_cmd(self.unit, self.addr, op, value,
                                    query=query)

    # ------------------------------------------------------------- 查询

    @property
    def position(self) -> float:
        """当前位置（mm，``TP``——编码器测量值）。"""
        return self._ctrl._parse_float(self._cmd("TP", query=True))

    def get_position(self) -> float:
        return self.position

    @property
    def is_moving(self) -> bool:
        """运动中（位置连续两次采样有变化，不信任状态位粘滞）。"""
        p1 = self.position
        time.sleep(0.05)
        return abs(self.position - p1) > STABLE_TOL_MM

    def status_register(self) -> int:
        """状态寄存器位掩码（``RS?``，位义见 Command Interface Manual）。"""
        return int(self._ctrl._parse_float(self._cmd("RS?", query=True)))

    def last_error(self) -> str:
        """最近一条命令的错误码（``TE``，``@`` = 无错）。"""
        return self._cmd("TE", query=True).strip()

    def device_info(self) -> str:
        """型号/序列号串（``ID?``，连接后可用）。"""
        try:
            return f"{self.model} {self.serial_no}".strip() or "CONEX-CC"
        except Exception:                              # noqa: BLE001
            return "CONEX-CC"

    def wait_until_still(self, timeout: float = 60.0, poll_s: float = 0.1) -> float:
        """轮询位置直到停稳，返回停稳位置（mm）；超时抛 ConexCcError。"""
        deadline = time.monotonic() + timeout
        last = self.position
        stable = 0
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            cur = self.position
            if abs(cur - last) < STABLE_TOL_MM:
                stable += 1
                if stable >= STABLE_COUNT:
                    return cur
            else:
                stable = 0
            last = cur
        raise ConexCcError(f"等待轴 {self.name} 停稳超时（{timeout:g}s，"
                           f"最后位置 {last:.4f} mm）")

    # ------------------------------------------------------------- 运动控制

    def _check_limit(self, pos: float) -> float:
        pos = float(pos)
        if not self.pos_min - 1e-6 <= pos <= self.pos_max + 1e-6:
            raise ConexCcError(
                f"目标 {pos:g} mm 超出软限位 [{self.pos_min:g}, "
                f"{self.pos_max:g}]，拒绝发送")
        return pos

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        """绝对运动 ``PA``（mm）。``wait=True`` 阻塞到停稳并校验到位。"""
        pos = self._check_limit(pos)
        self._cmd("PA", f"{pos:.4f}")
        if wait:
            settled = self.wait_until_still(timeout=timeout)
            if abs(settled - pos) > 0.01:
                raise ConexCcError(
                    f"绝对运动未到位: 目标 {pos:.4f} mm，实际 {settled:.4f} mm")
            return settled
        return pos

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        """相对运动 ``PR``（mm，有符号）。``wait=True`` 阻塞到停稳。"""
        delta = float(delta)
        target = self._check_limit(self.position + delta)
        self._cmd("PR", f"{delta:+.4f}")
        if wait:
            return self.wait_until_still(timeout=timeout)
        return target

    def home(self, timeout: float = 120.0) -> float:
        """回零（``OR``，真实运动；上电后建议先回零建立坐标系）。"""
        self._cmd("OR")
        settled = self.wait_until_still(timeout=timeout)
        if abs(settled) > 0.1:
            raise ConexCcError(f"Home 后位置异常: {settled:.4f} mm（预期 0±0.1）")
        return settled

    def set_zero(self) -> None:
        """当前点定义为新坐标系 0（``DH0``，不运动）。"""
        self._cmd("DH", 0)

    def stop(self, all_axes: bool = False) -> None:
        """停止运动（``ST``）。"""
        self._cmd("ST")

    def __repr__(self) -> str:
        return f"<ConexCcAxis {self.name}: unit{self.unit} {self.model}>"


class ConexCc:
    """Newport CONEX-CC 控制器组（1~N 台，每台一个 COM 口）。

    Parameters
    ----------
    port : str | None
        串口名（如 ``"COM5"``）。``None`` = connect() 时逐口发 ``1VE``
        探测，应答含 CONEX 即认定（可多口，每口一个轴）。
    transport : SerialTransport | None
        注入传输（无硬件冒烟/测试用，单 unit）。
    """

    def __init__(self, port: str | None = None,
                 transport: SerialTransport | None = None):
        self.port = port
        self._transports: list[SerialTransport] = []
        self._injected = transport
        self.axes: dict[str, ConexCcAxis] = {}
        self._default: ConexCcAxis | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（§4.3）——同一机箱下的两根轴各一条。

        扫描面板靠这两条把平移台与旋转台分开；旋转轴单位 deg，结果视图
        据此画 angle–intensity 极坐标（与 K10CR1 同一判据）。
        """
        return (
            ParamDesc(key="position", ptype="float",
                      label="Linear stage (mm)", unit="mm",
                      apply="write", vmin=-PARAM_LIMIT_MM,
                      vmax=PARAM_LIMIT_MM, access="rw", decimals=4,
                      settle_ms=0, setter="set_position",
                      getter="get_position"),
            ParamDesc(key="angle", ptype="float",
                      label="Rotary stage (deg)", unit="deg",
                      apply="write", vmin=0.0, vmax=360.0, access="rw",
                      decimals=3, settle_ms=200, setter="set_angle",
                      getter="get_angle"),
        )

    # ------------------------------------------------------------- 生命周期

    def connect(self, port: str | None = None) -> None:
        """探测/打开串口并联络（``1VE``），读型号与软限位，铺轴表。

        自动模式逐个候选口发 ``1VE``（应答含 CONEX 即认定）。
        """
        if self._transports:
            return
        if self._injected is not None:      # 测试注入路径（单 unit）
            if not self._injected.is_open:
                self._injected.open()
            self._adopt([self._injected])
            return
        port = port or self.port
        candidates = [port] if port else [
            p.device for p in sorted(list_ports.comports(), key=lambda p: p.device)]
        found: list[SerialTransport] = []
        for cand in candidates:
            tp = SerialTransport(cand, BAUDRATE, read_timeout_s=PROBE_TIMEOUT_S,
                                 write_timeout_s=PROBE_TIMEOUT_S)
            try:
                tp.open()
            except serial.SerialException:
                # FTDI 口刚被上一进程关闭时需短暂释放：退避重开一次
                time.sleep(0.5)
                try:
                    tp.open()
                except serial.SerialException:
                    continue
            ver = ""
            for attempt in range(3):    # 上电后/久置后首帧偶发丢失：重试
                try:
                    ver = self._ask(tp, 1, "VE")
                except (ConexCcError, TimeoutError):
                    ver = ""
                if IDN_TOKEN in ver.upper():
                    break
                time.sleep(0.1)
            if IDN_TOKEN in ver.upper():
                tp.close()                  # 探测口超时短，换正式超时重开
                tp = SerialTransport(cand, BAUDRATE)
                tp.open()
                found.append(tp)
            else:
                tp.close()
        if not found:
            raise ConexCcError(
                "未找到 CONEX-CC 控制器（921600 8N1，1VE 无 CONEX 应答）。"
                f"已尝试: {', '.join(candidates) or '（无可用串口）'}；"
                "若 Newport CONEX-CC GUI 正在运行请先关闭（串口独占）")
        self.port = found[0].port
        self._adopt(found)

    def _adopt(self, transports) -> None:
        self._transports = list(transports)
        self.axes = {}
        for u, tp in enumerate(self._transports):
            # 一个串口上可能菊花链多台（地址 1..N）：逐地址问 1VE，应答含
            # CONEX 才算一台。只问地址 1 会漏掉链上的后面几台（PR50CC 实测就是）。
            for addr in range(1, MAX_ADDR_PER_PORT + 1):
                try:
                    reply = self._ask(
                        tp, addr, "VE",
                        timeout_s=2.0 if addr == 1 else PROBE_TIMEOUT_S)
                except (ConexCcError, TimeoutError):
                    continue
                if IDN_TOKEN not in reply.upper():
                    continue
                name = str(len(self.axes) + 1)
                ax = ConexCcAxis(self, u, name)
                ax.addr = addr
                # 型号 / 序列号（ID? → "1ID<MODEL>_PN:…_UD:<SN>"）
                try:
                    idn = ax._cmd("ID?", query=True)
                    ax.model = idn.split("_")[0].replace("ID", "", 1).strip()
                    m = re.search(r"_UD:(\S+)", idn)
                    if m:
                        ax.serial_no = m.group(1)
                except (ConexCcError, TimeoutError):
                    pass
                # 固件软限位（SL?/SR?，mm 或 deg）
                try:
                    ax.pos_min = self._parse_float(ax._cmd("SL?", query=True))
                    ax.pos_max = self._parse_float(ax._cmd("SR?", query=True))
                except (ConexCcError, TimeoutError):
                    pass
                self.axes[name] = ax
        self._default = self.axes.get("1")
        logger.info("CONEX 轴表：%s", ", ".join(
            f"{n}:{a.model or '?'}@{a.addr} [{a.pos_min:g},{a.pos_max:g}]"
            for n, a in self.axes.items()))

    def disconnect(self) -> None:
        for tp in self._transports:
            try:
                tp.close()
            finally:
                pass
        self._transports = []
        self.axes = {}
        self._default = None

    def __enter__(self) -> "ConexCc":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------- 帧收发

    def _ask(self, tp: SerialTransport, addr: int, body: str,
             timeout_s: float = 2.0) -> str:
        """发查询帧并收一帧应答（应答 = "地址+命令字+返回值"）。"""
        with self._lock:
            frame = f"{addr}{body}"
            try:
                raw = tp.query_frame(frame.encode("ascii") + WRITE_END.encode(),
                                     REPLY_END, timeout_s=timeout_s)
            except TimeoutError as exc:
                raise ConexCcError(f"等待应答超时: {frame!r}") from exc
            return raw.decode("ascii", errors="replace").strip("\r\n\x00 ")

    def _axis_cmd(self, unit: int, addr: int, op: str,
                  value: float | int | str | None = None,
                  query: bool = False) -> str:
        """拼轴帧 ``addrOP[value]``（``?`` 由调用方写进 op，如 "ID?"/"TP"）。

        运动命令（PA/PR/OR/ST）实测不即回帧（见模块文档）：等满 2 s 只会白等，
        且迟到帧会污染下一次查询。所以非查询只等 50 ms 就清缓冲返回；查询必须
        收到**本条命令的回显**，否则当迟到帧丢弃重问（最多 3 次）。
        """
        body = op + ("" if value is None else str(value))
        tp = self._transports[unit]
        echo = f"{addr}{op.rstrip('?')}"
        frame = f"{addr}{body}"
        reply = ""
        for attempt in range(3):
            if attempt:
                time.sleep(0.05)
                tp.drain()              # 清掉迟到的旧应答，防错位
            try:
                reply = self._ask(
                    tp, addr, body,
                    timeout_s=2.0 if query else MOTION_REPLY_WAIT_S)
            except ConexCcError:
                if not query:
                    tp.drain()
                    return ""           # 运动命令无即时应答是常态
                if attempt == 2:
                    raise
                continue
            if not query:
                tp.drain()              # 运动命令的（迟到）回答不参与解析
                return ""
            if reply.startswith(echo) or reply.startswith(f"{addr}@" + op):
                break                   # 确是本条查询的应答
            reply = ""                  # 别的命令的迟到帧：丢掉重问
        if query and not reply:
            raise ConexCcError(f"应答始终与命令不匹配（{echo}）")
        if reply.startswith(echo):
            reply = reply[len(echo):]
        elif reply.startswith(f"{addr}@" + op):
            reply = reply[len(f"{addr}@{op}"):]
        reply = reply.strip()
        # 错误码帧（如 "1TE@" 之外的 "@xx" 前缀）按控制器报错处理；
        # TE/RS 等本身返回错误码字符串的命令放行
        if reply.startswith("@") and op not in ("TE", "RS"):
            raise ConexCcError(f"控制器报错 {reply!r}（{frame}）")
        return reply

    @staticmethod
    def _parse_float(reply: str) -> float:
        m = _FLOAT_RE.search(reply)
        if m is None:
            raise ConexCcError(f"应答无法解析为数值: {reply!r}")
        return float(m.group())

    # ------------------------------------------------------------- 轴访问

    def axis(self, name: str) -> ConexCcAxis:
        """按名取轴（``cc.axis("2")``）。"""
        ax = self.axes.get(str(name))
        if ax is None:
            raise KeyError(f"无此轴: {name!r}（可用: {sorted(self.axes)}）")
        return ax

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.axes, key=int))

    def version(self, unit: int = 0) -> str:
        """固件版本串（``1VE``，如 "CONEX-CC 2.0.1"）。"""
        return self._ask(self._transports[unit], 1, "VE")

    # --------------------------------------------- MotorAxis 合同（默认轴委托）

    @property
    def position(self) -> float:
        """默认轴（第 1 台）位置（mm）——GUI/CLI 通用读服务走这里。"""
        return self._default.position

    @property
    def is_moving(self) -> bool:
        return self._default.is_moving

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        """默认轴绝对运动（参数目录 position 的写侧，经合同自动推导）。"""
        return self._default.move_to(pos, wait=wait, timeout=timeout)

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        return self._default.move_by(delta, wait=wait, timeout=timeout)

    def stop(self, all_axes: bool = False) -> None:
        self._default.stop(all_axes=all_axes)

    # --------------------------- 按角色寻址（扫描目录 position/angle 的读写侧）

    def _axis_by_role(self, role: str) -> "ConexCcAxis":
        """按型号挑轴：PR/SR/RV 开头 = 旋转台，其余 = 平移台。

        找不到对应型号直接报错，**不做顺序兜底**：把直线轴当旋转轴驱动会让
        扫描假装在走角度（2026-10-21 真机：MFA-CC 被当成 PR50CC，位置永远 0）。
        """
        if not self.axes:
            raise ConexCcError("未连接：轴表为空")
        axes = [self.axes[n] for n in self.axis_names]
        models = ", ".join(f"{a.name}:{a.model or '?'}" for a in axes)
        rotary = [a for a in axes
                  if a.model.upper().startswith(ROTARY_PREFIXES)]
        if role == "rotary":
            if rotary:
                return rotary[0]
            raise ConexCcError(f"没有旋转轴（当前轴: {models}）")
        linear = [a for a in axes if a not in rotary]
        if linear:
            return linear[0]
        raise ConexCcError(f"没有平移轴（当前轴: {models}）")

    def get_position(self) -> float:
        """平移台位置（mm）——目录 position 的读侧。"""
        return self._axis_by_role("linear").position

    def set_position(self, mm: float):
        """平移台绝对运动（非阻塞：返回 WriteHandle，引擎轮询到位）。"""
        return _motion_handle(self._axis_by_role("linear"), float(mm),
                              ARRIVE_TOL_MM, STABLE_TOL_MM)

    def get_angle(self) -> float:
        """旋转台角度（deg）——目录 angle 的读侧。"""
        return self._axis_by_role("rotary").position

    def set_angle(self, deg: float):
        """旋转台绝对运动（非阻塞：返回 WriteHandle，引擎轮询到位）。"""
        return _motion_handle(self._axis_by_role("rotary"), float(deg),
                              ARRIVE_TOL_DEG, STABLE_TOL_DEG)

    # ------------------------------------------------- 按名轴控制（GUI 页用）

    def axis_position(self, name: str) -> float:
        """指定轴位置回读（mm）。"""
        return self.axis(name).position

    def axis_move_to(self, name: str, pos: float, wait: bool = True,
                     timeout: float = 60.0) -> float:
        """指定轴绝对运动。"""
        return self.axis(name).move_to(pos, wait=wait, timeout=timeout)

    def axis_move_by(self, name: str, delta: float, wait: bool = True,
                     timeout: float = 60.0) -> float:
        """指定轴相对运动。"""
        return self.axis(name).move_by(delta, wait=wait, timeout=timeout)

    def axis_home(self, name: str, timeout: float = 120.0) -> float:
        """指定轴回零（真实运动）。"""
        return self.axis(name).home(timeout=timeout)

    def axis_stop(self, name: str) -> None:
        """停止指定轴运动。"""
        self.axis(name).stop()

    def axis_info(self, name: str) -> dict:
        """指定轴铭牌（型号/序列号/软限位），连接后可用。"""
        ax = self.axis(name)
        return {"model": ax.model, "serial": ax.serial_no,
                "pos_min": ax.pos_min, "pos_max": ax.pos_max}


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="conex_cc", dev_id=Dev.CONEX, entry="ConexCc",
    display_name="Conex Stage", model="Newport CONEX-CC",
    capabilities=("motor_axis",), default_transport="serial",
    page="labcontrol.gui.pages.conex_page",
    connect_timeout_ms=30000, disconnect_timeout_ms=15000)
