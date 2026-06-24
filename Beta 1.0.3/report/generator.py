"""report/generator.py — HTML DIT 报告生成（日志驱动 + 并行抽帧 + XXHash-64 校验）。

性能要点：
- **日志驱动**：直接复用 XML 日志中拷贝/校验阶段已写入的 ``hash_dest``，避免对每个
  GB 级 R3D/MXF 文件重新整盘读取计算。重命名阶段已原子地把 ``dest`` 同步为新路径，
  文件内容未变，故存储的 hash 仍有效。
- **并行抽帧/探测**：多个视频用线程池并发处理，单视频内 head/mid/tail 三帧也并发。
- **HTML 构建**：用 ``list`` + ``"".join()`` 取代 ``+=``，避免 O(n²) 字符串拷贝。
"""
from __future__ import annotations

import html
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from config import REPORT_SUFFIX, is_video
from core.scanner import scan_sources
from core.verifier import hash_file
from gui.workers import JobSummary
from report.ffprobe_utils import VideoMeta, probe_available, probe_video
from report.frame_extractor import FrameSet, extract_frames


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_duration(secs: float) -> str:
    if secs <= 0:
        return "-"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    f = int((secs % 1) * 100)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}.{f:02d}"
    return f"{m}:{s:02d}.{f:02d}"


def _fmt_fps(fps: float) -> str:
    if fps <= 0:
        return "-"
    return f"{fps:.2f}"


def _esc(s: object) -> str:
    """HTML 转义用户数据（文件名/路径/校验值/状态等），防止 ``& < > "`` 破坏标签。"""
    return html.escape(str(s), quote=True)


def _img_tag(b64: str, alt: str = "") -> str:
    if not b64:
        return f'<div class="thumb empty">{_esc(alt)}</div>'
    # base64 仅含 [A-Za-z0-9+/=]，无需转义；alt 来自固定文案也转义以保持一致。
    return f'<img class="thumb" src="data:image/jpeg;base64,{b64}" alt="{_esc(alt)}">'


def _compute_checksum(filepath: str) -> str:
    """现场计算 XXHash-64（仅当日志缺失 ``hash_dest`` 时回退使用）。"""
    try:
        return hash_file(filepath)
    except OSError:
        return "ERROR"


def _read_file_statuses(xml_path: str) -> dict[str, dict]:
    """从日志 XML 读取每个源文件的校验状态、实际目标路径与已存储的 hash。

    返回 ``{normcase(src): {src, status, dest, size, hash_dest, hash_src}}``。

    使用日志中的 ``dest`` 而非 ``task.dest_under()`` 的原因：
    - KEEP 策略可能给文件名追加了 -1/-2 后缀；
    - 场记重命名阶段会原子地把 ``dest`` 更新为重命名后的最终路径。
    两种情况下日志里的 ``dest`` 才是磁盘上真实存在的目标文件路径，对应的 ``hash_dest``
    也由拷贝/校验阶段写入且内容未变（重命名只改名）。
    """
    result: dict[str, dict] = {}
    if not xml_path or not os.path.isfile(xml_path):
        return result
    try:
        tree = ET.parse(xml_path)
        for fe in tree.getroot().findall("files/file"):
            src = fe.get("src", "")
            if not src:
                continue
            try:
                size = int(fe.get("size", "0"))
            except ValueError:
                size = 0
            verified = fe.get("verified", "")
            result[os.path.normcase(src)] = {
                "src": src,
                "status": "Verified" if verified == "true" else "Error",
                "dest": fe.get("dest", ""),
                "size": size,
                "hash_dest": fe.get("hash_dest") or None,
                "hash_src": fe.get("hash_src") or None,
            }
    except Exception:
        pass
    return result


def _resolve_path(dest_path: str, renamed: dict[str, str]) -> str:
    """如果 ``dest_path`` 被重命名，返回新路径；否则返回原路径。"""
    norm = os.path.normcase(dest_path)
    return renamed.get(norm, dest_path)


