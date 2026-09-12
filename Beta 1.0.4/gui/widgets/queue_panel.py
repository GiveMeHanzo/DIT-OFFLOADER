"""右侧栏：任务队列（Job Queue）。"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


STATUS_DISPLAY = {
    "Pending": ("⏳ Pending", "#8b949e"),
    "Queued": ("⏳ Queued", "#8b949e"),
    "Copying": ("⬇ Copying", "#58a6ff"),
    "Verifying": ("✔ Verifying", "#d29922"),
    "Generating Report": ("📄 Generating Report", "#a371f7"),
    "Done": ("✅ Done", "#3fb950"),
    "Error": ("✖ Error", "#ff7b72"),
    "Aborted": ("⏹ Aborted", "#ff7b72"),
    "Skipping": ("⏭ Skipping", "#8b949e"),
}

# 终态：任务不再变化，可被「清空已完成」移除
_TERMINAL_STATUSES = ("Done", "Error", "Aborted")


class JobItemWidget(QFrame):
    cancelRequested = Signal(str)
    # 已完成的卡片被左键点击 → 请求打开目标目录
    openDestRequested = Signal(str)

    def __init__(self, job_name: str, dest_root: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.job_name = job_name
        self.dest_root = dest_root
        self._status = "Pending"
        self._start_ts: Optional[float] = None
        self._build_ui()
        self.set_status("Pending")

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("jobitem")
        self.setStyleSheet(
            "QFrame#jobitem { border: 1px solid #353840; border-radius: 8px; "
            "background: #23272d; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        top = QHBoxLayout()
        self.name_lbl = QLabel(f"<b>{self.job_name}</b>")
        self.name_lbl.setStyleSheet("color: #e6edf3;")
        self.status_lbl = QLabel()
        self.status_lbl.setStyleSheet("font-size: 11px;")
        top.addWidget(self.name_lbl)
        top.addStretch()
        top.addWidget(self.status_lbl)
        lay.addLayout(top)

        self.dest_lbl = QLabel(self.dest_root)
        self.dest_lbl.setStyleSheet("color: #8b949e; font-size: 10px;")
        self.dest_lbl.setWordWrap(True)
        lay.addWidget(self.dest_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        lay.addWidget(self.bar)

        self.detail_lbl = QLabel("等待开始…")
        self.detail_lbl.setStyleSheet("color: #8b949e; font-size: 11px;")
        lay.addWidget(self.detail_lbl)

        bot = QHBoxLayout()
        bot.addStretch()
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setFixedWidth(60)
        self.cancel_btn.setStyleSheet(
            "QPushButton { color: #ff7b72; background: #21262d; "
            "border: 1px solid #5a2f2f; border-radius: 4px; padding: 2px 8px; }"
            "QPushButton:hover { background: #3a1f1f; border-color: #ff7b72; }")
        self.cancel_btn.clicked.connect(
            lambda: self.cancelRequested.emit(self.job_name))
        self.cancel_btn.setVisible(False)
        bot.addWidget(self.cancel_btn)
        lay.addLayout(bot)

    def set_status(self, status: str) -> None:
        self._status = status
        text, color = STATUS_DISPLAY.get(status, (status, "#e0e0e0"))
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight:600;")
        self.cancel_btn.setVisible(status in ("Copying", "Verifying"))
        if status in ("Copying", "Verifying") and self._start_ts is None:
            self._start_ts = time.monotonic()
        # 已完成的卡片整卡可点击打开目标目录
        if status == "Done":
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip("点击打开目标目录")

    @property
    def status(self) -> str:
        return self._status

    def is_finished(self) -> bool:
        """任务是否已到终态（可被「清空已完成」移除）。"""
        return self._status in _TERMINAL_STATUSES

    def mousePressEvent(self, event) -> None:
        if self._status == "Done" and event.button() == Qt.LeftButton:
            self.openDestRequested.emit(self.dest_root)
        super().mousePressEvent(event)

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        self.bar.setValue(min(pct, 100))
        self.bar.setFormat(f"{pct}%  ·  {_human_size(done)} / {_human_size(total)}")
        if self._start_ts is not None:
            elapsed = time.monotonic() - self._start_ts
            speed = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / speed if speed > 0 else 0
            self.detail_lbl.setText(
                f"已用 {elapsed:.0f}s · 速度 {_human_size(speed)}/s · "
                f"预计剩余 {eta:.0f}s")
        # 使用 update() 而非 repaint() ——
        # repaint() 强制同步重绘，多信号连续到达时造成嵌套 paint → QBackingStore 损坏
        self.bar.update()

    def set_result(self, verified: int, failed: int, skipped: int,
                   bytes_total: int, bytes_verified: int, elapsed: float,
                   aborted: bool) -> None:
        if aborted:
            self.set_status("Aborted")
        elif failed > 0:
            self.set_status("Error")
        else:
            self.set_status("Done")
        self.bar.setValue(100 if failed == 0 and not aborted else self.bar.value())
        summary = (
            f"✓ {verified} 验证 · ✗ {failed} 失败 · ⏭ {skipped} 跳过 · "
            f"{_human_size(bytes_verified)} / {_human_size(bytes_total)} · "
            f"耗时 {elapsed:.1f}s")
        self.detail_lbl.setText(summary)
        self.cancel_btn.setVisible(False)


class QueuePanel(QWidget):
    # 「清空已完成」移除的卡片列表，供 MainWindow 清理同名映射
    finishedCleared = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # 按对象身份追踪卡片，不用显示名当 key——同名任务（同一目标
        # 追加拷贝）的卡片会互相覆盖字典条目，导致「清空已完成」漏卡
        self._items: list[JobItemWidget] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("Job Queue")
        title.setStyleSheet("font-weight: 600; font-size: 13px; color: #c0c4d0;")
        head.addWidget(title)
        head.addStretch()
        self.clear_btn = QPushButton("🗑 清空已完成")
        self.clear_btn.setStyleSheet(
            "QPushButton { color: #8b949e; background: #21262d; "
            "border: 1px solid #3e4248; border-radius: 4px; "
            "padding: 2px 8px; font-size: 11px; }"
            "QPushButton:hover { color: #e0e0e0; background: #3a4250; "
            "border-color: #5f9efa; }")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setToolTip("移除所有已完成（含失败/中止）的任务卡片")
        self.clear_btn.clicked.connect(self.clear_finished)
        head.addWidget(self.clear_btn)
        outer.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.inner_layout = QVBoxLayout(inner)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner_layout.setSpacing(8)
        self.inner_layout.addStretch()
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll, stretch=1)

    def add_job(self, job_name: str, dest_root: str) -> JobItemWidget:
        item = JobItemWidget(job_name, dest_root)
        self._items.append(item)
        self.inner_layout.insertWidget(self.inner_layout.count() - 1, item)
        return item

    def get_item(self, job_name: str) -> Optional[JobItemWidget]:
        for it in self._items:
            if it.job_name == job_name:
                return it
        return None

    def remove_item(self, item: JobItemWidget) -> None:
        """从面板移除单个卡片（取消排队中的任务时用）。"""
        if item in self._items:
            self._items.remove(item)
        self.inner_layout.removeWidget(item)
        item.deleteLater()

    def clear_finished(self) -> None:
        """一键移除所有终态（Done/Error/Aborted）任务卡片。"""
        removed = [it for it in self._items if it.is_finished()]
        for it in removed:
            self.inner_layout.removeWidget(it)
            it.deleteLater()
        self._items = [it for it in self._items if it not in removed]
        if removed:
            self.finishedCleared.emit(removed)


__all__ = ["QueuePanel", "JobItemWidget"]
