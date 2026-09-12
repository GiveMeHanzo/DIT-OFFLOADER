"""pushplus 推送功能单元测试（离线，不联网）。

覆盖：
- 消息构建三态标题（成功/校验失败/中断）与必含字段
- send_pushplus 的 payload 格式、成功/失败/异常分支（mock urlopen）
- QSettings 设置读写往返（INI 临时文件，不污染真实配置）

运行：python pushplus_test.py
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError

from core.notifier import (
    TITLE_ABORTED,
    TITLE_FAILED,
    TITLE_PREFIX,
    TITLE_SUCCESS,
    build_copy_result_message,
    send_pushplus,
)


class _FakeResponse:
    """替代 urlopen 返回值的假响应（支持 with 上下文）。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MessageBuildTests(unittest.TestCase):
    """build_copy_result_message 的标题与内容规则。"""

    def test_success_title_and_fields(self):
        title, html = build_copy_result_message(
            job_name="J", total=10, video=7, nonvideo=3,
            verified=10, failed=0, skipped=0, aborted=False,
        )
        self.assertEqual(title, TITLE_SUCCESS)
        self.assertEqual(title, "DIT-拷贝成功")
        self.assertIn("DIT-拷贝成功", html)  # 正文头也带标题
        self.assertIn("任务名称：</b>J", html)
        self.assertIn("拷贝文件数：</b>10", html)
        self.assertIn("视频文件数：</b>7", html)
        self.assertIn("非视频文件数：</b>3", html)
        self.assertIn("完成时间：</b>", html)
        self.assertNotIn("校验未通过", html)
        self.assertNotIn("任务已中断", html)

    def test_failed_title_and_failed_count(self):
        title, html = build_copy_result_message(
            job_name="J", total=10, video=7, nonvideo=3,
            verified=8, failed=2, skipped=0, aborted=False,
        )
        self.assertEqual(title, TITLE_FAILED)
        self.assertEqual(title, "DIT-拷贝失败")
        self.assertIn("校验未通过文件：2 个", html)
        self.assertNotIn("任务已中断", html)

    def test_aborted_title_and_note(self):
        title, html = build_copy_result_message(
            job_name="J", total=10, video=7, nonvideo=3,
            verified=4, failed=1, skipped=0, aborted=True,
        )
        self.assertEqual(title, TITLE_ABORTED)
        self.assertEqual(title, "DIT-拷贝中断")
        self.assertIn("任务已中断，未完成拷贝", html)
        self.assertIn("已验证 4 / 共 10 个", html)

    def test_job_name_escaped(self):
        _, html = build_copy_result_message(
            job_name="<J&1>", total=1, video=1, nonvideo=0,
            verified=1, failed=0, skipped=0, aborted=False,
        )
        self.assertNotIn("<J&1>", html)
        self.assertIn("&lt;J&amp;1&gt;", html)


class SendPushplusTests(unittest.TestCase):
    """send_pushplus 的 HTTP 行为（mock urlopen，不联网）。"""

    def _send(self, response=None, side_effect=None, token="tok123"):
        kwargs = {}
        if response is not None:
            kwargs["return_value"] = response
        if side_effect is not None:
            kwargs["side_effect"] = side_effect
        with mock.patch("urllib.request.urlopen", **kwargs) as m:
            ok, msg = send_pushplus(token, "T", "<b>x</b>")
        return ok, msg, m

    def test_success_and_payload(self):
        resp = _FakeResponse(
            json.dumps({"code": 200, "msg": 0, "data": "ok"}).encode("utf-8")
        )
        ok, msg, m = self._send(response=resp)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        req = m.call_args.args[0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {
            "token": "tok123",
            "title": "T",
            "content": "<b>x</b>",
            "template": "html",
        })
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers.get("content-type"), "application/json")

    def test_api_error_code(self):
        resp = _FakeResponse(
            json.dumps({"code": 903, "msg": "无效Token"}).encode("utf-8")
        )
        ok, msg, _ = self._send(response=resp)
        self.assertFalse(ok)
        self.assertIn("903", msg)

    def test_network_exception(self):
        ok, msg, _ = self._send(side_effect=URLError("connection refused"))
        self.assertFalse(ok)
        self.assertTrue(msg)  # 有可读原因

    def test_empty_token_short_circuits(self):
        with mock.patch("urllib.request.urlopen") as m:
            ok, msg = send_pushplus("", "T", "<b>x</b>")
        self.assertFalse(ok)
        self.assertIn("Token", msg)
        m.assert_not_called()  # 空 Token 不发起网络请求


class SettingsRoundtripTests(unittest.TestCase):
    """QSettings 读写往返（INI 临时文件，不触碰真实用户配置）。"""

    def test_roundtrip(self):
        from PySide6.QtCore import QCoreApplication, QSettings
        from gui.widgets.settings_dialog import (
            load_pushplus_settings,
            save_pushplus_settings,
        )

        if QCoreApplication.instance() is None:
            self._app = QCoreApplication([])  # noqa - 保持引用防止 GC

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "settings.ini")
            s1 = QSettings(path, QSettings.IniFormat)
            save_pushplus_settings(True, " abc123 ", s1)
            s2 = QSettings(path, QSettings.IniFormat)
            enabled, token = load_pushplus_settings(s2)
            self.assertTrue(enabled)
            self.assertEqual(token, "abc123")  # 去除首尾空白

    def test_defaults_on_fresh_settings(self):
        from PySide6.QtCore import QCoreApplication, QSettings
        from gui.widgets.settings_dialog import load_pushplus_settings

        if QCoreApplication.instance() is None:
            self._app = QCoreApplication([])

        with tempfile.TemporaryDirectory() as td:
            s = QSettings(os.path.join(td, "empty.ini"), QSettings.IniFormat)
            enabled, token = load_pushplus_settings(s)
            self.assertFalse(enabled)
            self.assertEqual(token, "")


class JobSummaryFieldsTests(unittest.TestCase):
    """JobSummary 新增统计字段有默认值，旧调用不破坏。"""

    def test_default_fields(self):
        from gui.workers import JobSummary
        s = JobSummary(
            job_name="J", dest_root="/tmp", verified=1, failed=0,
            skipped=0, bytes_total=1, bytes_verified=1, elapsed=1.0,
            aborted=False, generate_report=False, xml_path="x",
        )
        self.assertEqual(s.total_files, 0)
        self.assertEqual(s.video_files, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
