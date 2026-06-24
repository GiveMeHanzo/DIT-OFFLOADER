"""report/frame_extractor.py — FFmpeg 视频抽帧。"""
from __future__ import annotations

import base64
import functools
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from report.ffprobe_utils import _find_ffprobe as _find_ffprobe_base


@functools.lru_cache(maxsize=1)
def _find_ffmpeg() -> str | None:
    """查找系统 ffmpeg，检查 PATH 和常见安装路径（结果缓存）。"""
    # 复用 ffprobe_utils 的查找逻辑（也带 lru_cache），确保两个模块找到的是同一份
    # ffmpeg，避免 probe 用 winget 版而抽帧用 PATH 版的不一致（BUG 3 的根因）。
    ffprobe = _find_ffprobe_base()
    if ffprobe:
        return ffprobe.replace("ffprobe", "ffmpeg")
    return None


@dataclass
class FrameSet:
    """三帧截图集。"""
    head: str = ""    # Base64 JPEG (首部 10%)
    mid: str = ""     # Base64 JPEG (中部 50%)
    tail: str = ""    # Base64 JPEG (尾部 90%)
    ok: bool = False  # 是否成功提取


def extract_frames(
    filepath: str,
    duration: float = 0.0,
    timeout: float = 60.0,
) -> FrameSet:
    """提取视频首/中/尾三帧的 Base64 JPEG。

    Args:
        filepath: 视频文件绝对路径。
        duration: 已知时长（秒），==0 会尝试从 ffprobe 获取。
        timeout: 单帧提取超时秒数（三帧总计不超过 ``timeout``，因为并发提取）。

    Returns:
        FrameSet，包含三帧的 Base64 字符串。
    """
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None or not os.path.isfile(filepath):
        return FrameSet()

    # 如果没有提供时长，尝试从 ffprobe 获取
    if duration <= 0:
        duration = _get_duration(filepath)

    if duration <= 0:
        # 无法获取时长，仅提取首帧（0 秒）
        head_b64 = _extract_one(ffmpeg, filepath, 0, timeout)
        return FrameSet(head=head_b64, ok=bool(head_b64))

    positions = {
        "head": 0.0,                                    # 视频首帧
        "mid": duration * 0.50,
        "tail": max(duration - 0.1, 0.0),               # 视频尾帧
    }

    # 三帧彼此独立，并发提取（ffmpeg 子进程是外部 I/O，不占 GIL）。
    result = FrameSet()
    success = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {key: ex.submit(_extract_one, ffmpeg, filepath, pos, timeout)
                   for key, pos in positions.items()}
        for key, fut in futures.items():
            b64 = fut.result()
            setattr(result, key, b64)
            if b64:
                success += 1
    result.ok = success == 3
    return result


def _extract_one(
    ffmpeg: str, filepath: str, position_sec: float, timeout: float,
) -> str:
    """提取单帧 JPEG（最大宽度 600px，品质可控），返回 Base64 字符串。"""
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-ss", str(position_sec),
                "-i", filepath,
                "-vframes", "1",
                "-vf", "scale='min(600,iw)':-2",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "-q:v", "8",
                "-loglevel", "error",
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
        )
        if proc.returncode == 0 and proc.stdout:
            return base64.b64encode(proc.stdout).decode("ascii")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _get_duration(filepath: str) -> float:
    """快速获取视频时长（秒）。复用 ffprobe_utils 的查找，确保与 probe 一致。"""
    ffprobe = _find_ffprobe_base()
    if ffprobe is None:
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 0.0
