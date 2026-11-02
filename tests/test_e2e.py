"""端到端测试：严格按实现指南的框架规则断言（假仪器，无硬件）。

覆盖：
  1. 账号：注册 / 登录 / 重复注册报错
  2. 占用规则：accessible→可占用；locked by others→不得占用；非持有者不可释放
  3. task 一致性：未占用的仪器提交 task → 报错
  4. task.json：提交（跨两个 Foundation）→ 拆分分发 → 执行 → h5 data 回报
  5. 不自动解锁：task 完成后 device_state 仍为 locked by sdk_id
  6. 释放：显式 release 后 accessible → 其他账号可占用
"""

from __future__ import annotations

import io
import sys
import time

_HERE = r"D:\IRIS\iris_ver2\iris_ver2"
sys.path.insert(0, _HERE + r"\Foundation\src")
sys.path.insert(0, _HERE + r"\Service\src")
sys.path.insert(0, _HERE + r"\SDK\src")

import h5py

from iris_foundation.link import FoundationLink
from iris_foundation.node import FoundationNode
from iris_sdk.ai_client import AiClient, SdkError, make_task
from iris_service.main import ServiceDaemon


class FakeStage:
    def __init__(self):
        self.pos = 0.0

    def move_to(self, target: float) -> str:
        self.pos = float(target)
        return f"at {target}"

    def position(self) -> float:
        return self.pos


class FakeMeter:
    def read(self) -> float:
        return 42.0


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              + (f" — {detail}" if detail else ""), flush=True)
        if not cond:
            failures.append(name)

    def expect_err(name, fn, keyword=""):
        try:
            fn()
            check(name, False, "未报错")
        except SdkError as e:
            check(name, keyword in str(e), str(e))

    # ---- 起服务：核 + 两个触手 ----
    svc = ServiceDaemon()
    svc.start()
    fnd_a = FoundationNode("fnd-A")            # foundation_id 即 device_ip
    stage = FakeStage()
    fnd_a.attach_device("stageA", stage)
    FoundationLink(fnd_a).start()
    fnd_b = FoundationNode("fnd-B")
    fnd_b.attach_device("meterB", FakeMeter())
    FoundationLink(fnd_b).start()
    time.sleep(1.0)

    print("== 1. 账号 ==")
    alice = AiClient("alice", "s1")
    bob = AiClient("bob", "s2")
    check("注册并登录成功", alice.token is not None and bob.token is not None)
    try:
        AiClient("alice", "wrong")
        check("错误密码报错", False)
    except SdkError as e:
        check("错误密码报错", True)

    print("== 2. 占用规则 ==")
    states = alice.device_states()
    check("device_state_list 汇报", states["fnd-A"]["stageA"] == "accessible",
          str(states))
    check("accessible → alice 占用成功",
          alice.occupy("fnd-A", "stageA") == "locked by alice")
    expect_err("locked by others → bob 不得占用",
               lambda: bob.occupy("fnd-A", "stageA"), "不得占用")
    expect_err("非持有者不可释放",
               lambda: bob.release("fnd-A", "stageA"), "不可释放")

    print("== 3. task 一致性检查 ==")
    task_bad = make_task([{"device_ip": "fnd-B", "device_id": "meterB",
                           "call": "read"}])
    expect_err("未占用的仪器提交 task → 报错",
               lambda: alice.submit_task(task_bad), "device_state")

    print("== 4. task.json 提交与 data 回报 ==")
    check("alice 占用第二台", alice.occupy("fnd-B", "meterB")
          == "locked by alice")
    task = make_task([
        {"device_ip": "fnd-A", "device_id": "stageA",
         "call": "move_to", "args": [5.0]},
        {"device_ip": "fnd-B", "device_id": "meterB", "call": "read"},
        {"device_ip": "fnd-A", "device_id": "stageA", "call": "position"},
    ])
    r = alice.submit_task(task)
    task_id = r["task_id"]
    check("跨双 Foundation 提交成功", sorted(r["foundations"])
          == ["fnd-A", "fnd-B"])
    data = alice.get_data(task_id, save_dir=r"D:\IRIS\iris_ver2\tests\out")
    blobs = data["data"]
    check("收到两个 Foundation 的 h5 data", set(blobs) == {"fnd-A", "fnd-B"})
    with h5py.File(io.BytesIO(blobs["fnd-A"]), "r") as f:
        res0 = f["instructions/i0/result"][()]   # move_to → "at 5.0"
        res1 = f["instructions/i1/result"][()]   # position → 5.0
    check("h5 内容正确（move_to 与 position）",
          float(res1) == 5.0 and abs(stage.pos - 5.0) < 1e-12,
          f"i0={res0} i1={res1}")

    print("== 5. 不自动解锁 ==")
    states = alice.device_states()
    check("task 完成后仍 locked by alice",
          states["fnd-A"]["stageA"] == "locked by alice"
          and states["fnd-B"]["meterB"] == "locked by alice")

    print("== 6. 显式释放与转移占用 ==")
    check("alice 释放 → accessible",
          alice.release("fnd-A", "stageA") == "accessible")
    check("bob 现在可占用",
          bob.occupy("fnd-A", "stageA") == "locked by bob")
    alice.release("fnd-B", "meterB")

    print("== 7. 登录防爆破（借鉴 genie 五次上限） ==")
    AiClient("eve", "right")                    # 正常注册一个号
    for i in range(5):                          # 连错 5 次
        try:
            AiClient("eve", f"wrong{i}")
        except SdkError:
            pass
    expect_err("第 6 次失败被短锁拒绝",
               lambda: AiClient("eve", "wrong6"), "频繁")

    print()
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
