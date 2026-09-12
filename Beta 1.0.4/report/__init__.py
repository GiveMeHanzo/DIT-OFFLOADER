"""报告模块：视频元数据提取、抽帧截图、HTML 报告生成。"""
from report.ffprobe_utils import VideoMeta, probe_video, probe_available
from report.frame_extractor import extract_frames, FrameSet
from report.generator import generate_html_report

__all__ = [
    "VideoMeta", "probe_video", "probe_available",
    "extract_frames", "FrameSet",
    "generate_html_report",
]
