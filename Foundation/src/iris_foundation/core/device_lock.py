"""设备独占（跨进程）——docs/architecture.md §17。

一台真机在同一时刻只能被一个进程打开：Andor 的 DLL 尤其如此（两个进程同时
打开会让设备进入不可预期状态）。以前靠纪律（README 里那句"运行前先关 Solis"），
现在靠一个事实——**谁能打开设备，谁先拿到锁**。

三条设计约束：

1. **锁是 OS 级文件锁**（Windows `msvcrt` / POSIX `fcntl`）：进程死了锁自动
   消失，不会留下"上次崩过所以现在谁也连不上"的僵尸锁；
2. **锁文件里写着持有者**（pid/用户/用途/起止），抢不到时说得出"被谁占着"，
   而不是一句"设备忙"；
3. **不引入新概念**：GUI / CLI（run_experiment）/ 远端设备服务共用同一套，
   谁开设备谁加锁，关掉就释放。

锁表落在仓库根的 `logs/locks/<dev_id>.lock`（gitignored）：人可看、可 grep。
锁文件的内容形状有个 Windows 约束：**首字节是 OS 锁的靶子，持有者 JSON 从
第 2 字节起**——Windows 的字节区间锁连读取也一并挡住，读写同一区域就读不到
"是谁占着"（2026-09 实测踩到）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 本进程持有的锁：{dev_id: DeviceLock}（同进程重复 acquire 幂等）
_OWN: dict[str, "DeviceLock"] = {}

#: 锁文件首字节：OS 字节锁只锁它，持有者 JSON 从第 2 字节起（见模块 docstring）
_LOCK_BYTE = "L"


class DeviceBusy(RuntimeError):
    """设备已被别的进程占用（消息里带持有者，可直接给用户看）。"""

    def __init__(self, dev_id: str, holder: dict | None):
        self.dev_id = dev_id
        self.holder = holder or {}
        super().__init__(f"{dev_id} 已被占用（{describe(holder)}）")


def describe(holder: dict | None) -> str:
    """持有者人话描述（无信息 = 未知进程）。"""
    if not holder:
        return "未知进程"
    bits = [f"pid {holder.get('pid', '?')}"]
    for key, label in (("user", ""), ("purpose", ""), ("since", "自 ")):
        value = holder.get(key)
        if value:
            bits.append(f"{label}{value}")
    return " · ".join(bits)


def owner_info(purpose: str = "") -> dict:
    """本进程的持有者信息。"""
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    return {
        "pid": os.getpid(),
        "user": user,
        "purpose": purpose or "",
        "since": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- 锁目录

def lock_dir() -> Path:
    """锁目录：仓库根 logs/locks（与日志同处；找得到 pyproject 才算仓库）。"""
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "logs" / "locks"
    import tempfile
    return Path(tempfile.gettempdir()) / "labcontrol-locks"   # 装到别处时的退路


def _lock_path(dev_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(dev_id))
    return lock_dir() / f"{safe}.lock"


# ---------------------------------------------------------------- 读持有者

def _read_info(dev_id: str) -> dict | None:
    """锁文件里的持有者 JSON（空/坏行 = None）。"""
    path = _lock_path(dev_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            fh.seek(1)          # 跳开被 OS 锁住的首字节（Windows 连读也挡）
            text = fh.read().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_locked(dev_id: str) -> bool:
    """锁文件现在真的被锁着吗（试锁一次即放）。

    为何不能只看文件内容：持有者信息在释放时会清空、崩溃时会残留——
    “有主”必须 = 内容非空 **且** 锁真的在，否则界面会把一个崩溃残留
    报成“设备被占用”（§17）。
    """
    path = _lock_path(dev_id)
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    with os.fdopen(fd, "r+") as fh:
        if os.fstat(fh.fileno()).st_size == 0:
            return False
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False        # 试锁成功 = 没人在用
        except OSError:
            return True         # 试锁失败 = 有人占着


def holder(dev_id: str) -> dict | None:
    """当前持有者；**没人持锁 = None**（崩溃残留的旧内容不算持有者）。"""
    if dev_id in _OWN:
        return _read_info(dev_id) or owner_info()
    info = _read_info(dev_id)
    if not info:
        return None
    return info if _is_locked(dev_id) else None


def held() -> dict[str, dict]:
    """全部被占用的设备（界面/CLI 显示用；只读锁表，不加锁）。"""
    out: dict[str, dict] = {}
    for path in sorted(lock_dir().glob("*.lock")):
        info = holder(path.stem)
        if info:
            out[path.stem] = info
    return out


# ---------------------------------------------------------------- 锁

class DeviceLock:
    """一台设备的独占锁（上下文管理器）。"""

    def __init__(self, dev_id: str, purpose: str = ""):
        self.dev_id = str(dev_id)
        self.purpose = purpose
        self._fh = None

    # ---- OS 级加解锁 ----

    @staticmethod
    def _try_lock(fh) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    @staticmethod
    def _unlock(fh) -> None:
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.debug("解锁失败（进程退出时 OS 也会释放）：%s", fh.name,
                         exc_info=True)

    # ---- 取锁 / 还锁 ----

    def acquire(self, timeout_s: float = 0.0) -> "DeviceLock":
        """取锁；抢不到抛 DeviceBusy（timeout_s>0 则在时限内重试）。"""
        existing = _OWN.get(self.dev_id)
        if existing is not None:
            return existing                      # 同进程重复取锁 = 幂等
        path = _lock_path(self.dev_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(_LOCK_BYTE)         # 首字节：锁的靶子（第 2 字节起写 JSON）
                fh.flush()
            fh.seek(0)
            ok = self._try_lock(fh)
            deadline = time.monotonic() + max(0.0, timeout_s)
            while not ok and time.monotonic() < deadline:
                time.sleep(0.1)
                fh.seek(0)
                ok = self._try_lock(fh)
            if not ok:
                raise DeviceBusy(self.dev_id, holder(self.dev_id))
            fh.seek(1)
            fh.truncate()
            fh.write(json.dumps(owner_info(self.purpose), ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
        except BaseException:
            try:
                fh.close()
            except Exception:
                pass
            raise
        self._fh = fh
        _OWN[self.dev_id] = self
        logger.debug("设备锁已取得：%s（%s）", self.dev_id, self.purpose)
        return self

    def release(self) -> None:
        """还锁（多次调用无副作用）。"""
        fh = self._fh
        self._fh = None
        _OWN.pop(self.dev_id, None)
        if fh is None:
            return
        try:
            fh.seek(1)              # 清空持有者信息：残留内容不得被当成“有人在用”
            fh.truncate()
            fh.flush()
        except OSError:
            logger.debug("清空持有者信息失败：%s", self.dev_id, exc_info=True)
        try:
            self._unlock(fh)
        finally:
            try:
                fh.close()
            except Exception:
                pass
        logger.debug("设备锁已释放：%s", self.dev_id)

    @property
    def owned(self) -> bool:
        return self._fh is not None

    def holder(self) -> dict | None:
        return holder(self.dev_id)

    def __enter__(self) -> "DeviceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# ---------------------------------------------------------------- 模块级入口

def acquire(dev_id: str, purpose: str = "",
            timeout_s: float = 0.0) -> DeviceLock:
    """取设备锁（抢不到抛 DeviceBusy）；同进程重复取 = 返回同一个锁。"""
    return DeviceLock(dev_id, purpose).acquire(timeout_s=timeout_s)


def release(dev_id: str) -> None:
    """释放本进程持有的设备锁（没持有 = 无副作用）。"""
    lock = _OWN.get(dev_id)
    if lock is not None:
        lock.release()


def release_all() -> None:
    """释放本进程全部设备锁（窗口关闭/CLI 收尾）。"""
    for dev_id in list(_OWN):
        release(dev_id)


def owned(dev_id: str) -> bool:
    return dev_id in _OWN
