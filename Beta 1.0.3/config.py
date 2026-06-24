"""全局配置与常量。

集中管理支持的文件格式、流水分组、路径约定等，便于跨模块复用。
所有平台相关逻辑（路径分隔、盘符）都通过 os.path / pathlib 处理，
不在此处硬编码。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# 支持的文件格式
# ---------------------------------------------------------------------------

# 视频文件后缀（用于报告置顶、抽帧、元数据提取）
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    ext.lower()
    for ext in (
        # 常见封装
        ".mp4", ".mov", ".mxf", ".m4v", ".mkv", ".avi", ".webm",
        # Sony / ARRI / RED / Canon / Panasonic 专业格式
        ".r3d", ".braw", ".ari", ".crm", ".mts", ".m2ts", ".ts",
        # ProRes 常在 mov 内，独立列出 wav/audio 供报告区分
    )
)

# 音频文件后缀（报告里同样会显示元数据，但不抽帧）
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    ext.lower() for ext in (".wav", ".aiff", ".aif", ".mp3", ".aac", ".flac", ".m4a")
)

# 图片文件后缀（仅报告显示，不抽帧）
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    ext.lower() for ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".dng", ".cr2", ".cr3", ".arw", ".raf", ".nef", ".braw")
)


def is_video(path: str | os.PathLike) -> bool:
    """判断给定路径是否为视频文件（按扩展名）。"""
    return os.path.splitext(str(path))[1].lower() in VIDEO_EXTENSIONS


def is_media(path: str | os.PathLike) -> bool:
    """判断是否为需要报告处理的媒体文件（视频/音频/图片）。"""
    ext = os.path.splitext(str(path))[1].lower()
    return ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS or ext in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# 日志 / 报告命名
# ---------------------------------------------------------------------------

XML_LOG_SUFFIX = "_log.xml"
REPORT_SUFFIX = "_report.html"


def log_xml_path(dest_root: str | os.PathLike, job_name: str) -> str:
    """返回目标目录下的 XML 日志路径：[JobName]_log.xml"""
    return os.path.join(str(dest_root), f"{job_name}{XML_LOG_SUFFIX}")


def report_html_path(dest_root: str | os.PathLike, job_name: str) -> str:
    """返回目标目录下的 HTML 报告路径：[JobName]_report.html"""
    return os.path.join(str(dest_root), f"{job_name}{REPORT_SUFFIX}")


# ---------------------------------------------------------------------------
# 文件状态机
# ---------------------------------------------------------------------------

class FileStatus(str, Enum):
    """单个文件在 XML 日志中的状态。值即写入 XML 的字符串。"""
    PENDING = "pending"        # 已记录待拷贝
    COPYING = "copying"        # 拷贝中
    COPIED = "copied"          # 拷贝完成，待校验
    VERIFYING = "verifying"    # 校验中
    VERIFIED = "verified"      # 双校验通过（终态成功）
    FAILED = "failed"          # 拷贝或校验失败
    SKIPPED = "skipped"        # 用户选择跳过（重名/断点续传跳过已完成）


class JobStatus(str, Enum):
    """整个 Job 在 XML 根节点上的状态。"""
    RUNNING = "Running"
    COMPLETED = "Completed"
    ABORTED = "Aborted"
    ERROR = "Error"


# ---------------------------------------------------------------------------
# 重名处理策略
# ---------------------------------------------------------------------------

class NameConflictPolicy(str, Enum):
    """遇到目标同名文件时的策略（批量首次弹窗后由用户选择）。"""
    ASK = "ask"          # 逐个询问（默认，仅对第一个弹窗）
    KEEP = "keep"        # 保留：追加 -1/-2，不覆盖
    SKIP = "skip"        # 跳过：不拷贝该文件
    OVERWRITE = "overwrite"  # 覆盖（默认禁用，仅为完整性保留；DIT 场景不推荐）


# ---------------------------------------------------------------------------
# 流水线参数
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """流水线运行参数。"""
    copy_workers: int = 2          # 并行拷贝线程数（多盘源时可提高）
    verify_workers: int = 2        # 并行校验线程数
    copy_queue_size: int = 64      # 拷贝队列上限（背压）
    verify_queue_size: int = 64    # 校验队列上限
    hash_buffer_size: int = 1024 * 1024  # xxhash 读取缓冲 1MiB
    copy_buffer_size: int = 1024 * 1024  # 自定义拷贝缓冲 1MiB（shutil.copy2 内部）
    # 双校验开关；True=源+目标双校验；False=仅目标校验（更快但不防源盘损坏）
    verify_source_hash: bool = True
    # 是否对源 hash 在预扫描阶段并行预计算
    precompute_source_hash: bool = True
    # 预计算线程数
    precompute_workers: int = 2

    def __post_init__(self) -> None:
        if self.copy_workers < 1:
            raise ValueError("copy_workers 必须 >= 1")
        if self.verify_workers < 1:
            raise ValueError("verify_workers 必须 >= 1")


# ---------------------------------------------------------------------------
# 默认排除项（隐藏文件、系统垃圾文件）
# ---------------------------------------------------------------------------

EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".DS_Store",        # macOS
        "Thumbs.db",        # Windows
        "desktop.ini",      # Windows
        ".Spotlight-V100",  # macOS 元数据
        ".Trashes",         # macOS 回收站
        "__pycache__",
    }
)

# ---------------------------------------------------------------------------
# 驱动器面板排除项（不显示在左侧 DrivePanel 的挂载点 / 文件系统类型）
# ---------------------------------------------------------------------------

# 这些挂载点在 macOS 上是系统内部卷，用户不应通过 DIT 工具操作
EXCLUDED_MOUNTPOINTS: frozenset[str] = frozenset(
    {
        "/", "/dev", "/net", "/home",
    }
)
_EXCLUDED_MOUNTPOINT_PREFIXES: tuple[str, ...] = (
    "/System/Volumes/",
    "/private/",
)

# 这些文件系统类型在任何平台上都是系统内部使用的，从未包含用户数据
EXCLUDED_FS_TYPES: frozenset[str] = frozenset(
    {
        "devfs",    # macOS 设备文件系统
        "proc",     # Linux /proc
        "sysfs",    # Linux /sys
        "tmpfs",    # 内存临时文件系统
        "cgroup",   # Linux cgroup
        "debugfs",  # Linux debug
        "configfs", # Linux config
        "fuse.gvfsd-fuse",  # GNOME 虚拟文件系统
        "autofs",   # 自动挂载 stub
    }
)


__all__ = [
    "VIDEO_EXTENSIONS",
    "AUDIO_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "is_video",
    "is_media",
    "XML_LOG_SUFFIX",
    "REPORT_SUFFIX",
    "log_xml_path",
    "report_html_path",
    "FileStatus",
    "JobStatus",
    "NameConflictPolicy",
    "PipelineConfig",
    "EXCLUDED_NAMES",
    "EXCLUDED_MOUNTPOINTS",
    "EXCLUDED_FS_TYPES",
]
