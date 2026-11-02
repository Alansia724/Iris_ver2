"""Thorlabs K10CR1 笼式旋转台 —— 设备接口层（Kinesis .NET SDK，pythonnet 桥）。

纯 Python + pythonnet，无 GUI 依赖，供 agent 脚本、Jupyter、实验台 GUI 与
无头 CLI 共同调用。

依赖（一次性）::

    conda activate Iris
    pip install pythonnet        # 已装入 Iris 环境 (pythonnet 3.1)
    # Kinesis 套件装在 ``C:/Program Files/Thorlabs/Kinesis``（厂商原位，不复制）

硬件事实（官方日志 Device-55202754.log + 2026-08 真机实测）:

* K10CR1 是 Kinesis 生态的集成步机控制器，USB 枚举为 "APT USB Device"
  （FTDI VID_0403 PID_FAF0）；USB 总线供电（**勿接无源集线器**，供电不足
  电机无力矩）；K10CR1 上电后必须先 Home 才能绝对定位；
* Kinesis .NET 类：``Thorlabs.MotionControl.IntegratedStepperMotorsCLI.CageRotator``；
* **设备单位换算**（官方日志直接实测）：整圈 = **49152000 units**，
  即 136533.33 units/°。⚠ 网上常见的 1919.625 units/° 是其他型号的值，
  用错会缩小 71 倍。驱动对外一律用**度**，内部全部走 *_DeviceUnit 接口
  （避开 .NET 包装层的实单位换算）；
* 固件软限位 0 ~ 49152000 units（0~360°）：**绝对位置取模到 [0,360)**，
  相对运动无此限制；
* **运动等待必须轮询位置**：.NET 消息式等待（MoveRelative/MoveAbsolute 的
  waitTimeout）在本机不可靠且 moving 状态位可能粘滞 True 挡住后续命令。
  本驱动：命令前 StopImmediate 清状态位 → 短超时发令 → 轮询位置到连续
  稳定 → 校验到位误差；
* 回零重复性本机实测 ~0.16°。

用法::

    from iris_foundation.devices.k10cr1_rotator import K10cr1Rotator
    with K10cr1Rotator() as rot:               # 自动枚举 + 自动 Home
        rot.position                           # 当前角度（°，固件回读）
        rot.move_to(45.0)                      # 绝对运动（°，阻塞到稳定）
        rot.move_by(-10.0, wait=False)         # 相对运动（立即返回）
        rot.stop()                             # 急停
    K10cr1Rotator.list_devices()               # → ['55202754']
"""

from __future__ import annotations

import logging
import threading
import time

from iris_foundation.core.contract import DriverManifest, ParamDesc
from iris_foundation.core.topics import Dev

logger = logging.getLogger("iris_foundation.devices.k10cr1_rotator")

# Kinesis 安装位置（厂商原位，不复制；vendor 索引见 vendor/）
KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"

# K10CR1 固件单位换算（官方 Device-55202754.log：Rotation Cycle Size = 49152000）
UNITS_PER_REV = 49152000
UNITS_PER_DEG = UNITS_PER_REV / 360.0          # 136533.33 units/°

# 默认速度（°/s，设备单位下发）；K10CR1 标称最高 24 °/s
DEFAULT_VELOCITY_DEG_S = 10.0
DEFAULT_ACCEL_DEG_S2 = 20.0

# 已知支持型号（CageRotator.SupportedDevices()）
SUPPORTED_MODELS = ("K10CR1",)

# 到位判据：连续 N 次轮询位置变化 < STABLE_TOL 视为停稳
STABLE_TOL_DEG = 0.005
STABLE_COUNT = 4

# 停稳后的到位容差：本机回零重复性 ~0.16°，长行程后实测残余 ~0.38°（反向间隙）。
# 容差必须大于这个物理残余，否则永远判不到位——扫描会在 24° 处空等超时。
ARRIVE_TOL_DEG = 0.5

# 速度换算：设备单位/(°/s)。
# 速度的 device unit 与位置不是同一标度（位置 49,152,000/圈）：实测下发
# 1,365,333（按位置单位算的 10 °/s）只跑出 0.186 °/s（24° 用 129 s，扫描必超时），
# 厂商配置文件里对应 10 °/s 的 maxVelocity = 73,300,775，比值 53.7。
VEL_UNITS_PER_DEG_S = 7_330_077

# 相对运动的防缠绕安全区（°）
ANGLE_GUARD = 720.0


def _angle_gap(a: float, b: float) -> float:
    """两角最小间距（°）：位置取模到 [0,360)，停在 359.62 离 0 只差 0.38，不是 359.6。"""
    d = abs((float(a) - float(b)) % 360.0)
    return min(d, 360.0 - d)


