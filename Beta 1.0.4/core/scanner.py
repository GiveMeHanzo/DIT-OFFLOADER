r"""源文件枚举与任务项建模。

scanner 负责：把用户选中的「源路径列表」展开成一份扁平的待拷贝文件清单，
并保留每个文件相对其「源根」的相对路径，以便在目标侧重建目录结构。

例如用户选择源 ``D:\Shoots\A001``，则该目录内 ``Clip/clip_0001.mxf``
会被展开为::

    FileTask(src=D:\Shoots\A001\Clip\clip_0001.mxf,
             rel=Clip\clip_0001.mxf)

目标侧拼接为 ``<dest_root>\<job_name>\Clip\clip_0001.mxf``。

设计要点：
- 用户选「文件」时，rel 仅为文件名本身（无目录上下文）。
- 用户选「文件夹」时，rel 相对该文件夹根。
- 多个源可共享同一目标，互不串扰。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

from config import EXCLUDED_NAMES, is_video


@dataclass(frozen=True)
class FileTask:
    """单个待拷贝文件的不可变描述。"""
    src: str                # 绝对源路径
    rel: str                # 相对源根的路径（含子目录），用 os.sep 分隔
    size: int               # 字节数，用于进度统计与重名判断
    is_video: bool          # 是否视频（报告用）

    def dest_under(self, dest_root: str) -> str:
        """拼接目标绝对路径。"""
        return os.path.join(dest_root, self.rel)


def _is_excluded(name: str) -> bool:
    """是否应跳过（隐藏/系统垃圾文件）。
    跨平台排除所有以 '.' 开头的文件/目录（macOS .DS_Store、Linux .git 等）。
    额外的非点号开头文件（Thumbs.db、desktop.ini 等）由 EXCLUDED_NAMES 捕获。
    """
    if name.startswith('.'):
        return True
    if name in EXCLUDED_NAMES:
        return True
    return False


def scan_sources(sources: list[str]) -> list[FileTask]:
    """把用户选择的源路径列表展开为 FileTask 列表。

    Args:
        sources: 源路径列表，元素可以是文件或文件夹（绝对路径）。

    Returns:
        去重后的 FileTask 列表（按源路径排序，保证可复现）。
    """
    seen: set[str] = set()
    tasks: list[FileTask] = []

    for src in sources:
        src = os.path.abspath(src)
        if not os.path.exists(src):
            # 不存在的源直接跳过；调用方应在 UI 阶段过滤
            continue
        for task in _scan_one(src):
            if task.src in seen:
                continue
            seen.add(task.src)
            tasks.append(task)

    tasks.sort(key=lambda t: t.src.lower())
    return tasks


def _scan_one(src: str) -> Iterator[FileTask]:
    """展开单个源（文件或文件夹）。"""
    name = os.path.basename(src)

    if os.path.isfile(src):
        if _is_excluded(name):
            return
        try:
            size = os.path.getsize(src)
        except OSError:
            return
        yield FileTask(src=src, rel=name, size=size, is_video=is_video(src))
        return

    if os.path.isdir(src):
        # 文件夹源：rel 相对该文件夹本身，不含顶层目录名。
        # 例：D:\Shoots\A001\Clip\clip.mxf → rel = Clip\clip.mxf
        # 这样拷贝到目标时直接在目标根下展开，不会多一层 A001\ 包裹。
        for dirpath, dirnames, filenames in os.walk(src):
            # 原地修改 dirnames 以实现剪枝（跳过隐藏/系统目录）
            dirnames[:] = sorted(
                d for d in dirnames if not _is_excluded(d)
            )
            for fname in sorted(filenames):
                if _is_excluded(fname):
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                # 相对源文件夹的路径
                rel_dir = os.path.relpath(dirpath, src)
                if rel_dir == ".":
                    rel = fname
                else:
                    rel = os.path.join(rel_dir, fname)
                yield FileTask(
                    src=full, rel=rel, size=size, is_video=is_video(full)
                )


def has_copyable_file(sources: list[str]) -> bool:
    """快速探测源路径中是否存在任何可拷贝文件（找到第一个即返回）。

    供 GUI 启动前预检使用——scan_sources 会 walk 整张卡，
    大容量卡在 GUI 线程上会卡界面数秒；本函数与 scan_sources
    使用相同的排除规则，但找到第一个文件即短路返回。
    """
    for src in sources:
        src = os.path.abspath(src)
        if os.path.isfile(src):
            if not _is_excluded(os.path.basename(src)):
                return True
            continue
        if not os.path.isdir(src):
            continue
        for _dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if not _is_excluded(d)]
            for fname in filenames:
                if not _is_excluded(fname):
                    return True
    return False


def total_size(tasks: list[FileTask]) -> int:
    """统计总字节数。"""
    return sum(t.size for t in tasks)


__all__ = ["FileTask", "scan_sources", "has_copyable_file", "total_size"]
