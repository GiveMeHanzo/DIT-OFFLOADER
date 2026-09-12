"""pushplus 微信推送（拷贝任务完成/失败/中断通知）。

接口：POST http://www.pushplus.plus/send
    {"token": "...", "title": "...", "content": "<html>", "template": "html"}
响应 {"code": 200, ...} 表示受理成功。

纯标准库实现（urllib），不引入第三方依赖；HTTP 调用必须放在
非 GUI 线程执行（调用方负责），避免阻塞主线程。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from html import escape as _esc
from typing import Optional

PUSHPLUS_API = "http://www.pushplus.plus/send"
REQUEST_TIMEOUT = 15  # 秒

TITLE_PREFIX = "DIT"
TITLE_SUCCESS = f"{TITLE_PREFIX}-拷贝成功"
TITLE_FAILED = f"{TITLE_PREFIX}-拷贝失败"
TITLE_ABORTED = f"{TITLE_PREFIX}-拷贝中断"

# 主题色（与 GUI 深色主题的状态色一致，微信内联样式可用）
_COLOR_ERROR = "#e5534b"
_COLOR_OK = "#1a7f37"


def send_pushplus(
    token: str,
    title: str,
    content_html: str,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[bool, str]:
    """发送一条 pushplus 消息。返回 (是否成功, 失败原因)。

    网络异常、超时、非 200 响应码都视为失败并给出可读原因。
    """
    if not token:
        return False, "未配置推送 Token"
    payload = json.dumps(
        {
            "token": token,
            "title": title,
            "content": content_html,
            "template": "html",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - 网络层异常统一转可读原因
        return False, str(e)

    code = body.get("code") if isinstance(body, dict) else None
    if code == 200:
        return True, ""
    msg = body.get("msg", "") if isinstance(body, dict) else str(body)
    return False, f"pushplus 返回 code={code}: {msg}"


def build_copy_result_message(
    job_name: str,
    total: int,
    video: int,
    nonvideo: int,
    verified: int,
    failed: int,
    skipped: int,
    aborted: bool,
    finished_at: Optional[datetime] = None,
) -> tuple[str, str]:
    """构建拷贝结果消息。返回 (标题, HTML 正文)。

    标题三态：成功 / 校验失败 / 中断（aborted 优先于 failed）。
    正文为 pushplus html 模板：加粗 + 列表富文本。
    """
    finished_at = finished_at or datetime.now()
    if aborted:
        title = TITLE_ABORTED
    elif failed > 0:
        title = TITLE_FAILED
    else:
        title = TITLE_SUCCESS

    head_style = "" if title == TITLE_SUCCESS else f' style="color:{_COLOR_ERROR};"'
    items = [
        f"<b>任务名称：</b>{_esc(job_name)}",
        f"<b>拷贝文件数：</b>{total}",
        f"<b>视频文件数：</b>{video}",
        f"<b>非视频文件数：</b>{nonvideo}",
        f"<b>完成时间：</b>{finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if aborted:
        items.append(
            f'<span style="color:{_COLOR_ERROR};"><b>任务已中断，未完成拷贝</b>'
            f"（已验证 {verified} / 共 {total} 个）</span>"
        )
    elif failed > 0:
        items.append(
            f'<span style="color:{_COLOR_ERROR};">'
            f"<b>校验未通过文件：{failed} 个</b></span>"
        )
    else:
        items.append(f'<span style="color:{_COLOR_OK};"><b>全部校验通过</b></span>')

    html = f"<b{head_style}>{title}</b><ul>" + "".join(
        f"<li>{item}</li>" for item in items
    ) + "</ul>"
    return title, html
