"""SSCL 场记板驱动的媒体素材重命名模块。

根据 _SSCL.xml 中每条 clip 的场景/镜次/条次/机位信息，
自动搜索 media_dir 下匹配的视音频素材及 XML 附属文件，
按规范前缀重命名，并同步更新 _log.xml 中的 dest 路径。

主要入口
--------
``rename_media_assets(sscl_path, log_path, media_dir) -> RenameResult``
    解析 SSCL → 扫描素材目录 → 匹配并重命名 → 回写日志 XML。
    返回结构化结果供主程序判定成功/部分成功/失败。

重命名格式
----------
新文件名 = ``SC{Scene}_S{Shot}_T{Take}_{Camera}_`` + 原始文件名

示例
----
A001C001.mp4  → SC001_S001_T001_A_A001C001.mp4
0001.wav       → SC001_S001_T001_A_0001.wav
121_1949M01.XML → SC001_S001_T001_A_121_1949M01.XML
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 目标文件扩展名（仅扫描和重命名这些格式）
# ---------------------------------------------------------------------------

VIDEO_EXT: frozenset[str] = frozenset({".mp4", ".mov", ".mxf", ".r3d", ".nev"})
AUDIO_EXT: frozenset[str] = frozenset({".wav", ".mp3", ".bwf"})
SIDECAR_EXT: frozenset[str] = frozenset({".xml"})

ALL_TARGET_EXT: frozenset[str] = VIDEO_EXT | AUDIO_EXT | SIDECAR_EXT

# 前缀标识：已改名文件跳过，防止重复操作
RENAMED_PREFIX = "SC"

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class RenameResult:
    """重命名操作的完整结果。

    供主程序根据 status 字段判断整体成败，并通过 renamed/errors/not_found
    列表向用户展示详细信息。
    """

    status: str = "success"  # "success" | "partial" | "error"
    total_clips: int = 0
    renamed_count: int = 0
    skipped_count: int = 0
    not_found_count: int = 0
    error_count: int = 0
    renamed: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    not_found: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 主调函数
# ---------------------------------------------------------------------------


def rename_media_assets(
    sscl_path: str,
    log_path: str,
    media_dir: str,
) -> RenameResult:
    """根据 SSCL 场记板重命名媒体素材并更新日志。

    Args:
        sscl_path: _SSCL.xml 文件绝对路径。
        log_path: _log.xml 文件绝对路径。
        media_dir: 素材根目录（递归搜索）。

    Returns:
        RenameResult 结构化结果。
    """
    result = RenameResult()

    # ---- 校验 media_dir ----
    if not os.path.isdir(media_dir):
        result.status = "error"
        result.errors.append(f"素材目录不存在: {media_dir}")
        return result

    # ---- Step 1: 解析 SSCL ----
    clips = _parse_sscl(sscl_path)
    if clips is None:
        result.status = "error"
        result.errors.append(f"解析 SSCL 文件失败: {sscl_path}")
        return result
    result.total_clips = len(clips)

    # ---- Step 2: 解析 Log XML（内存 DOM，稍后修改） ----
    log_tree = _parse_log(log_path)
    if log_tree is None:
        result.status = "error"
        result.errors.append(f"解析日志文件失败: {log_path}")
        return result

    # ---- Step 3: 扫描素材目录 ----
    media_files = _scan_media(media_dir)  # {normcase_path: filename}

    # ---- Step 4: 匹配并重命名 ----
    renamed_map: dict[str, str] = {}  # old_path → new_path（用于日志更新）

    for clip in clips:
        prefix = _build_prefix(clip)
        video_val = clip.get("videoFile", "")
        audio_val = clip.get("audioFile", "")
        matched_this_clip = False

        # 遍历候选文件（快照迭代，允许在循环中删除字典条目）
        for old_path, filename in list(media_files.items()):
            # 跳过已有前缀的文件
            if filename[:2].upper() == RENAMED_PREFIX:
                result.skipped_count += 1
                del media_files[old_path]
                continue

            # 匹配 videoFile 或 audioFile（包含匹配，不区分大小写）
            fn_lower = filename.lower()
            hit_video = bool(video_val) and (video_val.lower() in fn_lower)
            hit_audio = bool(audio_val) and (audio_val.lower() in fn_lower)

            if not (hit_video or hit_audio):
                continue

            # 匹配成功 → 重命名
            new_name = prefix + filename
            new_path = os.path.join(os.path.dirname(old_path), new_name)

            file_type = _classify_file(filename)
            try:
                os.rename(old_path, new_path)
                result.renamed.append({
                    "old_path": old_path,
                    "new_path": new_path,
                    "clip_id": clip.get("id", ""),
                    "type": file_type,
                })
                renamed_map[old_path] = new_path
                result.renamed_count += 1
                matched_this_clip = True
            except OSError as e:
                result.errors.append(
                    f"重命名失败 (clip {clip.get('id')}): {old_path} → {new_path} | {e}"
                )
                result.error_count += 1

            # 已处理，移出候选池
            del media_files[old_path]

        if not matched_this_clip:
            result.not_found.append({
                "clip_id": clip.get("id", ""),
                "videoFile": video_val,
                "audioFile": audio_val,
            })
            result.not_found_count += 1

    # ---- Step 5: 更新 Log XML ----
    if renamed_map:
        _update_log(log_path, log_tree, renamed_map)

    # ---- 判定最终状态 ----
    if result.error_count > 0 and result.renamed_count == 0:
        result.status = "error"
    elif result.error_count > 0 or result.not_found_count > 0:
        result.status = "partial"

    return result


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _parse_sscl(path: str) -> Optional[list[dict]]:
    """解析 SSCL XML，返回 clip 字典列表；失败返回 None。"""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except (ET.ParseError, OSError) as e:
        # OSError 涵盖 FileNotFoundError 等
        return None

    if root.tag != "clapperboard":
        return None

    clips = []
    for elem in root.findall(".//clip"):
        clip = {
            "id": elem.get("id", ""),
            "scene": (elem.findtext("scene") or "").strip(),
            "shot": (elem.findtext("shot") or "").strip(),
            "take": (elem.findtext("take") or "").strip(),
            "camera": (elem.findtext("camera") or "").strip(),
            "videoFile": (elem.findtext("videoFile") or "").strip(),
            "audioFile": (elem.findtext("audioFile") or "").strip(),
        }
        # 至少需要有 scene/shot/take/camera 才能构建前缀
        if clip["scene"] and clip["shot"] and clip["take"] and clip["camera"]:
            clips.append(clip)
    return clips


def _parse_log(path: str) -> Optional[ET.ElementTree]:
    """解析日志 XML 返回 ElementTree；失败返回 None。"""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None
    if tree.getroot().tag != "job":
        return None
    return tree


def _scan_media(media_dir: str) -> dict[str, str]:
    """递归扫描目录，返回 {os.path.normcase(完整路径): 文件名}。

    仅收集扩展名在 ALL_TARGET_EXT 内的文件。
    """
    result: dict[str, str] = {}
    for root, _dirs, files in os.walk(media_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ALL_TARGET_EXT:
                full = os.path.join(root, f)
                result[os.path.normcase(full)] = f
    return result


def _build_prefix(clip: dict) -> str:
    """构造命名前缀 SC{Scene}_S{Shot}_T{Take}_{Camera}_"""
    return (
        f"SC{clip['scene']}"
        f"_S{clip['shot']}"
        f"_T{clip['take']}"
        f"_{clip['camera']}_"
    )


def _classify_file(filename: str) -> str:
    """根据扩展名返回文件类别标签：'video' / 'audio' / 'xml' / 'unknown'"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in SIDECAR_EXT:
        return "xml"
    return "unknown"


