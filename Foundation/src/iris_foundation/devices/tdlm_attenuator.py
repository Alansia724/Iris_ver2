"""SC 系列步进电机控制器（三轴）—— 电动连续衰减片设备接口层（RS-232 串口）。

纯 Python + pyserial，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

协议来源：《SC 系列步进电机控制器说明书》(2019.11) 3.4 串口指令（官方手册，
桌面 PDF）+ 2026-08-19 真机（COM1）逐条实测校准。串口 **9600, N, 8, 1** 无流控；
命令帧 ASCII 仅以 "/" 结尾（不加 CRLF）；应答帧以 "\\x10"（CHR$(10)，DC1）结尾，
成功回 "OK" 或 "XX=number"，失败回 "ERRxx" 或 "NC"（非联机）：

==================  ==========================  =============================
用途                主机 → 控制器               控制器 → 主机（真机实测）
==================  ==========================  =============================
联络（握手）        ``?R/``                     ``OK``（非联机回 ``NC``）
坐标查询            ``?X/`` (``?Y/`` ``?Z/``)   ``X=+123``（有符号整数脉冲）
运行状态            ``?ST/``                    ``ST=05`` 位掩码：bit0=X
                                                bit1=Y bit2=Z 运行中
绝对运动            ``X=400/``                  ``OK``（原生绝对，真机验证：
                                                连发同值位置不变）
相对运动            ``X:400/``                  ``OK``（终点=当前+N）
带补偿绝对运动       ``X;400/``                  ``OK`` 但本机不动作（定制
                                                功能，需 TX=C；实测无效）
闭环运动            ``X_400/``                  ``OK``（SC300 系列）
停止                ``S0/`` 全部 / ``S0X/`` X轴  ``OK``
归零运动            ``XH/`` / ``-XH/``          ``OK``（走到零位开关）
坐标清零（不运动）   ``HMX/``                    ``OK``（当前点设为原点）
速度设置/查询        ``VX=1000/`` / ``?VX/``     ``OK`` / ``VX=1000``（Hz 档）
回零速度             ``HVX=N/`` / ``?HVX/``      同上
加速度(3参)          ``AX=2,1,5/`` / ``?AX/``    ``OK`` / ``AX=2,1,5``
软限位              ``LX=n1,n2/`` / ``?LX/``    ``OK`` / ``LX=-n,+n``
细分                ``FX=N/`` / ``?FX/``        ``OK`` / ``FX=8``
导程/传动比          ``DX=N/`` / ``?DX/``        ``OK`` / ``DX=4000``
位移台类型           ``TX=D//C/`` / ``?TX/``     ``OK`` / ``TX=D``平移``C``旋转
限位状态            ``?LM/``                    ``LM=F006``（位掩码）
参数存储/载入        ``SAVE/`` / ``LOAD/``       ``OK``
==================  ==========================  =============================

错误码（手册表 3-1）：ERR01 未知指令、ERR02 数据无效、ERR03 指令功能禁止
（含运动中发运动指令，驱动内等待重试）、ERR04 系统被急停、ERR05 碰软限位、
ERR06~14 各轴硬限位/零位停止、ERR15/16 硬限位/零位故障。

单位换算：固件单位是**脉冲**，机械类型决定换算（构造参数 ``unit``）：

* ``"pulse"``（**默认**）：原始脉冲整数，所见即所得（无量化误差与未标定
  假设），范围 ±16777215（固件计数极限，`?LX/` 实测）；另有
  ``position_pulses``/``move_by_pulses``（任意单位模式下可用）；
* ``"deg"``：每圈脉冲数 = 200 全步 × 细分数，细分在 connect() 时自动读
  ``?FX/`` 自校准（本机实测 8 → 1600 脉冲/圈，1 脉冲 = 0.225°）。
  **假设电机直连转盘**；若有减速比，用 ``pulses_per_rev=`` 修正
 （发 360° 看转盘是否整圈即可验证）；
* ``"mm"``（直线轴）：``PULSES_PER_MM = 400``（para.reg/``?DX/`` 导程 4 mm ×
  400 脉冲/mm = 1600 脉冲/圈，自洽），1 脉冲 = 2.5 µm。

用法::

    from iris_foundation.devices.tdlm_attenuator import TdlmAttenuator
    with TdlmAttenuator() as att:            # 默认脉冲模式（±16777215 整数）
        att.position                         # 实际位置回读（脉冲整数）
        att.is_moving                        # ?ST 位掩码按轴解析
        att.move_to(400, wait=True)          # 绝对运动 X=400/
        att.move_by(-10)                     # 相对运动 X:-10/
        att.set_speed(2000)                  # VX=2000/（速度可线上修改）
        att.set_zero()                       # HMX/ 当前点设为原点（不运动）
        att.stop()                           # S0X/ 停本轴
    # 角度模式: TdlmAttenuator(unit="deg")；直线轴: unit="mm"
"""

