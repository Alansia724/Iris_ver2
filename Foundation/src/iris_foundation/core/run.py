"""运行会话与运行记录（docs/architecture.md §15）——一次运行是一等实体。

产品层面的单位不是"某台设备"，而是**一次运行**。本模块给它名字、状态、
溯源与索引，并收口全平台的 HDF5 落盘路径：

  1. 命名：``<prefix>[_<label>]_<ts>.h5``（唯一化经 storage.unique_path）
  2. 根 attrs：schema_version/source/created_at（经 storage.init_frame_file）
  3. 溯源：run_id/操作者/起止/状态/原因 + 设备清单 + 设备参数快照
  4. 运行索引：``<产物目录>/runs.jsonl`` 追加一行（数据目录从此可查询）

零 Qt（只依赖 h5py + 标准库），GUI/CLI/agent 共用；将来进程外/远程同样复用。

纪律：**溯源收集永不抛异常**——参数读不到就跳过。记录不能因为读不到某个
只读参数而让一次真实采集失败。
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 运行记录 schema（与数据 schema_version 独立演进）
RUN_SCHEMA = "run/1"

#: 运行索引文件名（落在产物目录，追加式）
RUN_INDEX_NAME = "runs.jsonl"

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ABORTED = "aborted"
STATUS_FAILED = "failed"


# ---------------------------------------------------------------- 身份与快照

def new_run_id() -> str:
    """运行 id：本地时间 + 随机后缀（同秒并发亦不重）。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def operator_name() -> str:
    """操作者名（取不到 = unknown，绝不抛）。"""
    for getter in (getpass.getuser,
                   lambda: os.environ.get("USERNAME"),
                   lambda: os.environ.get("USER")):
        try:
            name = getter()
        except Exception:
            continue
        if name:
            return str(name)
    return "unknown"


def rig_snapshot(devices: dict | None = None) -> list[dict]:
    """设备清单快照（§15.3）：身份/驱动/合同版本/能力/流 + 在不在。

    来自注册表声明（§6 驱动 manifest）——**不做任何设备调用**，注册表有的
    就入档。``devices`` 只用于标注 present（``{dev_id: 实例|None}``）。
    注册表不可用（远程源离线等）时返回空表，不向上抛。
    """
    from iris_foundation.core import registry
    try:
        manifests = registry.manifests()
    except Exception:
        logger.debug("设备清单快照失败（注册表不可用）", exc_info=True)
        return []
    present = dict(devices or {})
    out: list[dict] = []
    for m in manifests:
        out.append({
            "dev_id": m.dev_id,
            "driver": m.driver,
            "model": m.model,
            "contract_version": m.contract_version,
            "capabilities": list(m.capabilities),
            "streams": [s.name for s in m.streams],
            "present": present.get(m.dev_id) is not None,
        })
    return out


def param_snapshot(devices: dict | None = None) -> dict[str, dict]:
    """设备参数值快照（§15.3）：按驱动 ``params()`` 声明逐条回读。

    数据自证的关键——曝光/增益/波长/位置随文件入档，不靠人记。
    读失败的参数直接跳过（只读参数不可回读，不是运行失败的理由）。
    """
    from iris_foundation.core import registry
    from iris_foundation.core.device_services import reader
    out: dict[str, dict] = {}
    for dev_id, dev in (devices or {}).items():
        if dev is None:
            continue
        try:
            descs = registry.declared_params(dev_id)
        except Exception:
            logger.debug("参数声明不可用：%s", dev_id, exc_info=True)
            continue
        values: dict[str, float] = {}
        # 按 key 排序：attrs 稳定可比（注册表顺序不保证）
        for key in sorted(descs):
            value = reader(dev, descs[key])
            if value is not None:
                values[key] = value
        out[dev_id] = values
    return out


def rig_of(services) -> dict:
    """从注入服务字典取设备实例表（约定键 ``rig``，值为 ``Rig``）。

    装配方（``lab_bench._inject_services`` / ``BenchServices.scan_services``）
    注入 ``lambda: rig``；这里只认这一个形状。取不到按空表——溯源不能因为
    拿不到设备表而让一次真实采集失败。
    """
    getter = (services or {}).get("rig")
    if getter is None:
        return {}
    try:
        return dict(getter().devices)
    except Exception:
        logger.debug("rig 服务键取值失败", exc_info=True)
        return {}


# ---------------------------------------------------------------- 运行记录

