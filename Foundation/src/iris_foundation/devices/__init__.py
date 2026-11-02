"""设备 API 层：每台设备一个模块，纯 Python + ctypes，零 GUI 依赖。

驱动尾部 DRIVER_MANIFEST 自声明（平台合同，docs/architecture.md §6）；
共享传输（字节层往返）在 transports.py（§7）。
"""
