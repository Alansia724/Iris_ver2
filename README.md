# IRIS ver2 —— 按《IRIS_ver2_实现指南》生成的全新源码

> Service 是核，SDK 是接口，Foundation 是触手。
> ver1 仓库保持不动；ver2 仅实现框架所述内容，不添加框架之外的功能。

## 三个代码包

| 包 | 目录 | 职责 |
|---|---|---|
| Foundation（触手） | `Foundation/src/iris_foundation` | 特有资产 device_list（device_id ↔ device_state）+ 仪器驱动（devices/、core/ 自 ver1 提取迁入）。占用/释放状态转移、task 执行→h5 data 回传、不自动解锁 |
| Service（核） | `Service/src/iris_service` | 三面：面向 SDK（账号/读取 task.json/回报 data/汇报 device_state_list）；面向 Foundation（分发 task_to_foundation_id.json【拆分不编辑】/接收 h5 data/set device_state）；inner-face（sdk-list、foundation-list、task 与 device_state 一致性检查） |
| SDK（接口） | `SDK/src/iris_sdk` | AI 协作客户端 AiClient（GUI 面板之后再说）：占用仪器请求 ①、task.json 提交 ②、data 收取 |

## 标识与状态

- 每个 SDK ↔ 一个账号（sdk_id）；每个 Foundation 有 foundation_id，
  **foundation_id 同时作为仪器的 device_ip（路由地址）**
- device_state：`accessible` / `locked by sdk_id`
- 解锁唯一途径：持有者显式 release（task 完成不自动解锁）

## task.json

指南书写形：

```
To device_ip X, device_id A,
    调试函数;
To device_ip Y, device_id B,
    调试函数;
```

JSON 对应（`iris_sdk.ai_client.make_task` 构造）：

```json
{
  "instructions": [
    {"device_ip": "X", "device_id": "A", "call": "函数名", "args": [...]},
    {"device_ip": "Y", "device_id": "B", "call": "函数名", "args": [...]}
  ]
}
```

Service 按device_ip（=foundation_id）拆分为 task_to_foundation_id.json
分发（指令内容原样传递，不编辑）；提交前置检查：所需仪器必须都被
提交方 sdk_id 占用，不一致报错；Foundation 执行时再以自身 device_list
复核一次。

## 端口

| 端口 | 归属 | 面 |
|---|---|---|
| 7850 | Service | 面向 SDK |
| 7851 | Service | 面向 Foundation（Foundation 主动拨号注册并长轮询） |

## 安装与验证

```powershell
pip install -e Foundation
pip install -e Service
pip install -e SDK
python tests\test_e2e.py     # 端到端：假仪器，严格按框架规则断言
```
