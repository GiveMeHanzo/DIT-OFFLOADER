"""流水线编排：拷贝与校验并行执行。

实现需求中「拷贝 B 的同时校验 A」的核心机制。采用生产者-消费者模型：

    [枚举任务清单]
            │ copy_queue
            ▼
     ┌──────────────────┐
     │ 拷贝线程池(N)     │  ── 数据流中顺带计算源 hash（源盘只读一次）
     └──────────────────┘   └─ shutil.copystat 保留元数据
            │ verify_queue
            ▼
     ┌──────────────────┐
     │ 校验线程池(M)     │  ── 仅读目标盘计算 hash，与拷贝时的源 hash 比对
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

1.0.4 变更：
- 移除独立的源 hash 预读线程。旧版 2 个拷贝线程 + 2 个预读线程（外加校验
  回退重读）会同时读同一张卡，廉价 USB 读卡器在此并发下偶发
  ``Input/output error``。现在源 hash 在拷贝数据流中顺带计算，源盘只读一次。
- 断点续传时自动清理上次运行残留的未验证目标文件，保证完整拷贝拿回原名。
- ``verify_source_hash=True`` 时源 hash 缺失不再静默按「通过」处理，而是判失败。
"""
from __future__ import annotations

import errno
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import FileStatus, NameConflictPolicy, PipelineConfig
from .copier import (
    _remove_quiet,
    copy_file,
    ensure_parent_dir,
    reserve_dest,
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
    abort_reason: str = ""   # 中断原因（如源盘丢失），供 UI 展示


# 断点续传时视为「残留」的上次状态：目标文件可能不完整或未验证，重拷前先删除。
# PENDING 没有目标产物；SKIPPED/VERIFIED 不需要重拷。
_RESUME_STALE_STATUSES: frozenset[str] = frozenset(
    {
        FileStatus.COPYING.value,
        FileStatus.COPIED.value,
        FileStatus.VERIFYING.value,
        FileStatus.FAILED.value,
    }
)

# 设备级读错误：源盘被拔出/断开时可能出现。命中后还需二次确认源目录是否
# 已不可访问，才能判为「源盘丢失」——避免把「单个文件被外部删除」误判成拔盘。
# 设备级错误码：语义即「设备不存在/未配置」，在文件读取场景基本只有源盘被拔
# 或断开一种解释。命中即判源盘丢失——**不再要求源目录也已消失**：macOS 拔盘后
# 挂载点目录可能残留（或用户随即插回卡），仅检查目录存在会把中断误判为失败。
_DEVICE_GONE_ERRNOS: frozenset[int] = frozenset(
    e for e in (errno.ENXIO, errno.ENODEV) if e is not None
)
# Windows：设备未就绪 / CRC 错误 / 设备未连接 / 网络资源不可用
_DEVICE_GONE_WINERRORS: frozenset[int] = frozenset({21, 23, 1167, 55, 433})

# 其余可疑错误：可能源于源盘丢失，也可能只是单文件被删或坏道。需辅以
# 「源目录也已不可访问」才能判定，避免把单文件问题误判成拔盘、中断整个任务。
_AMBIGUOUS_ERRNOS: frozenset[int] = frozenset(
    e for e in (
        errno.EIO,          # I/O error（可能是坏道，也可能是设备消失后的读）
        errno.ENOENT,       # No such file（可能是单文件被删，也可能拔盘后打开源）
        getattr(errno, "EROFS", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "ESTALE", None),
    ) if e is not None
)
# Windows 泛化 I/O / 访问错误（非明确的设备码），同样需目录佐证
_AMBIGUOUS_WINERRORS: frozenset[int] = frozenset({1, 5, 995})


def _parent_unreachable(src: str) -> bool:
    """源文件所在目录是否已不可访问（整盘拔出后目录随之消失）。"""
    try:
        return not os.path.exists(os.path.dirname(src))
    except OSError:
        return True


def _looks_like_source_loss(exc: OSError, src: str) -> bool:
    """判断拷贝异常是否源于源盘丢失（拔出/断开）。

    分两级判定：
    1. 设备级错误码（ENXIO/ENODEV 等）→ **直接**判源盘丢失。设备已不存在，
       无需再检查目录是否消失（macOS 挂载点常残留，检查目录会漏判）。
    2. 其余可疑错误（EIO/ENOENT/字节数不符等）→ 仅当源目录也已不可访问时
       才判定，以区分「整个源盘被拔」与「单个文件被外部删除 / 坏道」——
       后者只应记为单文件失败，绝不应中断整个任务、把用户从续传流程里踢出去。
    """
    we = getattr(exc, "winerror", None)
    if we is not None:
        if we in _DEVICE_GONE_WINERRORS:
            return True
        if we in _AMBIGUOUS_WINERRORS:
            return _parent_unreachable(src)
        return False
    err = exc.errno
    if err in _DEVICE_GONE_ERRNOS:
        return True
    if err is None or err in _AMBIGUOUS_ERRNOS:
        # errno=None：copy_file 合成的错误（如字节数不符）也可能因拔盘引起，
        # 源目录确已消失时同样判中断。
        return _parent_unreachable(src)
    return False


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
        skip_srcs: 断点续传时已 verified 的源，直接跳过（旧语义，保留兼容）。
        resume_records: 断点续传时上次运行的状态记录
            {normcase(src): (status, dest)}。非 verified 的残留目标文件
            在重拷前会被清理，避免半截文件占用原名。
        reverify_records: 断点续传时上次已 verified 的文件
            {normcase(src): (logged_dest, stored_hash)}。这些文件不再直接信任，
            而是重新读取目标盘哈希与 stored_hash 比对——目标盘若发生静默损坏
            （极罕见故障），重校验会发现不一致并触发重拷，避免「已完成」的
            文件实际已损坏却被当作通过。
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
        resume_records: Optional[dict[str, tuple[str, str]]] = None,
        reverify_records: Optional[dict[str, tuple[str, Optional[str]]]] = None,
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
        self._resume_records = resume_records or {}
        self._reverify_records = reverify_records or {}

        self._cancel = threading.Event()
        # 源盘丢失标记：任一拷贝线程检测到拔盘即置位并触发取消，使任务被记为
        # 「中断」而非「完成」——否则 XML 会写成 Completed，断点续传失效。
        self._source_lost = False
        self._bytes_lock = threading.Lock()
        # 目标名分配锁：exists 探测 → 实际创建之间没有原子性，两个拷贝
        # 线程可能同时把不同源解析到同一目标路径并交错写同一文件（TOCTOU）。
        # 在这把锁内做「重名解析 + O_EXCL 独占占位」，保证各线程拿到互不冲突的目标名。
        self._dest_lock = threading.Lock()
        self._bytes_total = sum(t.size for t in tasks)
        self._bytes_done = 0

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

        # 1) 拷贝线程池（源 hash 在拷贝数据流中顺带计算，无独立预读线程）
        copy_threads = [
            threading.Thread(target=self._copy_worker, name=f"copy-{i}", daemon=True)
            for i in range(self.config.copy_workers)
        ]
        # 2) 校验线程池（仅读目标盘）
        verify_threads = [
            threading.Thread(target=self._verify_worker, name=f"verify-{i}", daemon=True)
            for i in range(self.config.verify_workers)
        ]

        for t in copy_threads + verify_threads:
            t.start()

        # 3) 投递任务
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

        elapsed = time.monotonic() - start
        self.result.elapsed = elapsed
        self.result.aborted = self._cancel.is_set() or producer_aborted
        if self._source_lost:
            self.result.abort_reason = (
                "源盘丢失（设备已断开或拔出），任务中断，未完成拷贝"
            )

        if not self.result.aborted:
            self.logger.mark_completed()
        else:
            self.logger.mark_aborted()

        self._emit(ProgressEvent(kind="job_done", bytes_total=self._bytes_done))
        return self.result

    # ---- 拷贝 worker ----
    def _copy_worker(self) -> None:
        while True:
            task = self._copy_queue.get()
            if task is None:
                return
            try:
                self._process_copy(task)
            except Exception as e:  # noqa: BLE001 - 隔离错误
                # 源盘丢失（拔卡/断开）不能只当单文件失败：否则整单会被误判为
                # 「完成」，XML 写成 Completed，断点续传彻底失效。此时中止任务，
                # 让 XML 记为 Aborted，未处理文件保持 pending 供续传。
                src_lost = isinstance(e, OSError) and _looks_like_source_loss(e, task.src)
                if src_lost:
                    self._source_lost = True
                    message = f"源盘丢失，任务中断: {e}"
                    self._cancel.set()
                else:
                    message = f"拷贝异常: {e}"
                self._emit(ProgressEvent(
                    kind="error", src=task.src, message=message,
                ))
                self.logger.set_file_status(
                    task.src, FileStatus.FAILED.value,
                    error=(message if src_lost else str(e)),
                )
                with self._bytes_lock:
                    self.result.failed_files += 1
            finally:
                self._copy_queue.task_done()

    def _process_copy(self, task: FileTask) -> None:
        src_key = os.path.normcase(task.src)

        # 断点续传：上次已验证的文件不直接跳过，而是重新校验目标盘。
        # 通过则记为新 verified（并顺带修正日志中过期的 dest 路径）；
        # 不通过（损坏/缺失）则删除坏文件并继续走正常重拷流程。
        is_reverify = src_key in self._reverify_records
        if is_reverify and self._reverify_existing(task, src_key):
            return

        # 断点续传：已 verified 的跳过（旧语义，供兼容调用）
        if src_key in self._skip_srcs:
            self.logger.set_file_status(task.src, FileStatus.SKIPPED.value)
            with self._bytes_lock:
                self.result.skipped_files += 1
                self._bytes_done += task.size
            return

        if self._cancel.is_set():
            return

        # 重校验失败后的重拷一律按 KEEP 处理，不受用户所选策略影响：
        # 这是「修复一个损坏的已完成文件」，若按 SKIP 跳过会让该源在目标盘
        # 上一个副本都不剩（可靠性黑洞）。加 -1 也只是不覆盖他人文件，
        # 不会造成数据丢失。
        policy = NameConflictPolicy.KEEP if is_reverify else self.conflict_policy

        dest_path = task.dest_under(self.dest_root)
        # 目标名分配（锁内）：断点续传残留清理 → 重名解析 → O_EXCL 原子占位。
        # 1) 清理上次运行残留的未验证目标文件：不清的话 KEEP 策略会把半截
        #    文件保留原名、完整新拷贝反被改名 -1；
        # 2) 两个拷贝线程（如两张卡各有一个同名文件）可能同时解析到同一
        #    目标路径，独占占位保证文件名互不冲突，二者都保留（KEEP 语义）。
        with self._dest_lock:
            prev = self._resume_records.get(os.path.normcase(task.src))
            if prev is not None:
                prev_status, prev_dest = prev
                if (
                    prev_status in _RESUME_STALE_STATUSES
                    and prev_dest
                    and os.path.normcase(prev_dest) == os.path.normcase(dest_path)
                    and os.path.exists(prev_dest)
                ):
                    _remove_quiet(prev_dest)
            resolved = resolve_conflict(dest_path, policy)
            preallocated = False
            if not resolved.skipped and not resolved.overwritten:
                ensure_parent_dir(resolved.dest_path)
                resolved.dest_path = reserve_dest(resolved.dest_path)
                preallocated = True
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
            hash_src=self.config.verify_source_hash,
            preallocated=preallocated,
        )

        self.logger.set_file_status(
            task.src, FileStatus.COPIED.value, dest=outcome.dest_path
        )

        # 投入校验队列
        self._verify_queue.put(_CopyResult(
            task=task, dest_path=outcome.dest_path, src_hash=outcome.src_hash,
        ))

    # ---- 断点续传重校验 ----
    def _locate_reverify_dest(
        self, logged_dest: str, src: str
    ) -> Optional[str]:
        """定位续传文件在目标盘上的真实路径。

        日志中的 dest 可能因场记重命名而与磁盘实际文件名不符（且这类文件
        带 SC 前缀，重命名器会跳过、无法自我纠正）。优先用日志 dest；不存在
        时在同目录搜索「以原 basename 结尾」的文件（即重命名后的新名）。
        """
        if logged_dest and os.path.isfile(logged_dest):
            return logged_dest
        base = os.path.basename(logged_dest) or os.path.basename(src)
        folder = os.path.dirname(logged_dest) if logged_dest else ""
        if folder and os.path.isdir(folder):
            for name in os.listdir(folder):
                if name != base and name.endswith(base):
                    cand = os.path.join(folder, name)
                    if os.path.isfile(cand):
                        return cand
        return None

    def _reverify_existing(self, task: FileTask, src_key: str) -> bool:
        """重新校验续传前已完成的文件。通过返回 True，否则 False（交由重拷）。"""
        logged_dest, stored_hash = self._reverify_records[src_key]
        dest = self._locate_reverify_dest(logged_dest, task.src)
        if dest is None:
            # 目标文件找不到 → 需要重新拷贝
            return False

        self.logger.set_file_status(task.src, FileStatus.VERIFYING.value, dest=dest)
        self._emit(ProgressEvent(
            kind="file_started", src=task.src, dest=dest,
            status=FileStatus.VERIFYING.value,
        ))
        try:
            hash_dest = hash_file(dest, buffer_size=self.config.hash_buffer_size)
        except OSError:
            hash_dest = ""

        # 与日志中记录的哈希比对（不使用 verify_pair 的 None 直通语义：
        # 缺少记录哈希时无法证明完好，按未通过处理，宁可重拷）
        if hash_dest and stored_hash and verify_pair(stored_hash, hash_dest):
            # 通过：写入新 verified，并用磁盘真实路径修正日志中过期的 dest
            self.logger.update_file(FileRecord(
                src=task.src, dest=dest, size=task.size,
                status=FileStatus.VERIFIED.value,
                hash_src=stored_hash, hash_dest=hash_dest, verified="true",
            ))
            with self._bytes_lock:
                self.result.verified_files += 1
                self.result.bytes_verified += task.size
                self._bytes_done += task.size
            self._emit(ProgressEvent(
                kind="file_done", src=task.src, dest=dest,
                status=FileStatus.VERIFIED.value, bytes_total=self._bytes_done,
            ))
            return True

        # 未通过：删除损坏文件，让后续正常拷贝写回原文件名
        _remove_quiet(dest)
        return False

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

        # 源 hash：来自拷贝数据流中的顺带计算（源盘只读一次，不再重读）。
        # verify_source_hash=True 时缺失即失败——旧版会静默按「通过」处理，
        # 对 DIT 场景是数据安全隐患。
        error_msg: Optional[str] = None
        if self.config.verify_source_hash:
            hash_src: Optional[str] = cr.src_hash
            if hash_src is None:
                ok = False
                error_msg = "源 hash 缺失，无法比对（拷贝时未取得源 hash）"
            else:
                ok = verify_pair(hash_src, hash_dest)
                if not ok:
                    error_msg = "校验失败：源与目标 hash 不一致"
        else:
            # 仅目标校验模式：verify_pair(None, dest) 按约定返回 True
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
        if not ok and error_msg:
            record.error = error_msg
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
            message=None if ok else error_msg,
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
    # 拷贝数据流中计算的源 hash（verify_source_hash=False 时为 None）
    src_hash: Optional[str] = None


__all__ = [
    "CopyPipeline",
    "PipelineResult",
    "ProgressEvent",
    "ProgressCallback",
]