class KinesisError(RuntimeError):
    """Kinesis SDK 缺失 / 设备通信失败 / 指令被拒。"""


def _load_kinesis():
    """惰性加载 Kinesis .NET 程序集（import 本模块不触发，缺 Kinesis 不拖垮平台）。"""
    import os
    import clr
    for name in ("Thorlabs.MotionControl.DeviceManagerCLI.dll",
                 "Thorlabs.MotionControl.GenericMotorCLI.dll",
                 "Thorlabs.MotionControl.IntegratedStepperMotorsCLI.dll"):
        clr.AddReference(os.path.join(KINESIS_DIR, name))
    from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
    from Thorlabs.MotionControl.GenericMotorCLI import MotorDirection
    from Thorlabs.MotionControl.IntegratedStepperMotorsCLI import CageRotator
    return DeviceManagerCLI, CageRotator, MotorDirection


class K10cr1Rotator:
    """Thorlabs K10CR1 笼式旋转台（一台控制器一个轴，单位：度）。

    Parameters
    ----------
    serial : str | None
        设备序列号（如 ``"55202754"``）。``None`` = connect() 时枚举，
        唯一设备自动选中；多台时报错要求显式指定。
    home_first : bool
        connect() 时自动 Home（默认 ``True``——K10CR1 不回零则绝对定位不可用，
        这是官方要求而非选项；Home 是真实运动，最多接近一圈）。
    velocity_deg_s, accel_deg_s2 : float
        连接后下发的运动速度/加速度（设备单位直接写，见 UNITS_PER_DEG）。
    """

    def __init__(self, serial: str | None = None, home_first: bool = True,
                 velocity_deg_s: float = DEFAULT_VELOCITY_DEG_S,
                 accel_deg_s2: float = DEFAULT_ACCEL_DEG_S2):
        self.serial = serial
        self.home_first = home_first
        self.velocity_deg_s = float(velocity_deg_s)
        self.accel_deg_s2 = float(accel_deg_s2)
        self._dev = None            # CageRotator .NET 实例
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 枚举/合同

    @staticmethod
    def list_devices() -> list[str]:
        """枚举在线 Kinesis 设备序列号（不打开设备）。"""
        DeviceManagerCLI, _CageRotator, _MD = _load_kinesis()
        DeviceManagerCLI.BuildDeviceList()
        return [str(s) for s in DeviceManagerCLI.GetDeviceList()]

    @classmethod
    def params(cls) -> tuple[ParamDesc, ...]:
        """扫描参数自声明：角度 0~360（度），留 2 s 稳定时间。

        getter 显式指向 position：驱动没有同名 ``angle`` 属性，不声明读不回
        （§15.3 运行记录里的角度快照靠它）。写侧仍缺声明：本驱动的 move_to
        默认 wait=True（阻塞至到位），扇入通用 writer 会卡住调用线程，
        故留待驱动主人显式声明非阻塞写侧（页走 call_async，不受影响）。
        """
        return (ParamDesc(
            key="angle", ptype="float", label="Angle (deg)", unit="deg",
            apply="write", vmin=0.0, vmax=360.0,
            access="rw", decimals=3, settle_ms=2000, getter="position"),)

    # ------------------------------------------------------------- 生命周期

    def connect(self, serial: str | None = None) -> None:
        """连接设备并初始化（连接 → 文件配置 → 等设置初始化 → 轮询 → 使能 →
        速度下发 → Home）。"""
        DeviceManagerCLI, CageRotator, _MD = _load_kinesis()
        DeviceManagerCLI.BuildDeviceList()
        serial = serial or self.serial
        online = self.list_devices()
        if serial is None:
            if len(online) != 1:
                raise KinesisError(
                    f"未指定 serial 且在线设备不唯一: {online or '（无）'}；"
                    "请用 K10cr1Rotator(serial='...') 显式指定")
            serial = online[0]
        elif serial not in online:
            raise KinesisError(f"设备 {serial} 不在线（在线: {online or '（无）'}）")
        try:
            dev = CageRotator.CreateCageRotator(serial)
            dev.Connect(serial)
        except Exception as exc:                       # noqa: BLE001 —— .NET 异常
            raise KinesisError(f"连接 Kinesis 设备 {serial} 失败: {exc}") from exc
        self.serial = serial
        self._dev = dev
        try:
            try:                                       # 假传输（--fake）下无 .NET 环境
                from Thorlabs.MotionControl.DeviceManagerCLI import DeviceConfiguration
                dev.LoadMotorConfiguration(
                    serial,
                    DeviceConfiguration.DeviceSettingsUseOptionType.UseFileSettings)
                dev.WaitForSettingsInitialized(5000)
            except ImportError:
                pass
            dev.StartPolling(250)
            dev.EnableDevice()
            time.sleep(0.3)                            # 使能生效
            self._apply_velocity()
        except Exception as exc:                       # noqa: BLE001
            self.disconnect()
            raise KinesisError(f"设备 {serial} 初始化失败: {exc}") from exc
        if self.home_first:
            self.home()

    def disconnect(self) -> None:
        if self._dev is not None:
            try:
                self._dev.ShutDown()
            except Exception:                          # noqa: BLE001
                pass
            finally:
                self._dev = None

    def __enter__(self) -> "K10cr1Rotator":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    @property
    def _require_dev(self):
        if self._dev is None:
            raise KinesisError("设备未连接（先 connect）")
        return self._dev

    # ------------------------------------------------------------- 查询

    @property
    def position(self) -> float:
        """当前角度（°，固件回读设备单位 → 度）。"""
        return float(self._require_dev.Position_DeviceUnit) / UNITS_PER_DEG

    def get_position(self) -> float:
        return self.position

    def set_angle(self, deg: float):
        """非阻塞写侧（扫描/编排用）：发令后返回 WriteHandle，由引擎轮询到位。

        ``move_to(wait=True)`` 会阻塞调用线程（引擎跑在 GUI 线程里），所以这里
        只发令；到位判定用位置（``is_moving`` 本机实测会粘滞）。
        ``resolve_setter`` 按 ``set_<key>`` 命名约定能直接找到本方法。
        """
        from iris_foundation.core.device_services import WriteHandle

        target = self.move_to(deg, wait=False)
        state = {"last": None, "stable": 0}

        def _arrived() -> bool:
            """停稳且到位 = 到位。

            只看瞬时距离不行：轮询周期 50 ms，10 °/s 下每周期走 0.5°，目标带
            ±0.05° 会被整个跳过（永远超时）；偶尔采到也是在运动途中穿过目标，
            随后继续滑出 0.3~0.4°（落盘回读因此对不上目标）。故先按位置连续
            STABLE_COUNT 次不变判定停稳，再校验残余误差。
            """
            try:
                pos = self.position
            except Exception:                      # noqa: BLE001
                return False
            prev, state["last"] = state["last"], pos
            state["stable"] = state["stable"] + 1 if (
                prev is not None and abs(pos - prev) < STABLE_TOL_DEG) else 0
            return (state["stable"] >= STABLE_COUNT
                    and _angle_gap(pos, target) <= ARRIVE_TOL_DEG)

        return WriteHandle(target, _arrived, timeout_s=120.0)

    @property
    def is_moving(self) -> bool:
        """运动中状态位（⚠ 本机实测可能粘滞 True，稳定判据以 _settle 为准）。"""
        try:
            return bool(self._require_dev.Status.IsMoving)
        except Exception:                              # noqa: BLE001
            return False

    def _settle(self, timeout: float, poll_s: float = 0.15) -> float:
        """轮询回读位置直到连续 STABLE_COUNT 次变化 < STABLE_TOL_DEG（停稳）。

        不信任 is_moving 状态位（可能粘滞）；以位置自身稳定性为准。
        返回停稳时的角度。超时抛 KinesisError。
        """
        deadline = time.monotonic() + timeout
        last = self.position
        stable = 0
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            cur = self.position
            if abs(cur - last) < STABLE_TOL_DEG:
                stable += 1
                if stable >= STABLE_COUNT:
                    return cur
            else:
                stable = 0
            last = cur
        raise KinesisError(f"等待运动停稳超时（{timeout:g}s，最后位置 {last:.4f}°）")

    def wait_until_still(self, timeout: float = 60.0, poll_s: float = 0.15) -> float:
        """等待停稳（位置稳定判据），返回停稳角度。"""
        return self._settle(timeout, poll_s)

    def device_info(self) -> str:
        """设备描述串（型号/固件，连接后可用）。"""
        try:
            return str(self._require_dev.DeviceDescription)
        except Exception:                              # noqa: BLE001
            return f"K10CR1 {self.serial}"

    # ------------------------------------------------------------- 运动控制

    def _check_angle(self, deg: float) -> float:
        deg = float(deg)
        if not -ANGLE_GUARD <= deg <= ANGLE_GUARD:
            raise KinesisError(f"角度 {deg:g}° 超出安全范围 ±{ANGLE_GUARD:g}°"
                               "（连续旋转台，但大圈数易缠绕线缆），拒绝发送")
        return deg

    def _clear_stuck_flag(self) -> None:
        """清除粘滞的 moving 状态位（本固件实测该位可能恒 True，挡住新命令）。

        物理静止时 StopImmediate 是无害空操作；若真在运动则立即停止——
        因此本驱动命令前总是先停，不做"运动中再插命令"。
        """
        try:
            self._require_dev.StopImmediate()
            time.sleep(0.05)
        except Exception:                              # noqa: BLE001
            pass

    def _fire(self, op, *args) -> None:
        """发运动命令：消息式等待不可靠，短超时发令（超时即忽略），
        等停稳交给 _settle。"""
        try:
            op(*args)
        except Exception as exc:                       # noqa: BLE001
            if type(exc).__name__ != "MoveTimeoutException":
                raise

    def _apply_velocity(self) -> None:
        """速度/加速度按设备单位下发（速度单位与位置不同标度，见 VEL_UNITS_PER_DEG_S）。

        失败只告警不阻断连接：连不上电机比跑得慢严重得多。
        """
        dev = self._require_dev
        try:
            dev.SetVelocityParams_DeviceUnit(
                int(round(self.velocity_deg_s * VEL_UNITS_PER_DEG_S)),
                int(round(self.accel_deg_s2 * VEL_UNITS_PER_DEG_S)))
            du = dev.GetVelocityParams_DeviceUnit()
            logger.info("K10CR1 速度：设备单位 %d/%d（目标 %.2f °/s）",
                        int(du.MaxVelocity), int(du.Acceleration),
                        self.velocity_deg_s)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("速度设定失败（沿用设备当前值）: %s", exc)

    def set_velocity(self, deg_per_s: float) -> float:
        """设置最大运动速度（°/s，1~24；加速度固定 DEFAULT_ACCEL_DEG_S2）。"""
        v = float(deg_per_s)
        if not 0 < v <= 24.0:
            raise ValueError(f"速度范围 0~24 °/s: {v}")
        self.velocity_deg_s = v
        self._apply_velocity()
        return v

    def move_to(self, deg: float, wait: bool = True,
                timeout: float = 120.0) -> float:
        """绝对运动到 deg（°，按整圈取模到 [0,360)）。

        ``wait=True`` 阻塞到停稳并校验到位。注意固件绝对软限位 0~360°。
        """
        deg = self._check_angle(deg)
        target = deg % 360.0
        with self._lock:
            dev = self._require_dev
            self._clear_stuck_flag()
            dev.SetMoveAbsolutePosition_DeviceUnit(int(round(target * UNITS_PER_DEG)))
            self._fire(dev.MoveAbsolute, 1000)
            if wait:
                settled = self._settle(timeout)
                if _angle_gap(settled, target) > ARRIVE_TOL_DEG:
                    raise KinesisError(
                        f"绝对运动未到位: 目标 {target:.3f}°，实际 {settled:.3f}°")
            return target

    def move_by(self, delta: float, wait: bool = True,
                timeout: float = 120.0) -> float:
        """相对运动 delta（°）。``wait=True`` 阻塞到停稳，返回运动后实际角度。"""
        delta = float(delta)
        if abs(delta) < 0.01:
            raise KinesisError(f"相对运动 {delta:g}° 过小，已拒绝")
        with self._lock:
            _, _, MotorDirection = _load_kinesis()
            direction = MotorDirection.Forward if delta > 0 else MotorDirection.Backward
            target = self._check_angle(self.position + delta)
            self._clear_stuck_flag()
            self._fire(self._require_dev.MoveRelative_DeviceUnit, direction,
                       int(round(abs(delta) * UNITS_PER_DEG)), 1000)
            if wait:
                settled = self._settle(timeout)
                if _angle_gap(settled, target) > ARRIVE_TOL_DEG:
                    raise KinesisError(
                        f"相对运动未到位: 目标 {target:.3f}°，实际 {settled:.3f}°")
                return settled
            return target

    def home(self, timeout: float = 120.0) -> float:
        """回零（编码器零位定位，真实运动——K10CR1 官方要求上电后先 Home）。

        本机实测回零重复性 ~0.16°，故容差 0.25°。
        """
        with self._lock:
            self._clear_stuck_flag()
            self._fire(self._require_dev.Home, 1000)
            settled = self._settle(timeout)
            if abs(settled) > 0.25:
                raise KinesisError(f"Home 后位置异常: {settled:.3f}°（预期 0±0.25）")
            return settled

    def stop(self) -> None:
        """立即停止运动（StopImmediate）。"""
        self._require_dev.StopImmediate()


# ---- 平台合同自声明（docs/architecture.md §6）----
DRIVER_MANIFEST = DriverManifest(
    driver="k10cr1_rotator", dev_id=Dev.ROTATOR, entry="K10cr1Rotator",
    display_name="Rotation Stage", model="Thorlabs K10CR1",
    capabilities=("motor_axis",), default_transport="kinesis",
    page="labcontrol.gui.pages.rotator_page",
    connect_timeout_ms=60000, disconnect_timeout_ms=15000)
