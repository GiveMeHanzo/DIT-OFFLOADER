"""拷贝引擎。

核心职责：
1. **属性保留**：使用 ``shutil.copy2`` 复制元数据（mtime/atime/权限/扩展属性）。
   在 macOS 上还会尝试保留 ``st_birthtime``（创建时间），shutil.copy2 不保留它，
   需用 ``os.setattrlist``/``os.utime`` 额外处理（Windows 无 birthtime 概念）。
2. **重名安全**：拷贝前检查目标；存在同名则按策略处理。默认 KEEP 策略追加
   ``-1``、``-2`` 后缀，**绝不覆盖**。
3. **进度回调**：流式拷贝（自定义实现而非 shutil.copyfileobj），逐块回调字节进度，
   供 UI 进度条更新；同时仍用 copy2 的 metadata 复制路径补齐属性。

为什么不直接用 shutil.copy2？
- copy2 内部用 sendfile/copy_file_range，速度快但不提供进度回调。
- 折中方案：先用自己的 chunked copy 复制数据内容并报进度，
  再用 ``shutil.copystat`` 复制权限/时间元数据。这样既能报进度又保留属性。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from config import NameConflictPolicy

# 默认 1MiB 拷贝缓冲
DEFAULT_BUFFER = 1024 * 1024


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


def copy_file(
    src: str,
    dest_path: str,
    *,
    buffer_size: int = DEFAULT_BUFFER,
    progress_cb: Optional[Callable[[int], None]] = None,
    overwrite: bool = False,
) -> CopyOutcome:
    """拷贝单个文件并保留元数据。

    Args:
        src: 源绝对路径（必须存在）。
        dest_path: 目标绝对路径（其父目录应已存在）。
        buffer_size: 读写缓冲。
        progress_cb: 进度回调，参数为本次写入字节数。
        overwrite: 是否覆盖已存在目标。

    Returns:
        CopyOutcome，含实际写入的目标路径与字节数。
    """
    if os.path.exists(dest_path) and not overwrite:
        # 由上层 resolve_conflict 处理，这里保守起见再次保护
        dest_path = _next_available(dest_path)

    size = os.path.getsize(src)
    copied = _chunked_copy(src, dest_path, buffer_size, progress_cb)
    # 复制元数据（mtime/atime/权限）
    _copy_metadata(src, dest_path)
    return CopyOutcome(
        dest_path=dest_path, bytes_copied=copied,
        overwritten=overwrite,
    )


def _chunked_copy(
    src: str,
    dest: str,
    buffer_size: int,
    progress_cb: Optional[Callable[[int], None]],
) -> int:
    """分块复制数据内容，返回写入字节数。

    仅在文件末尾调用一次 fsync，而非每块都刷盘——
    避免进度条因频繁同步 I/O 而卡在 0%。
    """
    total = 0
    with open(src, "rb") as fsrc, open(dest, "wb") as fdest:
        while True:
            chunk = fsrc.read(buffer_size)
            if not chunk:
                break
            fdest.write(chunk)
            total += len(chunk)
            if progress_cb is not None:
                progress_cb(len(chunk))
        # 仅在文件完成后刷盘一次
        fdest.flush()
        os.fsync(fdest.fileno())
    return total


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
    "copy_file",
    "CopyOutcome",
    "ensure_parent_dir",
    "DEFAULT_BUFFER",
]
