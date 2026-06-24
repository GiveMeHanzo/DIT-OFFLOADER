"""后端流水线与 Qt UI 的线程桥接。"""
from __future__ import annotations

import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from config import (
    FileStatus,
    NameConflictPolicy,
    PipelineConfig,
    log_xml_path,
)
from core.logger import (
    ExistingLogInfo,
    FileRecord,
    JobLogger,
    find_existing_logs,
)
from core.pipeline import CopyPipeline, PipelineResult, ProgressEvent
from core.scanner import FileTask, scan_sources
from core.verifier import hash_file, verify_pair


# ── 日志路径解析（场记重命名后的目标路径匹配）──

def _resolve_dest_from_logs(dest_root: str) -> dict[str, str]:
    """扫描目标目录中的 *_log.xml，构建 {normcase(src): dest} 映射。

    用于「仅校验」时解析因场记重命名而变更的目标文件路径。
    如果同一 src 出现在多份日志中，后解析的覆盖前者。
    """
    mapping: dict[str, str] = {}
    if not os.path.isdir(dest_root):
        return mapping
    for entry in os.listdir(dest_root):
        if not entry.endswith("_log.xml"):
            continue
        xml_path = os.path.join(dest_root, entry)
        try:
            tree = ET.parse(xml_path)
            for fe in tree.getroot().findall("files/file"):
                src = fe.get("src", "")
                dest = fe.get("dest", "")
                if src and dest:
                    mapping[os.path.normcase(src)] = dest
        except Exception:
            continue
    return mapping


@dataclass
class FileProgress:
    """单个文件的一次进度更新。"""
    src: str
    dest: str
    status: str
    bytes_done: int = 0
    message: Optional[str] = None


@dataclass
class JobSummary:
    """Job 结束汇总。"""
    job_name: str
    dest_root: str
    verified: int
    failed: int
    skipped: int
    bytes_total: int
    bytes_verified: int
    elapsed: float
    aborted: bool
    generate_report: bool
    xml_path: str


# ── 扫描 worker ──
@dataclass
class ScanResult:
    dest_root: str
    existing: list


class ScanWorker(QObject):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, dest_root: str) -> None:
        super().__init__()
        self.dest_root = dest_root

    @Slot()
    def run(self) -> None:
        try:
            existing = find_existing_logs(self.dest_root)
            self.finished_ok.emit(ScanResult(
                dest_root=self.dest_root, existing=existing))
        except Exception as e:
            self.failed.emit(str(e))


