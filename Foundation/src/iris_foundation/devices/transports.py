"""共享传输层（docs/architecture.md §7）：字节/帧往返，不含协议语义。

Transport 是普通类：追求**可替换**（换口/换波特率/换 VISA）而非可插拔。
协议语义（指令集 ↔ 合同 ParamDesc/能力接口）留在各驱动模块——
同一个 SerialTransport 可服务任何"ASCII 帧 + 终止符"的串口设备。

P2 落 SerialTransport（TDLMON 试用）；P4 样式件 VisaTransport（懒加载
pyvisa，环境未装不影响其他驱动）；NativeSdk（ctypes DLL）/ Gentl 随
后续设备迁入。SCPI 驱动模板见 templates/scpi_power_meter.py 与
docs/add_device_guide.md。
"""

from __future__ import annotations

import time

import serial


class SerialTransport:
    """RS-232 串口帧传输：开端口、写字节、按终止符读一帧、超时、清缓冲。

    职责边界：只管字节往返（本类），帧语义如 ``OK``/``ERRxx``（驱动）。
    读超时为"整体 deadline + 单次 read 各 0.5s"双层，与原 TDLMON 直写
    ``serial.Serial`` 的行为一致（P2 拆分，帧时序不变）。
    """

    def __init__(self, port: str, baudrate: int = 9600, bytesize: int = 8,
                 parity: str = "N", stopbits: int = 1,
                 read_timeout_s: float = 0.5, write_timeout_s: float = 0.5):
        self.port = port
        self._kwargs = dict(baudrate=baudrate, bytesize=bytesize, parity=parity,
                            stopbits=stopbits, timeout=read_timeout_s,
                            write_timeout=write_timeout_s)
        self._serial: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        """打开串口（SerialException 向上抛，由调用方决定换口/报错）。"""
        if self._serial is not None:
            return
        self._serial = serial.Serial(self.port, **self._kwargs)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def drain(self) -> None:
        """清空接收缓冲（连接/超时后的残包）。"""
        if self._serial is not None:
            self._serial.reset_input_buffer()

    def query_frame(self, request: bytes, reply_end: bytes,
                    timeout_s: float = 1.0, chunk: int = 64) -> bytes:
        """写一帧并读到 ``reply_end`` 终止符；返回不含终止符的应答字节。

        超时抛 ``TimeoutError``（协议层负责翻译成自己的错误类型）。
        """
        ser = self._serial
        if ser is None:
            raise IOError("串口未打开（先 open）")
        self.write_frame(request)
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline and reply_end not in buf:
            part = ser.read(chunk)
            if part:
                buf += part
        if reply_end not in buf:
            raise TimeoutError(f"等待应答超时: {request!r}")
        return bytes(buf.split(reply_end)[0])

    def write_frame(self, frame: bytes) -> None:
        """只写不读（无应答设备/命令用；异常向上抛由协议层翻译）。"""
        ser = self._serial
        if ser is None:
            raise IOError("串口未打开（先 open）")
        ser.write(frame)
        ser.flush()


class VisaTransport:
    """VISA 资源上的 SCPI 帧（P4 样式件，docs/architecture.md §7）。

    覆盖 TCPIP/GPIB/USB/串口等一切 pyvisa 资源（resource 串定传输）：
      TCPIP0::192.168.1.50::inst0::INSTR   # LXI 网络仪器
      GPIB0::15::INSTR                     # GPIB 总线
      ASRL3::INSTR                         # RS-232（COM3）
    SCPI 帧 = ASCII 命令 + 换行终止，与 SerialTransport 同一"帧往返"抽象，
    可互换注入驱动（transport= 参数）。

    pyvisa 懒加载：未安装 pyvisa 时本模块与整个 devices 包照常 import
    （registry 扫描不破），仅在真正 open() 时抛 ImportError——首个 SCPI
    设备进门时再 `pip install pyvisa pyvisa-py`。
    """

    def __init__(self, resource: str, read_termination: str = "\n",
                 write_termination: str = "\n", timeout_ms: int = 2000):
        self.resource = resource
        self._read_term = read_termination
        self._write_term = write_termination
        self._timeout_ms = timeout_ms
        self._inst = None

    @property
    def is_open(self) -> bool:
        return self._inst is not None

    def open(self) -> None:
        if self._inst is not None:
            return
        try:
            import pyvisa            # 懒加载（可选依赖）
        except ImportError as exc:
            raise ImportError(
                "SCPI/VISA 设备需要 pyvisa：conda activate lab_sf && "
                "pip install pyvisa pyvisa-py") from exc
        rm = pyvisa.ResourceManager()
        self._inst = rm.open_resource(
            self.resource, read_termination=self._read_term,
            write_termination=self._write_term, timeout=self._timeout_ms)

    def close(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            finally:
                self._inst = None

    def query(self, command: str) -> str:
        """SCPI 查询（问必答）：写命令读一行应答。"""
        if self._inst is None:
            raise IOError("VISA 资源未打开（先 open）")
        return self._inst.query(command)

    def write(self, command: str) -> None:
        """SCPI 设置（写不等应答）。"""
        if self._inst is None:
            raise IOError("VISA 资源未打开（先 open）")
        self._inst.write(command)
