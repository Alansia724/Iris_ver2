"""iris-foundation —— 触手：device_list + 仪器驱动（IRIS ver2 实现指南）。

特有资产：device_list（device_id ↔ device_state）、仪器驱动程序
（devices/ 与 core/ 自 IRIS ver1 提取迁入）。

device_state：accessible / locked by sdk_id
  accessible        收到占用请求 -> locked by sdk_id
  locked by sdk_id  收到 task -> 查 state 一致性（不一致报错）-> work -> 回 data（不自动解锁）
                   收到释放请求 -> accessible（唯一解锁途径：显式释放）
"""

__version__ = "0.2.0"