from __future__ import annotations

import threading
import time

import serial
from serial.tools import list_ports

from iris_foundation.core.contract import DriverManifest, ParamDesc
from iris_foundation.core.topics import Dev
from iris_foundation.devices.transports import SerialTransport

# 上位机内嵌的串口参数（TDLMON.EXE .data 段 "9600,n,8,1"，手册 2.2 同）
BAUDRATE = 9600

# 应答帧终止符（手册各指令"回送 …&CHR$(10)"；真机实测 OK\x10 / ST=00\x10）
REPLY_END = b"\x10"

# 手册表 3-1 错误码
ERR_MEANINGS = {
    "ERR01": "未知指令",
    "ERR02": "数据无效",
    "ERR03": "指令功能禁止（如运动中发运动指令）",
    "ERR04": "系统被急停",
    "ERR05": "碰软限位停止",
    "ERR06": "到 X 轴负硬限位停止",
    "ERR07": "到 X 轴正硬限位停止",
    "ERR08": "到 X 轴零位停止",
    "ERR09": "到 Y 轴负硬限位停止",
    "ERR10": "到 Y 轴正硬限位停止",
    "ERR11": "到 Y 轴零位停止",
    "ERR12": "到 Z 轴负硬限位停止",
    "ERR13": "到 Z 轴正硬限位停止",
    "ERR14": "到 Z 轴零位停止",
    "ERR15": "硬限位有故障",
    "ERR16": "硬件零位故障",
}

# 直线轴换算：para.reg / ?DX/ 导程 4 mm，软限位 ±41943.038 mm × 400 = 2^24-1 脉冲
PULSES_PER_MM = 400.0

# 旋转轴基准：200 全步/圈；每圈脉冲 = 200 × 细分（connect 时 ?FX/ 实读校准）
FULL_STEPS_PER_REV = 200
PULSES_PER_REV_DEFAULT = 1600.0     # 200 × 8（本机实测细分，未连接时的初值）

# 固件计数极限（?LX/ 软限位反推）
PULSES_MAX = 16777215  # 2^24 - 1

# 本机实测安全区：转轴每圈有一个零位光电开关（1600 脉冲/圈），
# 正向在 +1534、负向在 -50 处触发——越过时固件截停运动并清零坐标
# （ERR08 动作，命令仍回 OK）。默认把行程限在一圈内避开开关。
SAFE_MIN_PULSES = -50
SAFE_MAX_PULSES = 1534

_AXIS_PREFIX = {"x": "X", "y": "Y", "z": "Z"}
_AXIS_BIT = {"x": 0, "y": 1, "z": 2}     # ?ST 运行状态位（手册表 3-6）

_UNIT_ALIASES = {"deg": ("deg", "°"), "degree": ("deg", "°"),
                 "mm": ("mm", "mm"),
                 "pulse": ("pulse", "pulse"), "pulses": ("pulse", "pulse"),
                 "raw": ("pulse", "pulse")}


