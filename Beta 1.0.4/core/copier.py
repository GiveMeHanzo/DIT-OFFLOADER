"""拷贝引擎。

核心职责：
1. **属性保留**：使用 ``shutil.copy2`` 复制元数据（mtime/atime/权限/扩展属性）。
   在 macOS 上还会尝试保留 ``st_birthtime``（创建时间），shutil.copy2 不保留它，
   需用 ``os.setattrlist``/``os.utime`` 额外处理（Windows 无 birthtime 概念）。
2. **重名安全**：拷贝前检查目标；存在同名则按策略处理。默认 KEEP 策略追加
   ``-1``、``-2`` 后缀，**绝不覆盖**。
3. **进度回调**：流式拷贝（自定义实现而非 shutil.copyfileobj），逐块回调字节进度，
   供 UI 进度条更新；同时仍用 copy2 的 metadata 复制路径补齐属性。
4. **流式源 hash（1.0.4）**：拷贝数据的同时用 xxh64 计算源文件 hash，
   源盘只需读一次——旧版由独立预读线程再读一遍源盘，是廉价 USB 读卡器
   偶发 Input/output error 的主因之一。
5. **瞬时错误重试（1.0.4）**：读侧遇到可重试 errno（EIO/EBUSY 等）时按指数
   退避重试并从断点续写，USB/读卡器抖动不再直接导致文件失败。
6. **失败清理（1.0.4）**：拷贝异常时删除半截目标文件，避免断点续传时
   残留文件占用原名、完整拷贝反被改名为 ``-1``。

为什么不直接用 shutil.copy2？
- copy2 内部用 sendfile/copy_file_range，速度快但不提供进度回调。
- 折中方案：先用自己的 chunked copy 复制数据内容并报进度，
  再用 ``shutil.copystat`` 复制权限/时间元数据。这样既能报进度又保留属性。
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import xxhash

from config import NameConflictPolicy

# 默认 1MiB 拷贝缓冲
DEFAULT_BUFFER = 1024 * 1024

# 读失败自动重试次数（不含首次尝试）
MAX_READ_RETRIES = 3
# 重试基础退避秒数，按 0.5 / 1.0 / 2.0 指数增长
_RETRY_BACKOFF_BASE = 0.5

# 视为「瞬时」的可重试 errno：USB/读卡器抖动、设备忙、被信号打断等。
# 目标盘满（ENOSPC）、权限（EACCES）等确定性错误不重试。
_RETRYABLE_ERRNOS: frozenset[int] = frozenset(
    e for e in (
        errno.EIO,        # Input/output error —— 廉价读卡器并发读时偶发
        errno.EAGAIN,     # Resource temporarily unavailable (== EWOULDBLOCK)
        errno.EBUSY,      # Device or resource busy
        errno.EINTR,      # 被信号打断（保险起见）
        errno.ENODEV,     # 设备暂时消失（读卡器重新枚举）
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "EWOULDBLOCK", None),
    ) if e is not None
)
# Windows 原生错误码（OSError.winerror）：驱动器未就绪 / CRC 错误 / 信号量超时
_RETRYABLE_WINERRORS: frozenset[int] = frozenset({21, 23, 121})


def _is_retryable(exc: OSError) -> bool:
    """判断该 OSError 是否可能是瞬时故障、值得重试。"""
    if getattr(exc, "winerror", None) is not None:
        return exc.winerror in _RETRYABLE_WINERRORS
    return exc.errno in _RETRYABLE_ERRNOS


def _remove_quiet(path: str) -> None:
    """best-effort 删除文件（清理半截产物用，失败不抛）。"""
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 重名解析
# ---------------------------------------------------------------------------

@dataclass
class ResolveResult:
    """重名解析结果。"""
    dest_path: str          # 最终使用的目标路径（已追加后缀）
    skipped: bool = False   # 是否被跳过（用户选择 SKIP 或 OVERWRITE 被禁用）
    overwritten: bool = False  # 是否覆盖（仅 OVERWRITE 策略）


def resolve_conflict(
    dest_path: str,
    policy: NameConflictPolicy,
) -> ResolveResult:
    """根据策略解析目标路径冲突。

    Args:
        dest_path: 计划的目标绝对路径。
        policy: 冲突策略。

    Returns:
        ResolveResult。``dest_path`` 是最终应使用的路径。
    """
    if not os.path.exists(dest_path):
        return ResolveResult(dest_path=dest_path)

    # 目标已存在
    if policy == NameConflictPolicy.SKIP:
        return ResolveResult(dest_path=dest_path, skipped=True)
    if policy == NameConflictPolicy.OVERWRITE:
        return ResolveResult(dest_path=dest_path, overwritten=True)
    # KEEP / ASK(默认按 KEEP 处理)：追加 -1, -2 ...
    return ResolveResult(dest_path=_next_available(dest_path))


def _next_available(dest_path: str) -> str:
    """寻找下一个可用路径：name.ext → name-1.ext → name-2.ext ...

    保证不覆盖任何已存在文件。
    """
    if not os.path.exists(dest_path):
        return dest_path
    root, ext = os.path.splitext(dest_path)
    idx = 1
    while True:
        candidate = f"{root}-{idx}{ext}"
        if not os.path.exists(candidate):
            return candidate
        idx += 1


def reserve_dest(dest_path: str) -> str:
    """以独占创建（O_CREAT|O_EXCL）占位保留目标名，返回实际保留的路径。

    pipeline 在分配目标名时用它在多拷贝线程间做原子裁决：普通的
    「exists 探测 → 之后创建」在两个线程同时解析到同一路径时会交错写
    同一文件且不报错（TOCTOU）。路径已被占用时按 -1/-2 顺延。
    调用方随后以 preallocated=True 调用 copy_file 写入占位文件。
    """
    candidate = dest_path
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = _next_available(candidate)


# ---------------------------------------------------------------------------
# 实际拷贝
# ---------------------------------------------------------------------------

@dataclass
class CopyOutcome:
    """单文件拷贝结果。"""
    dest_path: str
    bytes_copied: int
    skipped: bool = False
    overwritten: bool = False
    # 拷贝数据流中顺带计算的源文件 xxhash64（hex，无 0x 前缀）；
    # hash_src=False 时为 None。
    src_hash: Optional[str] = None


def copy_file(
    src: str,
    dest_path: str,
    *,
    buffer_size: int = DEFAULT_BUFFER,
    progress_cb: Optional[Callable[[int], None]] = None,
    overwrite: bool = False,
    hash_src: bool = False,
    preallocated: bool = False,
) -> CopyOutcome:
    """拷贝单个文件并保留元数据。

    Args:
        src: 源绝对路径（必须存在）。
        dest_path: 目标绝对路径（其父目录应已存在）。
        buffer_size: 读写缓冲。
        progress_cb: 进度回调，参数为本次写入字节数。
        overwrite: 是否覆盖已存在目标。
        hash_src: 是否在拷贝数据流中顺带计算源文件 xxhash64。
        preallocated: dest_path 是否已由调用方（pipeline）独占占位。
            占位文件会被本函数直接截断写入，不再触发重名改名。

    Returns:
        CopyOutcome，含实际写入的目标路径、字节数与源 hash。

    Raises:
        OSError: 拷贝失败（重试耗尽或不可重试错误）。失败时半截目标文件
            已被清理。
    """
    if not preallocated and os.path.exists(dest_path) and not overwrite:
        # 由上层 resolve_conflict 处理，这里保守起见再次保护
        dest_path = _next_available(dest_path)

    hasher = xxhash.xxh64() if hash_src else None
    try:
        size = os.path.getsize(src)
        copied = _chunked_copy(src, dest_path, buffer_size, progress_cb, hasher)
        if copied != size:
            raise OSError(
                f"拷贝字节数不符：预期 {size}，实际 {copied}（源文件可能在拷贝中变化）"
            )
        # 复制元数据（mtime/atime/权限）
        _copy_metadata(src, dest_path)
    except Exception:
        # 失败清理：不留半截文件。断点续传时残留文件会占用原名，
        # 导致重拷的完整文件反被改名为 -1。
        _remove_quiet(dest_path)
        raise
    return CopyOutcome(
        dest_path=dest_path, bytes_copied=copied,
        overwritten=overwrite,
        src_hash=hasher.hexdigest() if hasher is not None else None,
    )


def _chunked_copy(
    src: str,
    dest: str,
    buffer_size: int,
    progress_cb: Optional[Callable[[int], None]],
    hasher: Optional["xxhash.xxh64"] = None,
) -> int:
    """分块复制数据内容，返回写入字节数。

    - 读侧瞬时错误（EIO/EBUSY 等）按指数退避自动重试，从断点续写：
      重新打开源与目标，目标截断到已写入字节数，源 seek 到相同偏移。
    - 仅在文件末尾调用一次 fsync，而非每块都刷盘——
      避免进度条因频繁同步 I/O 而卡在 0%。
    """
    total = 0
    retry = 0
    while True:
        try:
            # 首次写入创建新文件；断点续写打开已存在的半截文件并截齐
            mode = "wb" if total == 0 else "r+b"
            with open(src, "rb") as fsrc, open(dest, mode) as fdest:
                if total:
                    fsrc.seek(total)
                    fdest.seek(total)
                    fdest.truncate(total)
                while True:
                    chunk = fsrc.read(buffer_size)
                    if not chunk:
                        break
                    fdest.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    total += len(chunk)
                    if progress_cb is not None:
                        progress_cb(len(chunk))
                # 仅在文件完成后刷盘一次
                fdest.flush()
                os.fsync(fdest.fileno())
            return total
        except OSError as e:
            if not _is_retryable(e) or retry >= MAX_READ_RETRIES:
                raise
            retry += 1
            time.sleep(_RETRY_BACKOFF_BASE * (2 ** (retry - 1)))


def _copy_metadata(src: str, dest: str) -> None:
    """复制权限、时间等元数据；平台差异在此吸收。"""
    # copystat: 复制权限位 + atime/mtime（等价于 copy2 的 metadata 部分）
    try:
        shutil.copystat(src, dest)
    except OSError:
        # 某些跨文件系统/权限场景 copystat 可能失败，不阻塞拷贝
        pass

    # macOS 创建时间（birthtime）：copystat 不保留，单独处理
    if sys.platform == "darwin":
        try:
            st = os.stat(src)
            birth = getattr(st, "st_birthtime", None)
            if birth is not None:
                # os.utime 支持 follow_symlinks；birthtime 需通过 setattrlist，
                # 但 Python 标准库未暴露 setattrlist 的 SET 语义，
                # 退而求其次：确保 mtime 精确（copystat 已做），birthtime 尽力而为。
                os.utime(dest, (st.st_atime, st.st_mtime))
        except OSError:
            pass


def ensure_parent_dir(path: str) -> None:
    """确保目标文件的父目录存在（含多级创建）。"""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


__all__ = [
    "ResolveResult",
    "resolve_conflict",
    "reserve_dest",
    "copy_file",
    "CopyOutcome",
    "ensure_parent_dir",
    "DEFAULT_BUFFER",
    "_remove_quiet",
]
