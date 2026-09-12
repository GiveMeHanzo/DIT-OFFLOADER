"""XML 日志与断点续传（核心防灾机制）。

XML 是整个软件的「单点真相」：
- Start 时立即写入完整待办清单（status=pending）。
- 每个文件拷贝/校验完成，原子地更新其节点（先写临时文件再替换，避免半写损坏）。
- Job 结束写 ``<status>Completed</status>``。
- 再次 Start 时扫描目标目录：无 Completed → 断点续传；有 Completed → 追加备份提示。

为什么用 XML 而非 JSON/SQLite？
- 人类可读、易调试；增量追加/改写单节点直观。
- 单文件、无外部依赖。
- 解析容错：用 ElementTree，遇到损坏的尾部可尝试修复后重读。

线程模型：
- logger 内部用一把可重入锁串行化所有写操作。
- pipeline 的「日志线程」是唯一写者，但我们也加锁以防 GUI 线程读取时竞争。
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import FileStatus, JobStatus, XML_LOG_SUFFIX

# ElementTree 默认无 namespace，这里直接用裸标签


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    """XML 中单个 <file> 节点的内存表示。"""
    src: str
    dest: str
    size: int
    status: str = FileStatus.PENDING.value
    hash_src: Optional[str] = None
    hash_dest: Optional[str] = None
    verified: Optional[str] = None   # "true"/"false"
    ts: Optional[str] = None         # 最后更新时间 ISO
    error: Optional[str] = None      # 失败原因


@dataclass
class JobLog:
    """整个 Job 的 XML 日志内存表示。"""
    name: str
    started: str
    status: str = JobStatus.RUNNING.value
    finished: Optional[str] = None
    sources: list[tuple[str, str]] = field(default_factory=list)  # (path, dest_root)
    files: list[FileRecord] = field(default_factory=list)

    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def verified_bytes(self) -> int:
        return sum(f.size for f in self.files if f.status == FileStatus.VERIFIED.value)

    def is_completed(self) -> bool:
        return self.status == JobStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

# 落盘必须即时的状态（防灾语义：断电/崩溃后这些记录不能丢；
# 中间态丢了只会让断点续传重拷该文件，可以按 200ms 节流合并）
_DURABLE_STATUSES: frozenset[str] = frozenset(
    {
        FileStatus.VERIFIED.value,
        FileStatus.FAILED.value,
        FileStatus.SKIPPED.value,
    }
)


class JobLogger:
    """XML 日志的读写门面，线程安全。

    用法：
        log = JobLogger.create_new(xml_path, name, sources, files)
        log.update_file(record)        # 更新某文件状态
        log.mark_completed()           # 标记整个 Job 完成
        log.mark_aborted()

    写入节流（1.0.4）：每次状态变化都全量重写 XML，万级文件的作业会产生
    O(n²) 写放大。现在按 ``_WRITE_MIN_INTERVAL`` 合并低价值的中间状态更新
    （copying/copied/verifying——丢了只会导致断点续传重拷该文件）；
    mark_* 与终态（verified/failed/skipped）始终立即强制落盘（防灾语义：
    断电/崩溃后不能丢「已验证」记录）。序列化始终基于内存中最新的
    self.job，因此任何一次落盘都会带出全部已提交状态。
    """

    # 中间状态两次落盘的最小间隔（秒）
    _WRITE_MIN_INTERVAL = 0.2

    def __init__(self, xml_path: str, job: JobLog) -> None:
        self.xml_path = xml_path
        self.job = job
        self._lock = threading.RLock()
        self._last_write = 0.0
        self._write(force=True)  # 初次落盘

    # ---- 工厂方法 ----
    @classmethod
    def create_new(
        cls,
        xml_path: str,
        name: str,
        sources: list[tuple[str, str]],
        files: list[FileRecord],
    ) -> "JobLogger":
        job = JobLog(
            name=name,
            started=datetime.now().isoformat(timespec="seconds"),
            status=JobStatus.RUNNING.value,
            sources=list(sources),
            files=[_clone_pending(f) for f in files],
        )
        return cls(xml_path, job)

    # ---- 更新接口（pipeline 调用）----
    def update_file(self, record: FileRecord) -> None:
        """更新或追加一条文件记录（按 src 匹配）。"""
        with self._lock:
            record.ts = datetime.now().isoformat(timespec="seconds")
            for i, f in enumerate(self.job.files):
                if os.path.normcase(f.src) == os.path.normcase(record.src):
                    self.job.files[i] = record
                    self._write(force=record.status in _DURABLE_STATUSES)
                    return
            # 未找到则追加
            self.job.files.append(record)
            self._write(force=record.status in _DURABLE_STATUSES)

    def set_file_status(self, src: str, status: str, **extra: object) -> None:
        """便捷：仅更新某文件状态及附加字段。"""
        with self._lock:
            for f in self.job.files:
                if os.path.normcase(f.src) == os.path.normcase(src):
                    f.status = status
                    f.ts = datetime.now().isoformat(timespec="seconds")
                    for k, v in extra.items():
                        if hasattr(f, k):
                            setattr(f, k, v)
                    self._write(force=status in _DURABLE_STATUSES)
                    return

    def mark_completed(self) -> None:
        with self._lock:
            self.job.status = JobStatus.COMPLETED.value
            self.job.finished = datetime.now().isoformat(timespec="seconds")
            self._write(force=True)

    def mark_aborted(self) -> None:
        with self._lock:
            self.job.status = JobStatus.ABORTED.value
            self.job.finished = datetime.now().isoformat(timespec="seconds")
            self._write(force=True)

    def mark_error(self, message: str = "") -> None:
        with self._lock:
            self.job.status = JobStatus.ERROR.value
            self.job.finished = datetime.now().isoformat(timespec="seconds")
            self._write(force=True)

    # ---- 序列化 ----
    def _write(self, *, force: bool = False) -> None:
        """原子写：先写 .tmp 再替换，防止半写损坏。

        非强制模式下按 _WRITE_MIN_INTERVAL 节流：距离上次落盘太近时跳过。
        序列化源是内存中的 self.job，后续任何一次落盘（含 mark_* 强制写）
        都会带出最新状态，故节流不丢数据。
        """
        if not force:
            now = time.monotonic()
            if now - self._last_write < self._WRITE_MIN_INTERVAL:
                return
            self._last_write = now
        else:
            self._last_write = time.monotonic()

        root = ET.Element("job")
        root.set("name", self.job.name)
        root.set("started", self.job.started)
        root.set("status", self.job.status)
        if self.job.finished:
            root.set("finished", self.job.finished)

        srcs_el = ET.SubElement(root, "sources")
        for path, dest_root in self.job.sources:
            s = ET.SubElement(srcs_el, "source")
            s.set("path", path)
            s.set("dest", dest_root)

        files_el = ET.SubElement(root, "files")
        files_el.set("count", str(len(self.job.files)))
        for f in self.job.files:
            fe = ET.SubElement(files_el, "file")
            fe.set("src", f.src)
            fe.set("dest", f.dest)
            fe.set("size", str(f.size))
            fe.set("status", f.status)
            if f.hash_src:
                fe.set("hash_src", f.hash_src)
            if f.hash_dest:
                fe.set("hash_dest", f.hash_dest)
            if f.verified:
                fe.set("verified", f.verified)
            if f.ts:
                fe.set("ts", f.ts)
            if f.error:
                fe.set("error", f.error)

        tree = ET.ElementTree(root)
        # 缩进（Python 3.9+）
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass

        tmp = self.xml_path + ".tmp"
        # 用 ASCII 编码 + xml 声明，避免中文路径乱码问题
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        # Windows 上 os.replace 可跨文件系统原子替换
        os.replace(tmp, self.xml_path)


# ---------------------------------------------------------------------------
# 读取 / 断点续传扫描
# ---------------------------------------------------------------------------

@dataclass
class ExistingLogInfo:
    """扫描目标目录得到的既有日志信息，供 UI 决定如何提示用户。"""
    xml_path: str
    job: JobLog
    is_completed: bool
    pending_files: list[FileRecord]   # 未校验完的文件（断点续传候选）


def find_existing_logs(dest_root: str) -> list[ExistingLogInfo]:
    """扫描目标目录下所有 ``*_log.xml``，返回解析结果列表。"""
    results: list[ExistingLogInfo] = []
    if not os.path.isdir(dest_root):
        return results
    for entry in os.listdir(dest_root):
        if not entry.endswith(XML_LOG_SUFFIX):
            continue
        xml_path = os.path.join(dest_root, entry)
        info = load_log(xml_path)
        if info is not None:
            results.append(info)
    return results


def load_log(xml_path: str) -> Optional[ExistingLogInfo]:
    """读取并解析一个 XML 日志，返回 ExistingLogInfo；损坏则返回 None。"""
    if not os.path.isfile(xml_path):
        return None
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        # 尾部损坏（崩溃时半写）：尝试截断式修复
        job = _try_recover_partial(xml_path)
        if job is None:
            return None
    else:
        job = _parse_tree(tree.getroot())
        if job is None:
            return None

    pending = [
        f for f in job.files
        if f.status not in (FileStatus.VERIFIED.value, FileStatus.SKIPPED.value)
    ]
    return ExistingLogInfo(
        xml_path=xml_path,
        job=job,
        is_completed=(job.status == JobStatus.COMPLETED.value),
        pending_files=pending,
    )


def _safe_int(value: Optional[str], default: int = 0) -> int:
    """把 XML 属性安全转成 int。

    日志可能被外部工具改写、或极端半写下产生非数字字段（如 size="abc"）。
    旧版直接 int() 会抛 ValueError 击穿 load_log → ScanWorker，导致「扫描失败」
    且任务完全无法启动。这里容错为 default，使日志仍可解析、仍能断点续传。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_tree(root: ET.Element) -> Optional[JobLog]:
    if root.tag != "job":
        return None
    job = JobLog(
        name=root.get("name", ""),
        started=root.get("started", ""),
        status=root.get("status", JobStatus.RUNNING.value),
        finished=root.get("finished"),
    )
    srcs = root.find("sources")
    if srcs is not None:
        for s in srcs.findall("source"):
            job.sources.append((s.get("path", ""), s.get("dest", "")))
    files_el = root.find("files")
    if files_el is not None:
        for fe in files_el.findall("file"):
            job.files.append(FileRecord(
                src=fe.get("src", ""),
                dest=fe.get("dest", ""),
                size=_safe_int(fe.get("size", "0")),
                status=fe.get("status", FileStatus.PENDING.value),
                hash_src=fe.get("hash_src"),
                hash_dest=fe.get("hash_dest"),
                verified=fe.get("verified"),
                ts=fe.get("ts"),
                error=fe.get("error"),
            ))
    return job


def _try_recover_partial(xml_path: str) -> Optional[JobLog]:
    """尾部损坏时尝试修复：在最后一个完整的 </file> 处截断并补全闭合标签。"""
    try:
        with open(xml_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    # 截到最后一个 </file>
    cut = text.rfind("</file>")
    if cut < 0:
        return None
    repaired = text[: cut + len("</file>")] + "\n  </files>\n</job>"
    tmp = xml_path + ".recovered"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(repaired)
        tree = ET.parse(tmp)
        # 备份原损坏文件
        shutil.move(xml_path, xml_path + ".corrupt")
        os.replace(tmp, xml_path)
        return _parse_tree(tree.getroot())
    except (ET.ParseError, OSError, ValueError):
        return None


def _clone_pending(f: FileRecord) -> FileRecord:
    """创建初始 pending 记录（剥离已有 hash/status）。"""
    return FileRecord(src=f.src, dest=f.dest, size=f.size, status=FileStatus.PENDING.value)


__all__ = [
    "FileRecord",
    "JobLog",
    "JobLogger",
    "ExistingLogInfo",
    "find_existing_logs",
    "load_log",
]
