"""流水线编排：拷贝与校验并行执行。

实现需求中「拷贝 B 的同时校验 A」的核心机制。采用生产者-消费者模型：

    [枚举/预计算源hash]
            │ copy_queue
            ▼
     ┌──────────────────┐
     │ 拷贝线程池(N)     │  ── shutil.copystat 保留元数据
     └──────────────────┘
            │ verify_queue
            ▼
     ┌──────────────────┐
     │ 校验线程池(M)     │  ── 目标 hash，与预计算的源 hash 比对
     └──────────────────┘
            │
            ▼
     [单线程日志更新]      ── 原子写 XML，逐文件 flush

设计要点：
- **背压**：copy_queue / verify_queue 有界，防止内存膨胀。
- **取消**：通过 threading.Event 实现；线程在每个文件开始前检查。
- **进度**：通过回调上报「字节级」与「文件级」两种进度，UI 可任选。
- **错误隔离**：单个文件失败不影响其余文件；失败原因写入 XML 的 error 属性。
- **回调线程安全**：所有回调在调用方线程触发；UI 侧应通过 Qt signal 转发到主线程。

并发模型选择说明：
不使用 asyncio，因为这里瓶颈是磁盘 I/O 而非网络，线程更适合。
不使用 multiprocessing，因为拷贝/hash 是 C 扩展释放 GIL，线程已足够。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import FileStatus, NameConflictPolicy, PipelineConfig
from .copier import (
    copy_file,
    ensure_parent_dir,
    resolve_conflict,
)
from .logger import FileRecord, JobLogger
from .scanner import FileTask
from .verifier import hash_file, verify_pair


# ---------------------------------------------------------------------------
# 回调契约
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """进度事件，统一通过 on_progress 回调上报。"""
    kind: str               # "file_started" | "bytes" | "file_done" | "job_done" | "error"
    src: Optional[str] = None
    dest: Optional[str] = None
    bytes_delta: int = 0    # 本次新增字节
    bytes_total: int = 0    # 累计已处理字节
    status: Optional[str] = None
    message: Optional[str] = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class PipelineResult:
    """运行结束的汇总。"""
    total_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    bytes_total: int = 0
    bytes_verified: int = 0
    elapsed: float = 0.0
    aborted: bool = False


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class CopyPipeline:
    """拷贝-校验流水线。

    一次实例对应一次 Job 运行。不可重用。

    Args:
        tasks: 待拷贝文件清单（来自 scanner.scan_sources）。
        dest_root: 目标根目录（文件会按 rel 重建在其下）。
        logger: 已创建的 JobLogger（XML 已写入 pending 清单）。
        config: 流水线参数。
        conflict_policy: 重名策略。
        on_progress: 进度回调。
    """

    def __init__(
        self,
        tasks: list[FileTask],
        dest_root: str,
        logger: JobLogger,
        config: Optional[PipelineConfig] = None,
        conflict_policy: NameConflictPolicy = NameConflictPolicy.KEEP,
        on_progress: Optional[ProgressCallback] = None,
        skip_srcs: Optional[set[str]] = None,
    ) -> None:
        self.tasks = tasks
        self.dest_root = dest_root
        self.logger = logger
        self.config = config or PipelineConfig()
        self.conflict_policy = conflict_policy
        self.on_progress = on_progress
        # skip_srcs：断点续传时已 verified 的源，直接跳过
        # 统一用 normcase 规范化，确保 Windows（normcase 会小写）下也能正确匹配
        self._skip_srcs = {os.path.normcase(p) for p in (skip_srcs or set())}

        self._cancel = threading.Event()
        self._bytes_lock = threading.Lock()
        self._bytes_total = sum(t.size for t in tasks)
        self._bytes_done = 0

        # 源 hash 预计算缓存（src -> hex）
        self._src_hashes: dict[str, str] = {}
        self._src_hash_lock = threading.Lock()

        # 队列
        self._copy_queue: "queue.Queue[Optional[FileTask]]" = queue.Queue(
            maxsize=self.config.copy_queue_size
        )
        self._verify_queue: "queue.Queue[Optional[_CopyResult]]" = queue.Queue(
            maxsize=self.config.verify_queue_size
        )

        self.result = PipelineResult(bytes_total=self._bytes_total)

    # ---- 对外控制 ----
    def cancel(self) -> None:
        """请求取消（优雅停止：当前文件完成后退出）。"""
        self._cancel.set()

    def run(self) -> PipelineResult:
        """阻塞执行整个流水线。返回汇总结果。"""
        start = time.monotonic()
        threads: list[threading.Thread] = []
        precompute_thread: Optional[threading.Thread] = None

        # 1) 预计算源 hash（可选，并行；不阻塞主流程）
        if self.config.verify_source_hash and self.config.precompute_source_hash:
            precompute_thread = threading.Thread(
                target=self._precompute_source_hashes, daemon=True,
                name="precompute-main",
            )
            precompute_thread.start()

        # 2) 拷贝线程池
        copy_threads = [
            threading.Thread(target=self._copy_worker, name=f"copy-{i}", daemon=True)
            for i in range(self.config.copy_workers)
        ]
        # 3) 校验线程池
        verify_threads = [
            threading.Thread(target=self._verify_worker, name=f"verify-{i}", daemon=True)
            for i in range(self.config.verify_workers)
        ]

        for t in copy_threads + verify_threads:
            t.start()
            threads.append(t)

        # 4) 投递任务
        producer_aborted = False
        try:
            for task in self.tasks:
                if self._cancel.is_set():
                    producer_aborted = True
                    break
                self._copy_queue.put(task)
        finally:
            # 投递结束哨兵（数量 = 拷贝线程数，让每个线程都能退出）
            for _ in copy_threads:
                self._copy_queue.put(None)

        # 等待拷贝线程结束
        for t in copy_threads:
            t.join()
        # 拷贝全部结束 → 给校验队列发哨兵
        for _ in verify_threads:
            self._verify_queue.put(None)
        for t in verify_threads:
            t.join()

        # 等预计算线程（应在拷贝期间已结束）
        if precompute_thread is not None:
            precompute_thread.join(timeout=2.0)

        elapsed = time.monotonic() - start
        self.result.elapsed = elapsed
        self.result.aborted = self._cancel.is_set() or producer_aborted

        if not self.result.aborted:
            self.logger.mark_completed()
        else:
            self.logger.mark_aborted()

        self._emit(ProgressEvent(kind="job_done", bytes_total=self._bytes_done))
        return self.result

    # ---- 源 hash 预计算 ----
    def _precompute_source_hashes(self) -> None:
        """并行预计算源文件 hash，存入缓存供校验阶段比对。

        一次消费 self.tasks，按 precompute_workers 并行。
        拷贝线程不依赖此结果，故可异步。
        """
        work: "queue.Queue[Optional[FileTask]]" = queue.Queue()
        for t in self.tasks:
            work.put(t)
        for _ in range(self.config.precompute_workers):
            work.put(None)

        def worker() -> None:
            while True:
                task = work.get()
                if task is None:
                    work.task_done()
                    return
                if self._cancel.is_set():
                    work.task_done()
                    continue
                try:
                    h = hash_file(
                        task.src,
                        buffer_size=self.config.hash_buffer_size,
                    )
                    with self._src_hash_lock:
                        self._src_hashes[os.path.normcase(task.src)] = h
                except OSError:
                    pass  # 校验阶段会重试/标记失败
                work.task_done()

        ws = [
            threading.Thread(target=worker, name=f"precompute-{i}", daemon=True)
            for i in range(self.config.precompute_workers)
        ]
        for t in ws:
            t.start()
        for t in ws:
            t.join()

    # ---- 拷贝 worker ----
    def _copy_worker(self) -> None:
        while True:
            task = self._copy_queue.get()
            if task is None:
                return
            try:
                self._process_copy(task)
            except Exception as e:  # noqa: BLE001 - 隔离错误
                self._emit(ProgressEvent(
                    kind="error", src=task.src,
                    message=f"拷贝异常: {e}",
                ))
                self.logger.set_file_status(
                    task.src, FileStatus.FAILED.value, error=str(e)
                )
                with self._bytes_lock:
                    self.result.failed_files += 1
            finally:
                self._copy_queue.task_done()

    def _process_copy(self, task: FileTask) -> None:
        # 断点续传：已 verified 的跳过
        if os.path.normcase(task.src) in self._skip_srcs:
            self.logger.set_file_status(task.src, FileStatus.SKIPPED.value)
            with self._bytes_lock:
                self.result.skipped_files += 1
                self._bytes_done += task.size
            return

        if self._cancel.is_set():
            return

        dest_path = task.dest_under(self.dest_root)
        # 重名解析
        resolved = resolve_conflict(dest_path, self.conflict_policy)
        if resolved.skipped:
            self.logger.set_file_status(task.src, FileStatus.SKIPPED.value)
            with self._bytes_lock:
                self.result.skipped_files += 1
                self._bytes_done += task.size
            self._emit(ProgressEvent(
                kind="file_done", src=task.src, dest=resolved.dest_path,
                status=FileStatus.SKIPPED.value, bytes_total=self._bytes_done,
            ))
            return

        ensure_parent_dir(resolved.dest_path)

        self.logger.set_file_status(task.src, FileStatus.COPYING.value)
        self._emit(ProgressEvent(
            kind="file_started", src=task.src, dest=resolved.dest_path,
            status=FileStatus.COPYING.value,
        ))

        def on_bytes(n: int) -> None:
            with self._bytes_lock:
                self._bytes_done += n
            self._emit(ProgressEvent(
                kind="bytes", src=task.src, bytes_delta=n,
                bytes_total=self._bytes_done,
            ))

        outcome = copy_file(
            task.src,
            resolved.dest_path,
            buffer_size=self.config.copy_buffer_size,
            progress_cb=on_bytes,
            overwrite=resolved.overwritten,
        )

        self.logger.set_file_status(
            task.src, FileStatus.COPIED.value, dest=outcome.dest_path
        )

        # 投入校验队列
        self._verify_queue.put(_CopyResult(task=task, dest_path=outcome.dest_path))

    # ---- 校验 worker ----
    def _verify_worker(self) -> None:
        while True:
            cr = self._verify_queue.get()
            if cr is None:
                return
            try:
                self._process_verify(cr)
            except Exception as e:  # noqa: BLE001
                self._emit(ProgressEvent(
                    kind="error", src=cr.task.src,
                    message=f"校验异常: {e}",
                ))
                self.logger.set_file_status(
                    cr.task.src, FileStatus.FAILED.value, error=str(e)
                )
                with self._bytes_lock:
                    self.result.failed_files += 1
            finally:
                self._verify_queue.task_done()

    def _process_verify(self, cr: _CopyResult) -> None:
        if self._cancel.is_set():
            return

        self.logger.set_file_status(cr.task.src, FileStatus.VERIFYING.value)
        self._emit(ProgressEvent(
            kind="file_started", src=cr.task.src, dest=cr.dest_path,
            status=FileStatus.VERIFYING.value,
        ))

        # 目标 hash
        hash_dest = hash_file(
            cr.dest_path, buffer_size=self.config.hash_buffer_size
        )

        # 源 hash：优先取预计算缓存，否则现算
        hash_src: Optional[str] = None
        if self.config.verify_source_hash:
            with self._src_hash_lock:
                hash_src = self._src_hashes.get(os.path.normcase(cr.task.src))
            if hash_src is None:
                try:
                    hash_src = hash_file(
                        cr.task.src, buffer_size=self.config.hash_buffer_size
                    )
                except OSError:
                    hash_src = None

        ok = verify_pair(hash_src, hash_dest)
        record = FileRecord(
            src=cr.task.src,
            dest=cr.dest_path,
            size=cr.task.size,
            status=FileStatus.VERIFIED.value if ok else FileStatus.FAILED.value,
            hash_src=hash_src,
            hash_dest=hash_dest,
            verified=("true" if ok else "false"),
        )
        self.logger.update_file(record)

        with self._bytes_lock:
            if ok:
                self.result.verified_files += 1
                self.result.bytes_verified += cr.task.size
            else:
                self.result.failed_files += 1

        self._emit(ProgressEvent(
            kind="file_done", src=cr.task.src, dest=cr.dest_path,
            status=(FileStatus.VERIFIED.value if ok else FileStatus.FAILED.value),
            bytes_total=self._bytes_done,
            message=None if ok else "校验失败：源与目标 hash 不一致",
        ))

    # ---- 工具 ----
    def _emit(self, ev: ProgressEvent) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(ev)
            except Exception:  # noqa: BLE001 - 回调不应影响主流程
                pass


@dataclass
class _CopyResult:
    """拷贝产物，传入校验队列。"""
    task: FileTask
    dest_path: str


__all__ = [
    "CopyPipeline",
    "PipelineResult",
    "ProgressEvent",
    "ProgressCallback",
]
