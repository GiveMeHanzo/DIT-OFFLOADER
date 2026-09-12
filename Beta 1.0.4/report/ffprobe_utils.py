"""report/ffprobe_utils.py — FFprobe 元数据提取。

通过 subprocess 调用系统 ffprobe，提取视频文件的结构化元数据。
ffprobe 不可用时优雅降级。
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# 查找结果在整个进程内不变（main.py 启动时已把 ffmpeg 路径加入 PATH），
# 用 lru_cache 缓存避免对每个视频重复执行 shutil.which / os.walk。
@functools.lru_cache(maxsize=1)
def _find_ffprobe() -> Optional[str]:
    """查找系统 PATH 中的 ffprobe，同时检查常见安装路径。"""
    path = shutil.which("ffprobe")
    if path:
        return path
    # 常见 Windows 安装路径（winget/scoop/choco）
    candidates = [
        # winget (Gyan)
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        ),
        # scoop
        os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
        # chocolatey
        r"C:\ProgramData\chocolatey\bin",
        # manual
        r"C:\ffmpeg\bin",
    ]
    for base in candidates:
        for root, dirs, files in os.walk(base):
            # 限制深度避免全盘搜索
            depth = root.replace(base, "").count(os.sep)
            if depth > 4:
                dirs.clear()
                continue
            if "ffprobe.exe" in files:
                return os.path.join(root, "ffprobe.exe")
    return None


@functools.lru_cache(maxsize=1)
def _find_ffmpeg() -> Optional[str]:
    """查找系统 PATH 中的 ffmpeg，同时检查常见安装路径。"""
    path = shutil.which("ffmpeg")
    if path:
        return path
    ffprobe = _find_ffprobe()
    if ffprobe:
        return ffprobe.replace("ffprobe", "ffmpeg")
    return None


@dataclass
class VideoMeta:
    """视频文件的结构化元数据。"""
    filename: str
    filepath: str
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    framerate: float = 0.0
    pixel_format: str = ""
    timecode: str = ""
    audio_codec: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0
    raw: dict = field(default_factory=dict)


def probe_video(filepath: str, timeout: float = 30.0) -> Optional[VideoMeta]:
    ffprobe = _find_ffprobe()
    if ffprobe is None or not os.path.isfile(filepath):
        return None
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", filepath],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vid = next((s for s in streams if s.get("codec_type") == "video"), None)
    aud = next((s for s in streams if s.get("codec_type") == "audio"), None)

    w = vid.get("width", 0) or 0 if vid else 0
    h = vid.get("height", 0) or 0 if vid else 0

    fps = 0.0
    if vid:
        f_str = vid.get("avg_frame_rate") or vid.get("r_frame_rate") or ""
        if "/" in f_str:
            a, b = f_str.split("/", 1)
            try: fps = float(a) / float(b)
            except (ValueError, ZeroDivisionError): fps = 0.0
        elif f_str:
            try: fps = float(f_str)
            except ValueError: fps = 0.0

    tc = ""
    if vid:
        tc = (vid.get("tags", {}).get("timecode")
              or fmt.get("tags", {}).get("timecode") or "")

    dur = 0.0
    if fmt.get("duration"):
        try: dur = float(fmt["duration"])
        except ValueError: pass

    sz = 0
    if fmt.get("size"):
        try: sz = int(fmt["size"])
        except ValueError: pass

    return VideoMeta(
        filename=os.path.basename(filepath),
        filepath=filepath,
        format_name=fmt.get("format_name", ""),
        duration=dur,
        size=sz,
        video_codec=vid.get("codec_name", "") if vid else "",
        width=w, height=h,
        framerate=fps,
        pixel_format=vid.get("pix_fmt", "") if vid else "",
        timecode=tc,
        audio_codec=aud.get("codec_name", "") if aud else "",
        audio_sample_rate=aud.get("sample_rate", 0) or 0 if aud else 0,
        audio_channels=aud.get("channels", 0) or 0 if aud else 0,
        raw=data,
    )


def probe_available() -> bool:
    return _find_ffprobe() is not None