def generate_html_report(
    summary: JobSummary,
    sources: Optional[list[str]] = None,
    renamed_map: Optional[dict[str, str]] = None,
) -> str:
    """生成 HTML DIT 报告。

    Args:
        summary: 拷贝/校验作业汇总（JobSummary 或 VerifyOnlyResult）。
        sources: 源路径列表（仅在无日志回退时用于重新扫描文件清单）。
        renamed_map: 场记重命名映射 ``{旧dest路径: 新dest路径}``。日志驱动的正常路径
                     不依赖它解析路径（日志已含最终 dest），仅用于判定「是否已重命名」
                     以显示 ``Verified and renamed`` 状态。
    """
    renamed = renamed_map if renamed_map else {}
    # 重命名后的最终路径集合，用于判定单个文件是否被改名
    renamed_values = {os.path.normcase(v) for v in renamed.values()}

    # 兼容 JobSummary 与 VerifyOnlyResult 两种汇总类型
    failed = getattr(summary, "failed", 0)
    if failed == 0:
        failed = getattr(summary, "mismatched", 0) + getattr(summary, "missing", 0)
    skipped = getattr(summary, "skipped", 0)
    bytes_verified = getattr(summary, "bytes_verified", 0)
    if bytes_verified == 0:
        bytes_verified = getattr(summary, "bytes_done", 0)

    html_path = os.path.join(
        summary.dest_root,
        f"{summary.job_name}{REPORT_SUFFIX}",
    )

    # 统一的文件记录列表：{src, size, dest, hash(已存储或 None), status, is_video}
    records = _collect_records(summary, sources, renamed)

    video_recs = [r for r in records if r["is_video"]]
    other_recs = [r for r in records if not r["is_video"]]

    can_probe = probe_available()

    # 视频行：多个视频并发处理（probe + 抽帧）
    video_rows = _process_videos(video_recs, can_probe, renamed_values)

    # 非视频行
    other_rows: list[dict] = []
    for r in other_recs:
        checksum = r["hash"] or (
            _compute_checksum(r["dest"]) if os.path.exists(r["dest"]) else "N/A"
        )
        status = r["status"]
        was_renamed = os.path.normcase(r["dest"]) in renamed_values
        if was_renamed and status == "Verified":
            status = "Verified and renamed"
        other_rows.append({
            "filename": os.path.basename(r["src"]),
            "size": _human_size(r["size"]),
            "dest": r["dest"],
            "checksum": checksum,
            "status": status,
        })

    html_str = _render_html(
        job_name=summary.job_name,
        dest_root=summary.dest_root,
        verified=summary.verified,
        failed=failed,
        skipped=skipped,
        bytes_total=summary.bytes_total,
        bytes_verified=bytes_verified,
        elapsed=summary.elapsed,
        video_rows=video_rows,
        other_rows=other_rows,
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_str)
    return html_path


def _collect_records(
    summary: JobSummary,
    sources: Optional[list[str]],
    renamed: dict[str, str],
) -> list[dict]:
    """构建统一的文件记录列表。

    优先走「日志驱动」：直接用 XML 里的 dest/hash_dest（已是重命名后的最终路径，
    内容未变故 hash 仍有效）。
    日志缺失时回退到扫描源路径 + 现场计算。
    """
    file_info = _read_file_statuses(getattr(summary, "xml_path", ""))
    records: list[dict] = []
    if file_info:
        for info in file_info.values():
            records.append({
                "src": info["src"],
                "size": info["size"],
                "dest": info["dest"],            # 最终路径（日志已同步重命名）
                "hash": info["hash_dest"],        # 已存储 hash，可能为 None
                "status": info["status"],
                "is_video": is_video(info["src"]),
            })
        return records

    # 回退：无日志，扫描源并按 task.dest_under + 重命名映射推断路径
    tasks = scan_sources(sources) if sources else []
    for t in tasks:
        dest = _resolve_path(t.dest_under(summary.dest_root), renamed)
        records.append({
            "src": t.src,
            "size": t.size,
            "dest": dest,
            "hash": None,
            "status": "Verified",
            "is_video": t.is_video,
        })
    return records


