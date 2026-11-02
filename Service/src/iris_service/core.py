"""Service 内核 —— 三面实现（IRIS ver2 实现指南 Service 节）。

面向 SDK   ：管理账号 / 读取 task.json / 回报 data / 汇报 device_state_list
面向 FND   ：管理与控制设备 / 分发 task_to_foundation_id.json（拆分不编辑）
             / 接收 data(h5) / set device_state
inner-face ：sdk-list（sdk_id ↔ task）+ foundation-list
             （foundation_id ↔ device_list(device_id, device_state)）
             + task 调用仪器与 device_state 一致性检查（不一致报错）
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import secrets
import threading
import time
import uuid

log = logging.getLogger("iris_service.core")

_req_ids = itertools.count(1)


def _req_id() -> str:
    return f"req-{next(_req_ids)}"


class AccountError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    """限流/防爆破拒绝（借鉴 genie-service：失败过多短锁）。"""


class RateLimiter:
    """滑动窗口计数器：key → 窗口内事件数，超限即拒。"""

    def __init__(self, max_events: int, window_s: float):
        self.max_events = max_events
        self.window_s = window_s
        self._mu = threading.Lock()
        self._events: dict[str, list[float]] = {}

    def hit(self, key: str) -> None:
        """记录一次事件；窗口内超限抛 RateLimitError。"""
        now = time.monotonic()
        with self._mu:
            ev = [t for t in self._events.get(key, [])
                  if now - t < self.window_s]
            if len(ev) >= self.max_events:
                self._events[key] = ev
                raise RateLimitError(
                    f"操作过于频繁（{self.max_events} 次/"
                    f"{int(self.window_s)}s 内），请稍后再试")
            ev.append(now)
            self._events[key] = ev

    def check(self, key: str) -> None:
        """只查是否已超限，不记账（用于锁定期提前拒绝）。"""
        now = time.monotonic()
        with self._mu:
            ev = [t for t in self._events.get(key, [])
                  if now - t < self.window_s]
            self._events[key] = ev
            if len(ev) >= self.max_events:
                raise RateLimitError(
                    f"操作过于频繁（{self.max_events} 次/"
                    f"{int(self.window_s)}s 内），请稍后再试")

    def reset(self, key: str) -> None:
        with self._mu:
            self._events.pop(key, None)


class StateError(RuntimeError):
    """占用/释放/task 与 device_state 不一致（inner-face 检查）。"""


# ------------------------------------------------------------ 账号

class Accounts:
    """账号管理（sdk_id 即账号）。内存 + 可选落盘。

    防爆破（借鉴 genie-service OTP 五次上限）：登录失败按 sdk_id 记账，
    15 分钟内 5 次失败即短锁；成功登录清账。
    """

    FAIL_MAX = 5
    FAIL_WINDOW_S = 900          # 15 分钟

    def __init__(self):
        self._mu = threading.Lock()
        self._users: dict[str, dict] = {}     # sdk_id → {secret, token}
        self._login_fails = RateLimiter(self.FAIL_MAX, self.FAIL_WINDOW_S)

    def register(self, sdk_id: str, secret: str) -> None:
        with self._mu:
            if sdk_id in self._users:
                raise AccountError(f"账号 {sdk_id!r} 已存在")
            self._users[sdk_id] = {
                "secret": hashlib.sha256(secret.encode()).hexdigest(),
                "token": None}

    def login(self, sdk_id: str, secret: str) -> str:
        self._login_fails.check(sdk_id)          # 锁定期内直接拒绝
        with self._mu:
            u = self._users.get(sdk_id)
            if u is None:
                self._login_fails.hit(sdk_id)
                raise AccountError(f"账号 {sdk_id!r} 不存在")
            if u["secret"] != hashlib.sha256(secret.encode()).hexdigest():
                self._login_fails.hit(sdk_id)
                raise AccountError("密码错误")
            u["token"] = secrets.token_hex(16)
        self._login_fails.reset(sdk_id)          # 成功清账
        return u["token"]

    def verify(self, sdk_id: str, token: str) -> None:
        with self._mu:
            u = self._users.get(sdk_id)
            if u is None or not token or u["token"] != token:
                raise AccountError(f"sdk_id/token 校验失败: {sdk_id!r}")


# ------------------------------------------------------------ 核心

class _Pending:
    """发给某 Foundation 的一条指令及其应答等待。"""

    def __init__(self, cmd: dict):
        self.cmd = cmd
        self.req_id = cmd["req_id"]
        self.event = threading.Event()
        self.reply: dict | None = None


class ServiceCore:
    def __init__(self, reply_timeout_s: float = 30.0):
        self.accounts = Accounts()
        self.reply_timeout_s = reply_timeout_s
        # 操作节流（借鉴 genie-service 限流闸）：写操作每 sdk 60 次/分钟
        self._op_limit = RateLimiter(60, 60)
        self._mu = threading.Lock()
        # inner-face: sdk-list（sdk_id ↔ task）
        self.sdk_list: dict[str, list[str]] = {}
        # inner-face: foundation-list（foundation_id ↔ device_list(device_id, state)）
        self.foundation_list: dict[str, dict] = {}
        self._queues: dict[str, list[_Pending]] = {}   # foundation_id → 待取指令
        self._waiting: dict[str, _Pending] = {}        # req_id → Pending
        self._tasks: dict[str, dict] = {}              # task_id → 记录（含内容）
        # ---- 任务持久化：h5 落盘 + JSONL 台账（重启可读） ----
        from pathlib import Path
        self.data_dir = Path.home() / "iris_ver2" / "data"
        self.journal_path = Path.home() / "iris_ver2" / "logs" / "tasks.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_journal()

    # ---- 台账：追加 / 启动回放 ----

    def _journal_append(self, record: dict) -> None:
        import json
        try:
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("任务台账追加失败")

    def _load_journal(self) -> None:
        """启动回放：历史任务记录（h5 已在盘上）重新可查。"""
        import json
        if not self.journal_path.exists():
            return
        n = 0
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            tid = rec.get("task_id")
            if not tid:
                continue
            self._tasks[tid] = rec
            self.sdk_list.setdefault(rec.get("sdk_id", ""), []).append(tid)
            n += 1
        if n:
            log.info("任务台账回放: %d 条历史任务", n)

    # ================= 面向 Foundation =================

    def fnd_register(self, foundation_id: str, device_list: dict) -> dict:
        with self._mu:
            self.foundation_list[foundation_id] = {"devices": dict(device_list)}
            self._queues.setdefault(foundation_id, [])
        log.info("foundation 注册: %s devices=%s", foundation_id, device_list)
        return {"ok": True}

    def fnd_poll(self, foundation_id: str) -> dict:
        with self._mu:
            q = self._queues.get(foundation_id)
            if not q:
                return {"ok": True, "cmd": None}
            return {"ok": True, "cmd": q.pop(0).cmd}

    def fnd_reply(self, foundation_id: str, req_id: str,
                  reply: dict, device_list: dict) -> dict:
        with self._mu:
            # set device_state：以 Foundation 上报的 device_list 为准（权威）
            if foundation_id in self.foundation_list:
                self.foundation_list[foundation_id]["devices"] = dict(
                    device_list)
            p = self._waiting.pop(req_id, None)
        if p is not None:
            p.reply = reply
            p.event.set()
        return {"ok": True}

    # ---- 向 Foundation 下发指令并等应答 ----

    def _send(self, foundation_id: str, cmd: dict) -> dict:
        cmd = {"req_id": _req_id(), **cmd}
        p = _Pending(cmd)
        with self._mu:
            if foundation_id not in self.foundation_list:
                raise StateError(f"未知 device_ip/foundation: {foundation_id!r}")
            self._queues[foundation_id].append(p)
            self._waiting[cmd["req_id"]] = p
        if not p.event.wait(self.reply_timeout_s):
            with self._mu:
                self._waiting.pop(cmd["req_id"], None)
            raise StateError(f"foundation {foundation_id!r} 应答超时")
        return p.reply or {"ok": False, "error": "no reply"}

    # ================= 面向 SDK =================

    def sdk_device_states(self) -> dict:
        """汇报 device_state_list。"""
        with self._mu:
            return {fid: dict(info["devices"])
                    for fid, info in self.foundation_list.items()}

    def sdk_occupy(self, sdk_id: str, device_ip: str, device_id: str) -> dict:
        """占用仪器请求（device_state=accessible 时允许；locked by others 不得占用）。"""
        self._op_limit.hit(sdk_id)
        self._mirror_check_occupy(sdk_id, device_ip, device_id)
        r = self._send(device_ip, {"op": "occupy", "sdk_id": sdk_id,
                                   "device_id": device_id})
        if not r.get("ok"):
            raise StateError(f"占用失败: {r.get('error')}")
        log.info("占用: %s ← %s/%s", sdk_id, device_ip, device_id)
        return {"device_state": r["value"]}

    def sdk_release(self, sdk_id: str, device_ip: str, device_id: str) -> dict:
        """释放仪器请求（被自己占用时可释放 → accessible）。"""
        self._op_limit.hit(sdk_id)
        self._mirror_check_release(sdk_id, device_ip, device_id)
        r = self._send(device_ip, {"op": "release", "sdk_id": sdk_id,
                                   "device_id": device_id})
        if not r.get("ok"):
            raise StateError(f"释放失败: {r.get('error')}")
        log.info("释放: %s → %s/%s", sdk_id, device_ip, device_id)
        return {"device_state": r["value"]}

    def sdk_submit_task(self, sdk_id: str, task: dict) -> dict:
        """读取 task.json：仅当所需仪器都被自己占用时允许提交。"""
        self._op_limit.hit(sdk_id)
        instructions = task.get("instructions", [])
        if not instructions:
            raise StateError("task 无 instructions")
        # ---- inner-face 一致性检查（不一致报错）----
        for ins in instructions:
            dip, dev = ins.get("device_ip", ""), ins.get("device_id", "")
            self._check_locked_by(sdk_id, dip, dev)
        # ---- 拆分（不编辑）：按 device_ip 分组，指令原样传递 ----
        parts: dict[str, list] = {}
        for ins in instructions:
            parts.setdefault(ins["device_ip"], []).append(ins)
        task_id = task.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
        # 审计：任务内容留存 + 提交日志（此前只存结果，内容即丢）
        summary = "; ".join(
            f"{i['device_ip']}/{i['device_id']}.{i.get('call', '')}"
            for i in instructions)
        log.info("task 提交: %s by %s — %d 条指令 [%s]",
                 task_id, sdk_id, len(instructions), summary)
        submitted = time.time()
        data: dict[str, bytes] = {}
        errors: dict[str, str] = {}
        for fid, ins_list in parts.items():       # 逐 Foundation 下发
            r = self._send(fid, {"op": "task", "sdk_id": sdk_id,
                                 "instructions": ins_list})
            if r.get("ok"):
                data[fid] = r["value"]["data"]     # 接收 data（h5）
            else:
                errors[fid] = r.get("error", "")
        # ---- 持久化：h5 落盘 + 台账追加（重启后任务内容/日志仍可读） ----
        files: dict[str, str] = {}
        for fid, blob in data.items():
            path = self.data_dir / f"{task_id}.{fid}.h5"
            try:
                path.write_bytes(blob)
                files[fid] = str(path)
            except OSError:
                log.exception("h5 落盘失败: %s", path)
        entry = {"task_id": task_id, "sdk_id": sdk_id,
                 "instructions": instructions,
                 "parts": {fid: len(ins) for fid, ins in parts.items()},
                 "files": files, "errors": errors,
                 "submitted_at": submitted, "finished_at": time.time()}
        with self._mu:
            self._tasks[task_id] = entry           # sdk-list: sdk_id ↔ task
            self.sdk_list.setdefault(sdk_id, []).append(task_id)
        self._journal_append(entry)                # 台账（内存即记录本体）
        log.info("task 完成: %s errors=%s files=%s", task_id,
                 errors or "无", sorted(files))
        if errors:
            raise StateError(f"task 执行失败: {errors}")
        return {"task_id": task_id, "foundations": sorted(data)}

    def _task_of(self, sdk_id: str, task_id: str) -> dict:
        with self._mu:
            t = self._tasks.get(task_id)
        if t is None or t.get("sdk_id") != sdk_id:
            raise StateError(f"无此任务或非本账号提交: {task_id!r}")
        return t

    def sdk_task_info(self, sdk_id: str, task_id: str) -> dict:
        """任务内容可读：instructions / 拆分 / 时间 / 文件路径 / 错误。"""
        t = self._task_of(sdk_id, task_id)
        return {k: t.get(k) for k in
                ("task_id", "sdk_id", "instructions", "parts", "errors",
                 "submitted_at", "finished_at", "files")}

    def sdk_task_list(self, sdk_id: str) -> list[dict]:
        """本账号全部任务摘要（sdk-list 视图）。"""
        with self._mu:
            ids = list(self.sdk_list.get(sdk_id, []))
        return [{"task_id": i,
                 "submitted_at": self._tasks.get(i, {}).get("submitted_at"),
                 "instructions": len(
                     self._tasks.get(i, {}).get("instructions", []))}
                for i in ids]

    def sdk_get_data(self, sdk_id: str, task_id: str) -> dict:
        t = self._task_of(sdk_id, task_id)
        data: dict[str, bytes] = {}
        for fid, path in (t.get("files") or {}).items():
            try:
                data[fid] = open(path, "rb").read()   # 从落盘文件回读
            except OSError:
                log.exception("h5 回读失败: %s", path)
        return {"data": data, "errors": t.get("errors", {}),
                "instructions": t.get("instructions", []),
                "files": t.get("files", {})}

    # ================= inner-face 检查（镜像预检） =================

    def _device_state_of(self, device_ip: str, device_id: str) -> str:
        with self._mu:
            info = self.foundation_list.get(device_ip)
            if info is None:
                raise StateError(f"未知 device_ip/foundation: {device_ip!r}")
            st = info["devices"].get(device_id)
            if st is None:
                raise StateError(f"{device_ip!r} 上无此仪器: {device_id!r}")
            return st

    def _check_locked_by(self, sdk_id: str, device_ip: str, device_id: str):
        st = self._device_state_of(device_ip, device_id)
        if st != f"locked by {sdk_id}":
            raise StateError(
                f"task 调用 {device_ip}/{device_id} 但 device_state={st!r}"
                f"（需 locked by {sdk_id!r}）")

    def _mirror_check_occupy(self, sdk_id, device_ip, device_id):
        st = self._device_state_of(device_ip, device_id)
        if st != "accessible":                    # locked by others 不得占用
            raise StateError(f"device_state={st!r}，不得占用")

    def _mirror_check_release(self, sdk_id, device_ip, device_id):
        st = self._device_state_of(device_ip, device_id)
        if st != f"locked by {sdk_id}":
            raise StateError(f"device_state={st!r}，非本 sdk 占有，不可释放")