@dataclass
class RunRecord:
    """一次运行的记录（§15.2）。索引行 = 本结构（不含 rig/params）。"""

    run_id: str
    kind: str                       # "scan" | "single" | ...（怎么跑起来的）
    status: str = STATUS_RUNNING
    source: str = ""                # 数据主题 camera.frame
    measurement: str = ""           # 产出方名（测量 id / 设备视图名 / CLI 模式）
    label: str = ""                 # 用户可见名
    operator: str = ""
    started_at: str = ""
    finished_at: str = ""
    reason: str = ""                # 结束原因（引擎 reason 原样）
    file: str = ""                  # 产物文件名
    devices: tuple[str, ...] = ()   # 参与的设备

    def as_index_entry(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 记录器

class RunRecorder:
    """一次运行的记录器 + 落盘唯一入口（§15.1）。

    用法（打开→写数据→收尾）::

        run = RunRecorder(kind="single", source="camera.frame",
                          measurement="image", out_dir=DATA_DIR,
                          label="Image", devices=rig_of(services))
        run.open()                      # 建文件 + 根 attrs + 溯源
        FrameStackWriter(run.file, 1).append(frame)
        run.finish()                    # 结束事实 + 索引行 + 关文件

    或上下文形式（异常自动记 failed，异常照常上抛）::

        with RunRecorder(...) as run:
            FrameStackWriter(run.file, 1).append(frame)

    路径二选一：``path``（调用方定；引擎测试路径用这个）或
    ``out_dir`` + 命名规则。两处给路径即漂移，故给 ``path`` 时不改其名。
    """

    def __init__(self, *, kind: str, source: str = "", measurement: str = "",
                 label: str = "", out_dir: Path | str | None = None,
                 prefix: str | None = None, path: Path | str | None = None,
                 devices: dict | None = None, attrs: dict | None = None,
                 index: bool = True, snapshot: bool = True):
        self.record = RunRecord(
            run_id=new_run_id(), kind=str(kind), status=STATUS_RUNNING,
            source=str(source), measurement=str(measurement),
            label=str(label), operator=operator_name(),
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._prefix = prefix or kind
        self._attrs = dict(attrs or {})
        self._index = bool(index)
        self._devices = {k: v for k, v in (devices or {}).items() if v is not None}
        self._rig = rig_snapshot(devices) if snapshot else []
        self._params = param_snapshot(self._devices) if snapshot else {}
        self._h5 = None
        self._finished = False

        if path is not None:
            self.path = Path(path)          # 调用方已定名（引擎既有路径）
        else:
            from iris_foundation.core.storage import sanitize_name, unique_path
            base = Path(out_dir) if out_dir is not None else Path.cwd()
            ts = time.strftime("%Y%m%d_%H%M%S")
            stem = sanitize_name(self._prefix)
            lab = sanitize_name(label) if label else ""
            name = f"{stem}_{lab}_{ts}" if lab else f"{stem}_{ts}"
            self.path = unique_path(base / f"{name}.h5")
        self.record.file = self.path.name
        self.record.devices = tuple(sorted(self._devices))

    # ---- 打开 / 写 / 收尾 ----

    def _root_attrs(self) -> dict:
        """根 attrs：运行身份 + 溯源（其余由调用方 attrs 补）。"""
        out = {
            "run_schema": RUN_SCHEMA,
            "run_id": self.record.run_id,
            "run_kind": self.record.kind,
            "operator": self.record.operator,
            "started_at": self.record.started_at,
            "status": STATUS_RUNNING,
        }
        if self.record.measurement:
            out["measurement"] = self.record.measurement
        if self.record.label:
            out["label"] = self.record.label
        if self._rig:
            out["rig_json"] = json.dumps(self._rig, ensure_ascii=False,
                                         sort_keys=True)
        if self._params:
            out["params_json"] = json.dumps(self._params, ensure_ascii=False,
                                            sort_keys=True)
        out.update(self._attrs)
        return out

    def open(self, mode: str = "w"):
        """建/开产物文件并写根 attrs（仅 ``w`` 时写）。返回 h5py.File。"""
        import h5py

        from iris_foundation.core.storage import init_frame_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._h5 = h5py.File(self.path, mode)
        if mode == "w":
            init_frame_file(self._h5, self.record.source, **self._root_attrs())
        return self._h5

    @property
    def file(self):
        """当前 h5py.File（未打开则按 ``w`` 打开）。"""
        if self._h5 is None:
            self.open()
        return self._h5

    @property
    def attrs(self):
        return self.file.attrs

    def finish(self, status: str = STATUS_OK, reason: str = "",
               **extra) -> RunRecord:
        """收尾：写结束事实 → 关文件 → 追加索引行（幂等）。"""
        if self._finished:
            return self.record
        self._finished = True
        self.record.status = str(status)
        self.record.reason = str(reason or "")
        self.record.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._h5 is not None:
            try:
                a = self._h5.attrs
                a["status"] = self.record.status
                a["finished_at"] = self.record.finished_at
                if self.record.reason:
                    a["reason"] = self.record.reason
                for key, value in extra.items():
                    if value is not None:
                        a[key] = value
                self._h5.flush()
            except Exception:
                logger.warning("运行收尾写 attrs 失败：%s", self.path,
                               exc_info=True)
            finally:
                try:
                    self._h5.close()
                except Exception:
                    logger.warning("关闭 HDF5 失败：%s", self.path, exc_info=True)
                self._h5 = None
        self._append_index()
        return self.record

    def abort(self, reason: str = "", **extra) -> RunRecord:
        return self.finish(STATUS_ABORTED, reason, **extra)

    def fail(self, reason: str = "", **extra) -> RunRecord:
        return self.finish(STATUS_FAILED, reason, **extra)

    def _append_index(self) -> None:
        """追加一行索引（失败只记日志：索引是便利，不是合同）。"""
        if not self._index:
            return
        try:
            line = json.dumps(self.record.as_index_entry(), ensure_ascii=False,
                              sort_keys=True)
            with open(self.path.parent / RUN_INDEX_NAME, "a",
                      encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.warning("运行索引写入失败：%s",
                           self.path.parent / RUN_INDEX_NAME, exc_info=True)

    # ---- 上下文形式 ----

    def __enter__(self) -> "RunRecorder":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.finish(STATUS_OK)
        else:
            self.finish(STATUS_FAILED, f"{exc_type.__name__}: {exc}")
        return False


# ---------------------------------------------------------------- 索引读取

def load_index(out_dir: Path | str) -> list[dict]:
    """读回运行索引（坏行跳过——手工编辑/半行写入不该让查询崩）。"""
    path = Path(out_dir) / RUN_INDEX_NAME
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    logger.debug("运行索引坏行跳过：%s", line[:120])
    except OSError:
        logger.warning("运行索引读取失败：%s", path, exc_info=True)
    return out
