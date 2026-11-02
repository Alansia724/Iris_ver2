"""全局常量：DataHub 主题名与设备 id。

主题与设备 id 的唯一事实来源：拼错主题名 = 订阅落空 = 静默无数据，
因此全部字面量收敛到这里，其余模块只引用常量。

设备流主题由命名规则派生（<dev_id>.<stream_name>，docs/architecture.md
§4.4）：常量只声明"哪台设备的哪条流"，字符串由 contract.stream_topic
拼出——与各驱动 manifest 里 StreamDesc.topic() 同一规则，天然一致；
非设备流（引擎级）保留字面量。
"""

from __future__ import annotations

from iris_foundation.core.contract import stream_topic


class Dev:
    """设备 id（workspace/PARAM_CATALOG/服务键/左栏共用）。"""

    CAMERA = "camera"
    SPEC = "spec"
    DVP = "dvp"
    ATTENUATOR = "attenuator"
    POWER_METER = "power_meter"
    ROTATOR = "rotator"
    PICOMOTOR = "picomotor"
    CONEX = "conex"


class Topics:
    """DataHub 主题名。设备流 = stream_topic(dev, name)（规则唯一）。"""

    CAMERA_FRAME = stream_topic(Dev.CAMERA, "frame")       # Andor CCD 帧
    DVP_FRAME = stream_topic(Dev.DVP, "frame")             # DVP CMOS 帧
    CAMERA_TEMP = stream_topic(Dev.CAMERA, "temp")         # CCD 温度轮询
    CAMERA_STATUS = stream_topic(Dev.CAMERA, "status")     # CCD 状态轮询
    SPEC_CALIBRATION = stream_topic(Dev.SPEC, "calibration")  # 每像素波长标定（或 None）
    SCAN_POINT = "scan.point"           # 扫描每帧发布（引擎 → 扫描图监视器；非设备流）