# ── 拷贝 worker ──
class CopyJobWorker(QObject):
    job_started = Signal(str)
    file_started = Signal(object)
    file_done = Signal(object)
    bytes_progress = Signal(object, object, str)
    job_finished = Signal(object)
    error = Signal(str, str)

    def __init__(
        self,
        job_name: str,
        sources: list[str],
        dest_root: str,
        conflict_policy: NameConflictPolicy = NameConflictPolicy.KEEP,
        generate_report: bool = False,
        config: Optional[PipelineConfig] = None,
        resume_from: Optional[ExistingLogInfo] = None,
    ) -> None:
        super().__init__()
        self.job_name = job_name
        self.sources = sources
        self.dest_root = dest_root
        self.conflict_policy = conflict_policy
        self.generate_report = generate_report
        self.config = config or PipelineConfig()
        self.resume_from = resume_from

        self._pipeline: Optional[CopyPipeline] = None
        self._bytes_total = 0
        # _on_progress 被多个 pipeline 拷贝/校验线程并发调用，节流计数器需加锁保护
        self._lock = threading.Lock()
        self._last_bytes_emit = 0.0

    @Slot()
    def run(self) -> None:
        try:
            self._execute()
        except Exception as e:
            self.error.emit("", f"作业执行失败: {e}")

    def _execute(self) -> None:
        self.job_started.emit(self.job_name)

        all_tasks = scan_sources(self.sources)
        if not all_tasks:
            self.error.emit("", "未找到任何可拷贝文件，请检查源路径。")
            return

        self._bytes_total = sum(t.size for t in all_tasks)

        skip_srcs: set[str] = set()
        if self.resume_from is not None:
            verified = [f for f in self.resume_from.job.files
                        if f.status == FileStatus.VERIFIED.value]
            skip_srcs = {f.src for f in verified}
            logger = JobLogger(self.resume_from.xml_path, self.resume_from.job)
            tasks_to_run = all_tasks
        else:
            os.makedirs(self.dest_root, exist_ok=True)
            xml_path = log_xml_path(self.dest_root, self.job_name)
            initial = [
                FileRecord(src=t.src, dest=t.dest_under(self.dest_root),
                           size=t.size)
                for t in all_tasks
            ]
            logger = JobLogger.create_new(
                xml_path, self.job_name,
                [(s, self.dest_root) for s in self.sources], initial,
            )
            tasks_to_run = all_tasks

        self._pipeline = CopyPipeline(
            tasks=tasks_to_run, dest_root=self.dest_root, logger=logger,
            config=self.config, conflict_policy=self.conflict_policy,
            on_progress=self._on_progress, skip_srcs=skip_srcs,
        )
        result: PipelineResult = self._pipeline.run()

        summary = JobSummary(
            job_name=self.job_name, dest_root=self.dest_root,
            verified=result.verified_files, failed=result.failed_files,
            skipped=result.skipped_files,
            bytes_total=result.bytes_total, bytes_verified=result.bytes_verified,
            elapsed=result.elapsed, aborted=result.aborted,
            generate_report=self.generate_report and not result.aborted,
            xml_path=log_xml_path(self.dest_root, self.job_name),
        )
        self.job_finished.emit(summary)

    def _on_progress(self, ev: ProgressEvent) -> None:
        # 对 bytes 事件做节流：最多每 80ms 发射一次信号
        # 避免大文件拷贝时成千上万个 QueuedConnection 事件淹没主线程
        if ev.kind == "bytes":
            now = time.monotonic()
            # pipeline 的多个拷贝/校验线程会并发触发本回调，节流计数器需加锁
            with self._lock:
                if now - self._last_bytes_emit < 0.08:
                    return  # 跳过此次发射
                self._last_bytes_emit = now

        if ev.kind == "file_started":
            self.file_started.emit(FileProgress(
                src=ev.src or "", dest=ev.dest or "",
                status=ev.status or "", bytes_done=ev.bytes_total,
            ))
        elif ev.kind == "file_done":
            self.file_done.emit(FileProgress(
                src=ev.src or "", dest=ev.dest or "",
                status=ev.status or "", bytes_done=ev.bytes_total,
                message=ev.message,
            ))
        elif ev.kind == "bytes":
            self.bytes_progress.emit(ev.bytes_total, self._bytes_total,
                                     ev.src or "")
        elif ev.kind == "error":
            self.error.emit(ev.src or "", ev.message or "")

    @Slot()
    def request_cancel(self) -> None:
        if self._pipeline is not None:
            self._pipeline.cancel()


# ── 仅校验 worker ──
@dataclass
class VerifyOnlyResult:
    job_name: str
    dest_root: str
    total_in_source: int
    found_in_dest: int
    verified: int
    mismatched: int
    missing: int
    bytes_total: int
    bytes_done: int
    elapsed: float
    aborted: bool
    generate_report: bool
    xml_path: str
    mismatched_files: list = field(default_factory=list)