def _update_log(
    log_path: str,
    tree: ET.ElementTree,
    renamed_map: dict[str, str],
) -> None:
    """将日志中 dest 属性匹配旧文件名的 <file> 节点更新为新路径。

    匹配策略：按 **文件名** (basename) 匹配，而非完整路径。
    这是因为主程序生成的日志中目录路径格式可能与实际文件系统不完全一致
    （如空格 vs 下划线、正斜杠 vs 反斜杠等），但文件名是稳定的匹配锚点。

    新 dest = 原 dest 的目录部分 + 新文件名。
    使用原子写（.tmp → os.replace）避免半写损坏，与 JobLogger._write 一致。
    """
    files_el = tree.getroot().find("files")
    if files_el is None:
        return

    # 构建文件名映射：{normcase(旧文件名): 新文件名}
    basename_map: dict[str, str] = {}
    for old_path, new_path in renamed_map.items():
        old_base = os.path.basename(old_path)
        new_base = os.path.basename(new_path)
        basename_map[os.path.normcase(old_base)] = new_base

    updated = 0
    for fe in files_el.findall("file"):
        dest = fe.get("dest", "")
        if not dest:
            continue
        dest_base = os.path.basename(dest)
        norm_base = os.path.normcase(dest_base)
        if norm_base in basename_map:
            dest_dir = os.path.dirname(dest)
            new_dest = os.path.join(dest_dir, basename_map[norm_base])
            fe.set("dest", new_dest)
            updated += 1

    if updated == 0:
        return  # 无需写入

    # 原子写
    tmp = log_path + ".tmp"
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, log_path)


__all__ = [
    "RenameResult",
    "rename_media_assets",
    "VIDEO_EXT",
    "AUDIO_EXT",
    "SIDECAR_EXT",
]