def _process_videos(
    video_recs: list[dict], can_probe: bool, renamed_values: set[str],
) -> list[dict]:
    """并发处理所有视频：probe 元数据 + 三帧抽帧 + 组装行。"""
    if not video_recs:
        return []

    def handle(rec: dict) -> dict:
        try:
            meta: Optional[VideoMeta] = None
            frames = FrameSet()
            if can_probe:
                meta = probe_video(rec["src"])
                dur = meta.duration if meta else 0.0
                frames = extract_frames(rec["src"], duration=dur)
            checksum = rec["hash"] or (
                _compute_checksum(rec["dest"]) if os.path.exists(rec["dest"]) else "N/A"
            )
            status = rec["status"]
            was_renamed = os.path.normcase(rec["dest"]) in renamed_values
            if was_renamed and status == "Verified":
                status = "Verified and renamed"
            return _build_video_row(rec, meta, frames, checksum, status)
        except Exception:
            return _build_video_row(rec, None, FrameSet(), "ERROR", "Error")

    # 单视频的抽帧已并发，这里再对多视频并发；线程数封顶避免进程风暴
    max_workers = min(4, len(video_recs))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(handle, video_recs))


def _build_video_row(
    rec: dict,
    meta: Optional[VideoMeta],
    frames: FrameSet,
    checksum: str,
    status: str,
) -> dict:
    """组装单个视频行。``status`` 由上层解析（含重命名态），此处不再硬编码。"""
    src = rec["src"]
    return {
        "filename": os.path.basename(src),
        "frames": frames,
        "format": (
            meta.format_name if meta
            else os.path.splitext(src)[1].lstrip(".").upper()
        ),
        "dimensions": (
            f"{meta.width}×{meta.height}" if meta and meta.width else "-"
        ),
        "framerate": _fmt_fps(meta.framerate) if meta else "-",
        "timecode": meta.timecode if meta and meta.timecode else "-",
        "codec": meta.video_codec if meta else "-",
        "duration": _fmt_duration(meta.duration) if meta else "-",
        "size": _human_size(rec["size"]),
        "dest": rec["dest"],
        "checksum": checksum,
        "status": status,
    }


# ──────────────────────────────────────────────
# HTML 模板
# ──────────────────────────────────────────────

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI','PingFang SC','Helvetica Neue',sans-serif;
       font-size: 11px; color: #1a1a1a; background: #f4f5f7; }
