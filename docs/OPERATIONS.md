# OPERATIONS —— 运行态 SSOT（单一直相源）

> 借鉴 genie-service OPERATIONS 纪律：端口表、重启纪律、已知边界，
> 一条事实只有一个地址。本文只谈"怎么跑"；架构语义见 README 与
> 《IRIS_ver2_实现指南》。

## 端口表（SSOT = 此处）

| 机器 | 组件 | 端口 | 角色 |
|---|---|---|---|
| 服务主机 | iris_service（SDK 面） | :7850 | 账号/占用/释放/submit_task/get_data/device_states（msgpack over zmq REP） |
| 服务主机 | iris_service（FND 面） | :7851 | register / poll / reply（Foundation 主动拨号+长轮询） |
| 仪器 PC | iris_foundation | 无监听 | 主动外连 :7851，不暴露任何入站端口 |

## 启动顺序

```
1. 服务主机：  python -m iris_service --host <本机Tailscale IP>
              # 单机/回环测试可省 --host（默认 127.0.0.1）
2. 各仪器 PC： python -m iris_foundation ...    # 触手拨号注册 device_list
              # link 的 service_face 指 tcp://<服务主机IP>:7851
3. 用户侧：    AiClient(sdk_id, secret, service="tcp://<服务主机IP>:7850")
```

## 重启纪律（重启前必读）

| 动作 | 影响 | 纪律 |
|---|---|---|
| 重启 iris_service | **内存态全丢**：账号/token、foundation-list 镜像、task data | ①公告所有在线 SDK；②重启后**必须重启所有 Foundation 的 link**（注册只发生在 link.start()）；③已占用设备的锁存于 Foundation 侧——**Foundation 不重启则锁仍在**，重启后 Service 镜像会显示 accessible 但 Foundation 复核会拒绝 task，须人肉核对 |
| 重启 iris_foundation | 该节点 device_state **回 accessible**（内存态） | 若有他人占用，先协商；正在执行的 task 会丢结果 |
| 全部重启 | 锁与任务全清 | 仪器物理状态不受影响（占用语义≠物理动作） |

## 已知边界（挂账）

1. **任务已持久化**（2026-11-02 起）：h5 落 `~/iris_ver2/data/<task_id>.<fid>.h5`，
   台账 `~/iris_ver2/logs/tasks.jsonl`（启动回放，task_info/task_list/get_data
   重启后仍可读）；**账号/token 仍为内存态**——重启需重新登录
   （sqlite 方案参考 genie-service accounts.py，待迁）。
2. **占用的真相在 Foundation 内存**：Service 镜像可能漂移（Foundation 不重启而 Service 重启时），以 Foundation 复核为准。
3. **限流为单实例内存态**：多 Service 实例部署时防爆破/节流不共享。
4. **无 TLS/对外门**：跨机部署绑定 Tailscale IP；对外暴露需自加反代。

## 验证入口

```
python tests\test_e2e.py        # 端到端（假仪器，框架规则全断言）
python tests\test_layering.py   # 分层守卫（依赖方向=契约）
```
