"""pushplus 微信推送设置对话框。

设置项通过 QSettings 持久化（用户目录，打包后依然有效）：
    pushplus/enabled — 推送总开关
    pushplus/token   — pushplus 密钥

QSettings 依赖 main.py 里 QApplication 设置的
OrganizationName / ApplicationName，请勿在应用初始化之前调用读写函数。
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QSettings, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from core.notifier import send_pushplus

_TOKEN_KEY = "pushplus/token"
_ENABLED_KEY = "pushplus/enabled"

_TEST_TITLE = "DIT-测试消息"
_TEST_CONTENT = (
    "<b>DIT OFFLOADER</b><ul><li>这是一条测试消息</li>"
    "<li>收到即说明微信推送配置成功</li></ul>"
)


def load_pushplus_settings(settings: Optional[QSettings] = None) -> tuple[bool, str]:
    """读取推送设置，返回 (是否开启, token)。"""
    s = settings or QSettings()
    token = str(s.value(_TOKEN_KEY, "") or "")
    enabled = s.value(_ENABLED_KEY, False, type=bool)
    return bool(enabled), token.strip()


def save_pushplus_settings(
    enabled: bool, token: str, settings: Optional[QSettings] = None
) -> None:
    """保存推送设置并立即落盘。"""
    s = settings or QSettings()
    s.setValue(_ENABLED_KEY, bool(enabled))
    s.setValue(_TOKEN_KEY, (token or "").strip())
    s.sync()


class SettingsDialog(QDialog):
    """设置窗口：推送开关 + Token 配置 + 测试推送。"""

    _test_done = Signal(bool, str)  # 测试推送结果（HTTP 线程 → 主线程）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 — 微信推送（pushplus）")
        self.setMinimumWidth(480)
        self._test_thread: Optional[threading.Thread] = None

        enabled, token = load_pushplus_settings()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        self.push_cb = QCheckBox("开启微信推送（拷贝任务结束后推送到微信）")
        self.push_cb.setChecked(enabled)
        self.push_cb.toggled.connect(self._on_toggle)
        root.addWidget(self.push_cb)

        hint = QLabel(
            "消息通过 pushplus 推送到微信。登录 pushplus 官网后，在"
            "「一对一推送」页面复制您的 Token 粘贴到下方。"
            "Token 只保存在本机，不会随任务数据外发。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e; border: none; background: transparent;")
        root.addWidget(hint)

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_lbl = QLabel("Token：")
        token_lbl.setStyleSheet("border: none; background: transparent;")
        self.token_edit = QLineEdit(token)
        self.token_edit.setPlaceholderText("pushplus 密钥（一串字母数字）")
        token_row.addWidget(token_lbl)
        token_row.addWidget(self.token_edit, stretch=1)
        root.addLayout(token_row)

        link_row = QHBoxLayout()
        link_row.addStretch()
        link = QLabel(
            '<a href="https://www.pushplus.plus/" style="color: #5f9efa;">'
            "获取 Token（pushplus 官网）↗</a>"
        )
        link.setOpenExternalLinks(True)
        link_row.addWidget(link)
        root.addLayout(link_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setStyleSheet("border: none; background: transparent;")
        root.addWidget(self.result_lbl)

        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("测试推送")
        self.test_btn.setToolTip("用当前 Token 发送一条测试消息到微信")
        self.test_btn.clicked.connect(self._on_test_push)
        btn_row.addWidget(self.test_btn)
        btn_row.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #2c7be5; border: 1px solid #2c7be5; }"
            "QPushButton:hover { background-color: #3d8cf5; border-color: #3d8cf5; }"
            "QPushButton:pressed { background-color: #1f5fbf; }"
        )
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

        self._test_done.connect(self._on_test_done)
        self._on_toggle(enabled)  # 初始联动禁用态

    # ── 交互 ──
    def _on_toggle(self, checked: bool) -> None:
        """开关联动：关闭推送时置灰 Token 输入与测试按钮。"""
        self.token_edit.setEnabled(checked)
        self.test_btn.setEnabled(checked and not self._testing)

    @property
    def _testing(self) -> bool:
        return (
            self._test_thread is not None and self._test_thread.is_alive()
        )

    def _on_test_push(self) -> None:
        token = self.token_edit.text().strip()
        if not token:
            self._show_result(False, "请先输入 Token")
            return
        self.test_btn.setEnabled(False)
        self._show_result(None, "正在发送测试消息…")

        def work() -> None:
            ok, msg = send_pushplus(token, _TEST_TITLE, _TEST_CONTENT)
            self._test_done.emit(ok, msg)

        self._test_thread = threading.Thread(
            target=work, daemon=True, name="pushplus-test"
        )
        self._test_thread.start()

    @Slot(bool, str)
    def _on_test_done(self, ok: bool, msg: str) -> None:
        self.test_btn.setEnabled(self.push_cb.isChecked())
        if ok:
            self._show_result(True, "测试消息已发送，请在微信中查收。")
        else:
            self._show_result(False, f"发送失败：{msg}")

    def _show_result(self, ok: Optional[bool], text: str) -> None:
        color = {True: "#3fb950", False: "#ff7b72", None: "#8b949e"}[ok]
        self.result_lbl.setText(f'<span style="color:{color};">{text}</span>')

    def _on_save(self) -> None:
        save_pushplus_settings(
            self.push_cb.isChecked(), self.token_edit.text()
        )
        self.accept()