.header { background: #1f2937; color: white; padding: 18px 24px; }
.header h1 { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
.header .meta { font-size: 11px; color: #9ca3af; }
.header .meta span { margin-right: 18px; }
.header .meta .pass { color: #3fb950; font-weight: 600; }
.header .meta .fail { color: #ff7b72; font-weight: 600; }
.content { max-width: 1100px; margin: 0 auto; padding: 20px 16px; }
.section-title { font-size: 14px; font-weight: 600; margin: 20px 0 10px;
                 color: #1f2937; border-bottom: 2px solid #1f2937;
                 padding-bottom: 4px; }
.card { background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
        padding: 16px; margin-bottom: 16px; }
.thumbs { display: flex; gap: 10px; margin-bottom: 12px; }
.thumb { width: 300px; height: 169px; object-fit: cover; border-radius: 6px;
         border: 1px solid #e5e7eb; }
.thumb.empty { background: #f9fafb; display: flex; align-items: center;
               justify-content: center; color: #9ca3af; font-size: 12px; }
/* 双列表格 */
.two-col { display: flex; gap: 20px; }
.two-col table { flex: 1; }
table { width: 100%; border-collapse: collapse; font-size: 11px; }
td { padding: 5px 8px; border-bottom: 1px solid #f0f0f0; }
td.label { color: #6b7280; width: 120px; font-weight: 500; white-space: nowrap; }
td.value { color: #111827; word-break: break-all; font-family: 'Consolas','Menlo',monospace; }
td.value.path { font-size: 9px; font-family: 'Consolas','Menlo',monospace; }
td.value.hash { font-size: 10px; color: #d97706; font-family: 'Consolas','Menlo',monospace; }
td.value.error { color: #dc2626; font-weight: 600; }
td.value.pass { color: #16a34a; font-weight: 600; }
.footer { text-align: center; color: #9ca3af; font-size: 10px;
          padding: 24px 0 16px; }
.footer span { margin: 0 12px; }
.sep { border-top: 2px solid #e5e7eb; margin: 12px 0; }
"""


def _render_html(
    job_name: str,
    dest_root: str,
    verified: int,
    failed: int,
    skipped: int,
    bytes_total: int,
    bytes_verified: int,
    elapsed: float,
    video_rows: list[dict],
    other_rows: list[dict],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = verified + failed + skipped
    status_cls = "pass" if failed == 0 else "fail"
    status_text = "全部通过" if failed == 0 else f"{failed} 个失败"

    # ── 视频表格（双列）── 用 list 收集片段再 join，避免 O(n²) 字符串拼接
    parts: list[str] = []
    for vr in video_rows:
        frames = vr["frames"]
        parts.append('<div class="card">')
        parts.append('<div class="thumbs">')
        parts.append(_img_tag(frames.head, "首帧"))
        parts.append(_img_tag(frames.mid, "中部 50%"))
        parts.append(_img_tag(frames.tail, "尾帧"))
        parts.append('</div>')
        parts.append('<div class="two-col">')

        # 左列
        parts.append('<table>')
        for lbl, key in [
            ("Filename", "filename"),
            ("Format", "format"),
            ("Dimensions", "dimensions"),
            ("Codec", "codec"),
            ("Duration", "duration"),
            ("Size", "size"),
        ]:
            cls = "value path" if key == "filename" else "value"
            parts.append(
                f'<tr><td class="label">{lbl}</td>'
                f'<td class="{cls}">{_esc(vr[key])}</td></tr>'
            )
        parts.append('</table>')

        # 右列
        parts.append('<table>')
        for lbl, key in [
            ("Framerate", "framerate"),
            ("Start Timecode", "timecode"),
            ("XXHash-64 Checksum", "checksum"),
            ("Destination", "dest"),
            ("Status", "status"),
        ]:
            if key == "checksum":
                cls = "value hash"
            elif key == "dest":
                cls = "value path"
            elif key == "status":
                cls = "value error" if "Error" in str(vr.get(key, "")) else "value pass"
            else:
                cls = "value"
            parts.append(
                f'<tr><td class="label">{lbl}</td>'
                f'<td class="{cls}">{_esc(vr[key])}</td></tr>'
            )
        parts.append('</table>')

        parts.append('</div></div>\n')

    video_html = "".join(parts)

    # ── 非视频表格 ──
    other_parts: list[str] = []
    if other_rows:
        other_parts.append('<div class="card"><table>')
        other_parts.append(
            '<tr style="font-weight:600">'
            '<td class="label">Filename</td>'
            '<td class="label">Size</td>'
            '<td class="label">XXHash-64 Checksum</td>'
            '<td class="label">Destination</td>'
            '<td class="label">Status</td></tr>'
        )
        for or_ in other_rows:
            row_cls = "value error" if "Error" in str(or_.get("status", "")) else "value pass"
            other_parts.append(
                f'<tr>'
                f'<td class="value path">{_esc(or_["filename"])}</td>'
                f'<td class="value">{_esc(or_["size"])}</td>'
                f'<td class="value hash">{_esc(or_["checksum"])}</td>'
                f'<td class="value path">{_esc(or_["dest"])}</td>'
                f'<td class="{row_cls}">{_esc(or_["status"])}</td>'
                f'</tr>'
            )
        other_parts.append('</table></div>\n')

    other_html = "".join(other_parts)

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DIT Offload Report — {_esc(job_name)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="header">
  <h1>DIT Offload Report</h1>
  <div class="meta">
    <span>Job: {_esc(job_name)}</span>
    <span>目标: {_esc(os.path.basename(dest_root))}</span>
    <span>文件: {total}</span>
    <span>体积: {_human_size(bytes_verified)} / {_human_size(bytes_total)}</span>
    <span>耗时: {elapsed:.1f}s</span>
    <span class="{status_cls}">状态: {status_text}</span>
  </div>
</div>
<div class="content">
  <div class="section-title">Video Files ({len(video_rows)})</div>
  {video_html or '<p style="color:#9ca3af;padding:8px">（无视频文件）</p>'}
  <div class="section-title">Non-Video Files ({len(other_rows)})</div>
  {other_html or '<p style="color:#9ca3af;padding:8px">（无非视频文件）</p>'}
</div>
<div class="footer">
  <span>DIT Offload v1.0</span>
  <span>生成时间: {now}</span>
</div>
</body>
</html>"""