class VerifyOnlyWorker(QObject):
    job_started = Signal(str)
    file_started = Signal(object)
    bytes_progress = Signal(object, object, str)
    file_done = Signal(object)
    job_finished = Signal(object)
    error = Signal(str, str)

    def __init__(
        self,
        job_name: str,
        sources: list[str],
        dest_root: str,
        generate_report: bool = False,
        config: Optional[PipelineConfig] = None,
    ) -> None:
        super().__init__()
        self.job_name = job_name
        self.sources = sources
        self.dest_root = dest_root
        self.generate_report = generate_report
        self.config = config or PipelineConfig()
        self._cancel = threading.Event()
        self._bytes_total = 0
        # 节流字节进度信号（拷贝模式用同样的 80ms 节流，参考 CopyJobWorker）
        self._progress_lock = threading.Lock()
        self._last_progress_emit = 0.0

    @Slot()
    def run(self) -> None:
        try:
            self._execute()
        except Exception as e:
            self.error.emit("", f"校验作业失败: {e}")

    @Slot()
    def request_cancel(self) -> None:
        self._cancel.set()

    def _execute(self) -> None:
        start = time.monotonic()
        self.job_started.emit(self.job_name)

        tasks = scan_sources(self.sources)
        if not tasks:
            self.error.emit("", "未找到任何源文件，无法校验。")
            return

        self._bytes_total = sum(t.size for t in tasks)
        os.makedirs(self.dest_root, exist_ok=True)

        # 读取目标目录中已有的日志，建立 src→实际dest 映射
        # 这样即使拷贝后执行了场记重命名，也能正确定位目标文件
        log_dest_map = _resolve_dest_from_logs(self.dest_root)

        xml_path = log_xml_path(self.dest_root, self.job_name)
        initial = [
            FileRecord(
                src=t.src,
                dest=log_dest_map.get(
                    os.path.normcase(t.src), t.dest_under(self.dest_root)
                ),
                size=t.size,
            )
            for t in tasks
        ]
        logger = JobLogger.create_new(
            xml_path, self.job_name,
            [(s, self.dest_root) for s in self.sources], initial,
        )

        verified = mismatched = missing = found = 0
        bytes_done = 0
        bad_files: list[str] = []

        # ── 进度上报（拷贝模式用 80ms 节流的 bytes_progress；这里复刻同样方案） ──
        # 每个文件需哈希源 + 目标两份，原始累计会超过实际体积，除以 2 归一化。
        self._verify_chunk_acc = 0   # 当前文件的原始 chunk 累计（src+dest 各累加）

        def _on_chunk(n: int) -> None:
            """每次 1MiB 读取触发，节流发射 bytes_progress 驱动进度条。"""
            self._verify_chunk_acc += n
            now = time.monotonic()
            with self._progress_lock:
                if now - self._last_progress_emit < 0.08:
                    return
                self._last_progress_emit = now
            # 归一化：同时读 src 和 dest，两份都累进 _verify_chunk_acc，÷2 得实际进度
            done = bytes_done + self._verify_chunk_acc // 2
            self.bytes_progress.emit(min(done, self._bytes_total), self._bytes_total, "")

        for task in tasks:
            if self._cancel.is_set():
                break
            # 优先从已有日志映射中获取真实目标路径（含重命名后的路径）
            dest_path = log_dest_map.get(
                os.path.normcase(task.src), task.dest_under(self.dest_root)
            )
            # ── 文件开始，进度条反映已完成的字节 ──
            self.bytes_progress.emit(bytes_done, self._bytes_total, "")
            self.file_started.emit(FileProgress(
                src=task.src, dest=dest_path, status="verifying",
                bytes_done=bytes_done,
            ))
            logger.set_file_status(task.src, FileStatus.VERIFYING.value)

            if not os.path.exists(dest_path):
                missing += 1
                bad_files.append(task.src)
                bytes_done += task.size
                self.bytes_progress.emit(bytes_done, self._bytes_total, "")
                logger.update_file(FileRecord(
                    src=task.src, dest=dest_path, size=task.size,
                    status=FileStatus.FAILED.value, error="目标文件缺失",
                ))
                self.file_done.emit(FileProgress(
                    src=task.src, dest=dest_path,
                    status=FileStatus.FAILED.value, bytes_done=bytes_done,
                    message="目标文件缺失",
                ))
                continue

            found += 1
            try:
                # 重置当前文件的 chunk 计数器，进度回调会把 hash_file 的每次
                # 1MiB 读取通过节流后的 bytes_progress 推送到进度条。
                self._verify_chunk_acc = 0
                h_src = hash_file(task.src,
                                  buffer_size=self.config.hash_buffer_size,
                                  progress_cb=_on_chunk)
                h_dest = hash_file(dest_path,
                                   buffer_size=self.config.hash_buffer_size,
                                   progress_cb=_on_chunk)
            except OSError as e:
                mismatched += 1
                bad_files.append(task.src)
                bytes_done += task.size
                self.bytes_progress.emit(bytes_done, self._bytes_total, "")
                logger.update_file(FileRecord(
                    src=task.src, dest=dest_path, size=task.size,
                    status=FileStatus.FAILED.value, error=f"读取失败: {e}",
                ))
                self.file_done.emit(FileProgress(
                    src=task.src, dest=dest_path,
                    status=FileStatus.FAILED.value, bytes_done=bytes_done,
                    message=f"读取失败: {e}",
                ))
                continue

            ok = verify_pair(h_src, h_dest)
            bytes_done += task.size
            # ── 文件完成，进度条跳到当前累计 ──
            self.bytes_progress.emit(bytes_done, self._bytes_total, "")
            if ok:
                verified += 1
                logger.update_file(FileRecord(
                    src=task.src, dest=dest_path, size=task.size,
                    status=FileStatus.VERIFIED.value,
                    hash_src=h_src, hash_dest=h_dest, verified="true",
                ))
                self.file_done.emit(FileProgress(
                    src=task.src, dest=dest_path,
                    status=FileStatus.VERIFIED.value, bytes_done=bytes_done,
                ))
            else:
                mismatched += 1
                bad_files.append(task.src)
                logger.update_file(FileRecord(
                    src=task.src, dest=dest_path, size=task.size,
                    status=FileStatus.FAILED.value,
                    hash_src=h_src, hash_dest=h_dest, verified="false",
                    error="hash 不一致",
                ))
                self.file_done.emit(FileProgress(
                    src=task.src, dest=dest_path,
                    status=FileStatus.FAILED.value, bytes_done=bytes_done,
                    message="hash 不一致",
                ))

        elapsed = time.monotonic() - start
        aborted = self._cancel.is_set()
        if not aborted:
            logger.mark_completed()
        else:
            logger.mark_aborted()

        result = VerifyOnlyResult(
            job_name=self.job_name, dest_root=self.dest_root,
            total_in_source=len(tasks), found_in_dest=found,
            verified=verified, mismatched=mismatched, missing=missing,
            bytes_total=self._bytes_total, bytes_done=bytes_done,
            elapsed=elapsed, aborted=aborted,
            generate_report=self.generate_report and not aborted,
            xml_path=xml_path, mismatched_files=bad_files,
        )
        self.job_finished.emit(result)