class TdlmAttenuatorError(IOError):
    """SC 控制器通信失败 / 指令被拒。"""


class TdlmAttenuator:
    """电动连续衰减片（SC 三轴控制器的一根轴，默认 X）。

    Parameters
    ----------
    port : str | None
        串口名（如 ``"COM1"``）。``None`` = connect() 时自动探测：逐口以
        9600/N/8/1 发 ``?R/`` 联络，应答 ``OK`` 即认定。
    axis : str
        衰减片所在轴，``"x"`` / ``"y"`` / ``"z"``。
    unit : str
        对外单位：``"pulse"``（原始脉冲整数，**默认**——所见即所得，无量化
        误差与未标定假设，范围 ±16777215）/ ``"deg"``（角度，200 全步 ×
        细分换算）/ ``"mm"``（直线轴）。
    pulses_per_rev : float | None
        每圈脉冲数。``None`` = connect() 时按 ``200 × ?FX/``（细分）自动
        计算；固定值用于强制覆盖（如有减速比，= 电机每圈脉冲 × 减速比）。
    travel_limit : float | None
        软限位（当前单位，对称），超出拒绝发送。``None`` = 固件计数极限
        （±2²⁴−1 脉冲）换算后的值。
    transport : SerialTransport | None
        注入传输（无硬件冒烟/测试用）。``None`` = connect() 时按 port 自动
        开串口（docs/architecture.md §7：字节层在 Transport，帧语义在本模块）。
    """

    def __init__(self, port: str | None = None, axis: str = "x",
                 unit: str = "pulse", pulses_per_rev: float | None = None,
                 travel_limit: float | None = None,
                 transport: SerialTransport | None = None):
        if axis.lower() not in _AXIS_PREFIX:
            raise ValueError(f"axis 必须是 x/y/z: {axis!r}")
        if unit.lower() not in _UNIT_ALIASES:
            raise ValueError(f"unit 必须是 deg/mm/pulse: {unit!r}")
        self.port = port
        self.axis = axis.lower()
        self.unit, self.unit_label = _UNIT_ALIASES[unit.lower()]
        self.microstep: int | None = None       # connect 时 ?FX/ 读到
        self.pulses_per_rev: float | None = float(pulses_per_rev) \
            if pulses_per_rev is not None else None
        self._apply_unit_scale()
        if travel_limit is not None:
            self.travel_limit = float(travel_limit)
        elif self.unit == "pulse":
            # 脉冲模式默认用实测安全区（一圈内，避开零位开关）
            self.travel_limit = float(SAFE_MAX_PULSES)
            self._travel_min = float(SAFE_MIN_PULSES)
        else:
            self.travel_limit = PULSES_MAX / self.unit_scale
            self._travel_min = -self.travel_limit
        self._transport: SerialTransport | None = transport
        self._lock = threading.Lock()

    def _apply_unit_scale(self) -> None:
        """按 unit 与 pulses_per_rev 设定 脉冲/单位 换算系数。"""
        if self.unit == "deg":
            ppr = self.pulses_per_rev or PULSES_PER_REV_DEFAULT
            self.unit_scale = ppr / 360.0
        elif self.unit == "mm":
            self.unit_scale = PULSES_PER_MM
        else:
            self.unit_scale = 1.0

    # ------------------------------------------------------------- 合同声明

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明（docs/architecture.md §4.3）——默认脉冲模式安全区。

        量程直接引用 SAFE_MIN/MAX_PULSES：参数目录与驱动从此单一事实来源
        （原 core/params.py 手抄的 [-50, 1534] 已随 P2 删除）。
        """
        return (ParamDesc(
            key="position", ptype="int", label="Position (pulse)", unit="pulse",
            apply="write", vmin=float(SAFE_MIN_PULSES), vmax=float(SAFE_MAX_PULSES),
            access="rw", decimals=0, settle_ms=2000),)

    # ------------------------------------------------------------- 生命周期

    def connect(self, port: str | None = None) -> None:
        """打开串口并联络（?R/ → OK，手册 3.4.3.1-1）。无应答的口自动跳过。"""
        if self._transport is not None:
            if not self._transport.is_open:
                self._transport.open()
            self._greet(self._transport)
            return
        port = port or self.port
        candidates = [port] if port else [p.device for p in list_ports.comports()]
        last_err = ""
        for cand in candidates:
            try:
                tp = SerialTransport(cand, BAUDRATE)
                tp.open()
            except serial.SerialException as exc:
                last_err = f"{cand}: {exc}"
                continue
            try:
                self._greet(tp)
                return
            except TdlmAttenuatorError as exc:
                last_err = f"{cand}: {exc}"
                continue
        raise TdlmAttenuatorError(
            "未找到 SC 步进电机控制器（9600,N,8,1，?R/ 联络无 OK 应答）。"
            f"已尝试: {', '.join(candidates) or '（无可用串口）'}"
            + (f"；最后错误 {last_err}" if last_err else ""))

    def _greet(self, tp: SerialTransport) -> None:
        """联络（?R/ → OK）并读轴参数；成功才绑定 self._transport/port。

        失败时关闭并解绑传输，供 connect() 换下一个候选口重试。
        """
        self._transport = tp
        try:
            tp.drain()
            reply = self._cmd("?R/")
            if reply != "OK":
                raise TdlmAttenuatorError(f"联络应答异常 {reply!r}")
        except TdlmAttenuatorError:
            tp.close()
            self._transport = None
            raise
        self.port = tp.port
        self._read_profile()

    def _read_profile(self) -> None:
        """连接后读轴参数（细分自动校准角度换算；其余仅记录）。"""
        try:
            self.microstep = self.get_microstep()
            if self.unit == "deg" and self.pulses_per_rev is None:
                self.pulses_per_rev = FULL_STEPS_PER_REV * self.microstep
                self._apply_unit_scale()
                self.travel_limit = PULSES_MAX / self.unit_scale
        except TdlmAttenuatorError:
            self.microstep = None      # 查询失败不阻断连接（换算用默认值）

    def disconnect(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            finally:
                self._transport = None

    def __enter__(self) -> "TdlmAttenuator":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # ------------------------------------------------------------- 帧收发（字节层在 Transport，§7）

    def _cmd(self, frame: str, timeout: float = 1.0) -> str:
        """发送一帧（frame 需自带 '/' 终止符，不加 CRLF）并等一帧应答。

        应答以 ``\\x10`` 分隔；``OK``=确认，``ERRxx``=拒绝（抛错并附手册含义），
        ``NC``=非联机（抛错），其余（``ST=..``/``X=..``）原样返回。
        """
        with self._lock:
            if self._transport is None:
                raise TdlmAttenuatorError("串口未打开（先 connect）")
            try:
                raw = self._transport.query_frame(frame.encode("ascii"), REPLY_END,
                                                  timeout_s=timeout)
            except TimeoutError as exc:
                raise TdlmAttenuatorError(f"等待应答超时: {frame!r}") from exc
            reply = raw.decode("ascii", errors="replace").strip("\r\n\x00 ")
            if reply.startswith("ERR"):
                meaning = ERR_MEANINGS.get(reply[:5], "未知错误")
                raise TdlmAttenuatorError(
                    f"控制器拒绝指令 {frame!r}: {reply}（{meaning}）")
            if reply == "NC":
                raise TdlmAttenuatorError(
                    f"控制器非联机状态（{frame!r} 回 NC）：检查串口或手动/联机状态")
            return reply

    def _axis_cmd(self, op: str, pulses: int | None = None,
                  retry_on_busy: bool = False) -> str:
        """拼轴命令帧：轴前缀 + 操作符 + 有符号整数 + '/'。

        ``retry_on_busy=True``（运动指令）：运动中会收 ERR03（指令功能禁止），
        等待空闲后重试一次。
        """
        frame = _AXIS_PREFIX[self.axis] + op
        if pulses is not None:
            frame += str(int(pulses))   # 不加显式 '+'（手册：正号一般不写）
        try:
            return self._cmd(frame + "/")
        except TdlmAttenuatorError as exc:
            if not retry_on_busy or "ERR03" not in str(exc):
                raise
            self.wait_until_still()
            return self._cmd(frame + "/")

    @staticmethod
    def _parse_value(reply: str, prefix: str) -> str:
        """"X=+123" → "+123"；不匹配则抛错。"""
        if not reply.startswith(prefix):
            raise TdlmAttenuatorError(f"应答格式异常: {reply!r}（预期 {prefix}…）")
        return reply[len(prefix):]

    # ------------------------------------------------------------- 查询

    @property
    def position(self) -> float:
        """实际位置（当前单位）——固件回读 ``?X/``，非指令记账。"""
        return self._position_raw() / self.unit_scale

    @property
    def position_pulses(self) -> int:
        """原始脉冲位置（标定用，不受 unit 影响）。"""
        return self._position_raw()

    def _position_raw(self) -> int:
        reply = self._cmd(f"?{_AXIS_PREFIX[self.axis]}/")
        try:
            return int(self._parse_value(reply, f"{_AXIS_PREFIX[self.axis]}="))
        except ValueError:
            raise TdlmAttenuatorError(f"位置应答无法解析: {reply!r}") from None

    def get_position(self) -> float:
        return self.position

    def status_raw(self) -> int:
        """``?ST/`` 运行状态字（手册表 3-6 位掩码 bit0=X bit1=Y bit2=Z）。"""
        reply = self._cmd("?ST/")
        try:
            return int(self._parse_value(reply, "ST="), 16)
        except ValueError:
            raise TdlmAttenuatorError(f"状态应答无法解析: {reply!r}") from None

    @property
    def is_moving(self) -> bool:
        """本轴运行状态位（按轴解析 ``?ST`` 位掩码）。"""
        return bool((self.status_raw() >> _AXIS_BIT[self.axis]) & 1)

    def status_query(self) -> str:
        """状态查询 ``?ST/``，返回应答原文（如 ``ST=01``）。"""
        return self._cmd("?ST/")

    def wait_until_still(self, timeout: float = 60.0, poll_s: float = 0.1) -> None:
        """轮询本轴 ``?ST`` 位直到空闲；超时抛错（ERR03 重试的前提）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_moving:
                return
            time.sleep(poll_s)
        raise TdlmAttenuatorError(f"等待运动完成超时（{timeout:g}s）")

    # ---- 参数查询（手册 3.4.3.1）----

    def get_microstep(self) -> int:
        """细分（``?FX/``，本机实测 8）。"""
        return int(self._parse_value(self._cmd(f"?F{_AXIS_PREFIX[self.axis]}/"),
                                     f"F{_AXIS_PREFIX[self.axis]}="))

    def get_speed(self) -> int:
        """运行速度档 Hz（``?VX/``，范围 10~50000，本机 1000）。"""
        return int(self._parse_value(self._cmd(f"?V{_AXIS_PREFIX[self.axis]}/"),
                                     f"V{_AXIS_PREFIX[self.axis]}="))

    def get_home_speed(self) -> int:
        """回零速度档 Hz（``?HVX/``）。"""
        return int(self._parse_value(self._cmd(f"?HV{_AXIS_PREFIX[self.axis]}/"),
                                     f"HV{_AXIS_PREFIX[self.axis]}="))

    def get_acceleration(self) -> tuple[int, int, int]:
        """加速度三参数（``?AX/`` → "2,1,5"；1、3 范围 1~255，2 范围 1~90）。"""
        raw = self._parse_value(self._cmd(f"?A{_AXIS_PREFIX[self.axis]}/"),
                                f"A{_AXIS_PREFIX[self.axis]}=")
        try:
            return tuple(int(v) for v in raw.replace("，", ",").split(","))
        except ValueError:
            raise TdlmAttenuatorError(f"加速度应答无法解析: {raw!r}") from None

    def get_soft_limit(self) -> tuple[int, int]:
        """软限位（``?LX/`` → (负限位, 正限位)，均 0 表示无软限位）。"""
        raw = self._parse_value(self._cmd(f"?L{_AXIS_PREFIX[self.axis]}/"),
                                f"L{_AXIS_PREFIX[self.axis]}=")
        try:
            return tuple(int(v) for v in raw.replace("，", ",").split(","))
        except ValueError:
            raise TdlmAttenuatorError(f"软限位应答无法解析: {raw!r}") from None

    def get_lead(self) -> int:
        """导程/传动比（``?DX/``；平移台=导程×1000(µm)，旋转台=传动比）。"""
        return int(self._parse_value(self._cmd(f"?D{_AXIS_PREFIX[self.axis]}/"),
                                     f"D{_AXIS_PREFIX[self.axis]}="))

    def get_stage_type(self) -> str:
        """位移台类型（``?TX/`` → 'D' 平移台 / 'C' 旋转台）。"""
        return self._parse_value(self._cmd(f"?T{_AXIS_PREFIX[self.axis]}/"),
                                 f"T{_AXIS_PREFIX[self.axis]}=")

    def get_limit_status(self) -> str:
        """限位状态字（``?LM/``，位义见手册表 3-5）。"""
        return self._parse_value(self._cmd("?LM/"), "LM=")

    # ------------------------------------------------------------- 运动控制

    def _check_travel(self, pos: float) -> float:
        pos = float(pos)
        if not (self._travel_min <= pos <= self.travel_limit):
            raise TdlmAttenuatorError(
                f"位置 {pos:g} {self.unit_label} 超出安全范围 "
                f"[{self._travel_min:g}, {self.travel_limit:g}] {self.unit_label}"
                "（一圈以内，越界会被零位开关截停并清零），拒绝发送")
        return pos

    def _to_pulses(self, value: float) -> int:
        pulses = int(round(float(value) * self.unit_scale))
        if pulses == 0 and abs(value) > 1e-12:
            raise TdlmAttenuatorError(
                f"{value:g} {self.unit_label} 不足 1 脉冲"
                f"（1 脉冲 = {1 / self.unit_scale:g} {self.unit_label}），"
                "量化后为 0，已拒绝")
        return pulses

    def move_to(self, pos: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        """绝对运动 ``X=N/``（手册 3.4.3.3-5，原生绝对，真机验证）。

        注：手册 3.4.3.3-6 的带精度补偿绝对运动 ``X;N/`` 在本机**不可用**
 （2026-08-19 实测：回 OK 但电机不动）——该功能为出厂定制项且需
 “位移台类型”配为旋转台（本机 ``?TX/``=D）；本机亦无闭环（SC1xx/2xx）。

        ``wait=True`` 轮询本轴 ``?ST`` 位到空闲。运动中 ERR03 自动等待重发。
        """
        pos = self._check_travel(pos)
        self._axis_cmd("=", self._to_pulses(pos), retry_on_busy=True)
        if wait:
            self.wait_until_still(timeout=timeout)
        return pos

    def move_by(self, delta: float, wait: bool = False,
                timeout: float = 60.0) -> float:
        """相对运动 ``X:N/``（手册 3.4.3.3-4，终点=当前+N）。"""
        delta = float(delta)
        self._check_travel(self.position + delta)
        self._axis_cmd(":", self._to_pulses(delta), retry_on_busy=True)
        if wait:
            self.wait_until_still(timeout=timeout)
        return self.position

    def move_by_pulses(self, pulses: int, wait: bool = False,
                       timeout: float = 60.0) -> int:
        """相对运动 N 个原始脉冲（标定用：验证每圈脉冲数/减速比）。"""
        self._axis_cmd(":", int(pulses), retry_on_busy=True)
        if wait:
            self.wait_until_still(timeout=timeout)
        return self._position_raw()

    def home(self, direction: int = 1) -> None:
        """归零运动 ``XH/`` / ``-XH/``（手册 3.4.3.3-1，走到零位开关）。

        ⚠ 未在真机实测（电机会走到零位开关），首次使用先确认行程安全。
        """
        if direction not in (1, -1):
            raise ValueError("direction 必须是 1（正向）或 -1（反向）")
        self._axis_cmd("H" if direction == 1 else "-H", retry_on_busy=True)

    def set_zero(self) -> None:
        """坐标清零 ``HMX/``（手册 3.4.3.3-2）：当前点设为原点，**不运动**。"""
        self._axis_cmd("M")

    def stop(self, all_axes: bool = False) -> None:
        """停止 ``S0X/``（本轴）或 ``S0/``（全部轴，手册 3.4.3.3-9）。"""
        if all_axes:
            self._cmd("S0/")
        else:
            self._cmd(f"S0{_AXIS_PREFIX[self.axis]}/")

    # ------------------------------------------------------------- 参数设置（手册 3.4.3.2）

    def set_speed(self, hz: int) -> int:
        """运行速度 ``VX=N/``（Hz 档 10~50000；注意 V 在轴字母前）。"""
        hz = int(hz)
        if not 10 <= hz <= 50000:
            raise ValueError(f"速度范围 10~50000 Hz: {hz}")
        self._cmd(f"V{_AXIS_PREFIX[self.axis]}={hz}/")
        return hz

    def set_home_speed(self, hz: int) -> int:
        """回零速度 ``HVX=N/``（Hz 档 10~50000）。"""
        hz = int(hz)
        if not 10 <= hz <= 50000:
            raise ValueError(f"回零速度范围 10~50000 Hz: {hz}")
        self._cmd(f"HV{_AXIS_PREFIX[self.axis]}={hz}/")
        return hz

    def set_acceleration(self, p1: int, p2: int, p3: int) -> None:
        """加速度 ``AX=p1,p2,p3/``（p1、p3 范围 1~255，p2 范围 1~90）。"""
        if not (1 <= p1 <= 255 and 1 <= p2 <= 90 and 1 <= p3 <= 255):
            raise ValueError(f"加速度参数范围 1~255,1~90,1~255: {p1},{p2},{p3}")
        self._cmd(f"A{_AXIS_PREFIX[self.axis]}={p1},{p2},{p3}/")

    def set_soft_limit(self, neg_pulses: int, pos_pulses: int) -> None:
        """软限位 ``LX=n1,n2/``（脉冲；均 0 = 取消软限位）。"""
        self._cmd(f"L{_AXIS_PREFIX[self.axis]}={int(neg_pulses)},{int(pos_pulses)}/")

    def save_params(self) -> None:
        """参数存储 ``SAVE/``（写 EEPROM；手册 3.4.3.2-11，断电不丢失）。"""
        self._cmd("SAVE/")

    def load_params(self) -> None:
        """参数载入 ``LOAD/``（恢复上电初始值；手册 3.4.3.2-12）。"""
        self._cmd("LOAD/")


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="tdlm_attenuator", dev_id=Dev.ATTENUATOR, entry="TdlmAttenuator",
    display_name="Attenuator", model="TDLMON 3-axis X",
    capabilities=("motor_axis",), default_transport="serial",
    page="labcontrol.gui.pages.attenuator_page",
    connect_timeout_ms=30000, disconnect_timeout_ms=15000)
