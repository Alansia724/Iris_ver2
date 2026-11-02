"""iris_foundation.core — 运行时内核（无窗口部件依赖）。

从 gui/ 收敛出的核心层：设备无关的发布-订阅中枢、全局常量、
HDF5 落盘约定。只依赖 QtCore（不 import QtWidgets），GUI 与 CLI 共用：

    iris_foundation.core.topics   Topics/Dev 常量（主题名/设备 id 唯一事实来源）
    iris_foundation.core.hub      DataHub 发布-订阅（进程内、GUI 线程）
    iris_foundation.core.storage  HDF5 统一落盘（schema_version/帧栈/波长标定）
"""