# ── 报告生成 worker ──
class ReportWorker(QObject):
    """在后台线程生成 HTML 报告，避免阻塞 GUI 主线程。

    与 CopyJobWorker / VerifyOnlyWorker 同样走 WorkerThread 模式：
    main_window 创建后 moveToThread，通过信号在主线程槽里收尾。
    """

    finished = Signal(str)   # html_path
    failed = Signal(str)     # 错误信息

    def __init__(
        self,
        summary: object,
        sources: list[str],
        renamed_map: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__()
        self.summary = summary
        self.sources = sources
        self.renamed_map = renamed_map

    @Slot()
    def run(self) -> None:
        try:
            # 延迟导入：避免在模块加载阶段引入报告依赖链
            from report.generator import generate_html_report
            path = generate_html_report(
                self.summary, sources=self.sources,
                renamed_map=self.renamed_map,
            )
            self.finished.emit(path)
        except Exception as e:  # noqa: BLE001 - 报告失败不应让整个流程崩溃
            self.failed.emit(str(e))


# ── 线程持有器 ──
class WorkerThread(QThread):
    """绑定 worker 线程。主线程在 _on_job_finished 末尾手动 quit()。"""

    def __init__(self, worker: QObject) -> None:
        super().__init__()
        self.worker = worker
        self.worker.moveToThread(self)
        self.started.connect(self.worker.run)
        if isinstance(worker, CopyJobWorker):
            # job_finished 不再自动 quit —— 由主线程在报告/重命名完成后显式 quit
            worker.error.connect(self.quit)
        elif isinstance(worker, ScanWorker):
            worker.finished_ok.connect(self.quit)
            worker.failed.connect(self.quit)
        elif isinstance(worker, VerifyOnlyWorker):
            # 同上，job_finished 不再自动 quit
            worker.error.connect(self.quit)
        elif isinstance(worker, ReportWorker):
            # 报告完成/失败后自动退出本线程（主线程通过信号槽收尾 UI）
            worker.finished.connect(self.quit)
            worker.failed.connect(self.quit)


__all__ = [
    "FileProgress", "JobSummary", "ScanResult", "ScanWorker",
    "CopyJobWorker", "VerifyOnlyResult", "VerifyOnlyWorker",
    "ReportWorker", "WorkerThread",
]
