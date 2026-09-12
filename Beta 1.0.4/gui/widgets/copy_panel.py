"""中间栏：Copy From / Copy To 配置区 + 全局选项（深色风格）。

上半部「Copy From」：源文件/文件夹池，支持拖拽与动画按钮。
下半部「Copy To」：单个目标根目录。
底部：「开始拷贝」+「仅校验」双按钮 + 全局报告勾选。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# ───────────────── 微动画按钮 ─────────────────

class _AnimButton(QPushButton):
    """轻量 hover/press 动画按钮：hover 时微微提亮，press 时下压。"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self._normal = (
            "QPushButton { background-color: #303540; border: 1px solid #4a4e55;"
            "border-radius: 5px; padding: 5px 14px; color: #e0e0e0; }"
        )
        self.setStyleSheet(self._normal)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.setStyleSheet(
            "QPushButton { background-color: #3a4250; border: 1px solid #5f9efa;"
            "border-radius: 5px; padding: 5px 14px; color: #ffffff; }"
        )
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.setStyleSheet(self._normal)
        super().leaveEvent(event)

# ───────────────── 主要类 ─────────────────


class _DropList(QListWidget):
    """支持拖拽的源池列表。"""
    pathsDropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.toLocalFile() and os.path.exists(url.toLocalFile())]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()


class _DropEdit(QLineEdit):
    """支持拖拽文件夹的目标输入框。"""
    pathDropped = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        local = event.mimeData().urls()[0].toLocalFile()
        if local and os.path.isdir(local):
            self.pathDropped.emit(local)
            event.acceptProposedAction()


class _Section(QFrame):
    """带标题的分组卡片。"""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("section")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 8, 10, 10)
        self._lay.setSpacing(6)
        lbl = QLabel(title)
        lbl.setStyleSheet("font-weight: 600; font-size: 12px; color: #a0a4b0;")
        self._lay.addWidget(lbl)

    def addWidget(self, w: QWidget) -> None:  # type: ignore[override]
        self._lay.addWidget(w)

    def addLayout(self, l) -> None:  # type: ignore[override]
        self._lay.addLayout(l)

    def addStretch(self) -> None:  # type: ignore[override]
        self._lay.addStretch()


class CopyPanel(QWidget):
    """中间配置区。"""

    startRequested = Signal()   # 开始拷贝
    verifyRequested = Signal()  # 仅校验
    configChanged = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # ===== Copy From =====
        src_sec = _Section("⬆  Copy From （原文件/文件夹）")
        self.src_list = _DropList()
        self.src_list.pathsDropped.connect(self._add_sources)
        self.src_list.itemDoubleClicked.connect(self._reveal_source)
        src_sec.addWidget(self.src_list)

        src_btn_row = QHBoxLayout()
        self.add_files_btn = _AnimButton("＋ 添加文件…")
        self.add_files_btn.clicked.connect(self._pick_files)
        self.add_dir_btn = _AnimButton("📂 添加文件夹…")
        self.add_dir_btn.clicked.connect(self._pick_dir)
        self.sel_all_btn = _AnimButton("☑ 全选")
        self.sel_all_btn.clicked.connect(self._select_all_sources)
        self.rm_src_btn = _AnimButton("✕ 移除选中")
        self.rm_src_btn.clicked.connect(self._remove_selected_sources)
        src_btn_row.addWidget(self.add_files_btn)
        src_btn_row.addWidget(self.add_dir_btn)
        src_btn_row.addStretch()
        src_btn_row.addWidget(self.sel_all_btn)
        src_btn_row.addWidget(self.rm_src_btn)
        src_sec.addLayout(src_btn_row)

        self.src_count_lbl = QLabel("源：0 项")
        self.src_count_lbl.setStyleSheet("color: #808590; font-size: 11px;")
        src_sec.addWidget(self.src_count_lbl)
        layout.addWidget(src_sec)

        # ===== Copy To =====
        dst_sec = _Section("⬇  Copy To （目标位置）")
        dst_row = QHBoxLayout()
        self.dst_edit = _DropEdit()
        self.dst_edit.setPlaceholderText("拖入目标文件夹，或点击右侧选择…")
        self.dst_edit.pathDropped.connect(self._set_dest)
        self.dst_edit.textChanged.connect(lambda _: self.configChanged.emit())
        dst_browse = _AnimButton("…")
        dst_browse.setFixedWidth(36)
        dst_browse.clicked.connect(self._pick_dest)
        dst_row.addWidget(self.dst_edit)
        dst_row.addWidget(dst_browse)
        dst_sec.addLayout(dst_row)

        # Job 名称
        job_row = QHBoxLayout()
        job_row.addWidget(QLabel("Job 名称："))
        self.job_name_edit = QLineEdit()
        self.job_name_edit.setPlaceholderText("如 2026-06-20_A001")
        self.job_name_edit.textChanged.connect(lambda _: self.configChanged.emit())
        job_row.addWidget(self.job_name_edit, stretch=1)
        dst_sec.addLayout(job_row)
        layout.addWidget(dst_sec)

        # ===== 场记重命名 =====
        rename_sec = _Section("🏷  素材自动重命名 （需使用电子场记单： givemehanzo.github.io/sscl/）")
        sscl_row = QHBoxLayout()
        self.sscl_import_btn = _AnimButton("导入 _SSCL.xml…")
        self.sscl_import_btn.clicked.connect(self._pick_sscl)
        self.sscl_edit = QLineEdit()
        self.sscl_edit.setPlaceholderText("未导入场记文件")
        self.sscl_edit.setReadOnly(True)
        self.sscl_edit.textChanged.connect(lambda _: self.configChanged.emit())
        sscl_row.addWidget(self.sscl_import_btn)
        sscl_row.addWidget(self.sscl_edit, stretch=1)
        rename_sec.addLayout(sscl_row)

        self.rename_cb = QCheckBox("根据场记 XML 文件自动重命名素材")
        self.rename_cb.setChecked(False)
        self.rename_cb.setToolTip(
            "勾选后，拷贝/校验完成后将根据 _SSCL.xml 中的场记信息\n"
            "按 SC{场}_S{镜}_T{次}_{机位}_ 格式自动重命名目标素材。"
        )
        self.rename_cb.toggled.connect(lambda _: self.configChanged.emit())
        rename_sec.addWidget(self.rename_cb)
        layout.addWidget(rename_sec)

        # ===== 重名策略 =====
        pol_sec = _Section("重名处理")
        self.policy_combo = QComboBox()
        self.policy_combo.addItem("保留（追加 -1, -2，不覆盖）", "keep")
        self.policy_combo.addItem("跳过已存在", "skip")
        pol_sec.addWidget(self.policy_combo)
        layout.addWidget(pol_sec)

        layout.addStretch()

        # ===== 全局选项 + 双按钮 =====
        self.report_cb = QCheckBox("完成时生成 HTML 格式的 DIT 报告")
        self.report_cb.setChecked(True)
        layout.addWidget(self.report_cb)

        btns = QHBoxLayout()
        self.start_btn = QPushButton("▶  开始拷贝")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #2c7be5; color: white; font-weight: 600;"
            "font-size: 14px; border-radius: 8px; border: none; }"
            "QPushButton:hover { background-color: #3d8cf5; }"
            "QPushButton:pressed { background-color: #1f5fbf; }"
            "QPushButton:disabled { background-color: #3a4455; color: #6b7280; }"
        )
        self.start_btn.clicked.connect(self.startRequested.emit)

        self.verify_btn = QPushButton("✔  仅校验")
        self.verify_btn.setMinimumHeight(40)
        self.verify_btn.setCursor(Qt.PointingHandCursor)
        self.verify_btn.setStyleSheet(
            "QPushButton { background-color: #4a5568; color: #e0e0e0; font-weight: 600;"
            "font-size: 14px; border-radius: 8px; border: 1px solid #5a6578; }"
            "QPushButton:hover { background-color: #5a6578; border-color: #7a8da0; }"
            "QPushButton:pressed { background-color: #374151; }"
            "QPushButton:disabled { background-color: #2d333b; color: #5f6a7a; border-color: #3a4550; }"
        )
        self.verify_btn.clicked.connect(self.verifyRequested.emit)

        btns.addWidget(self.start_btn, stretch=1)
        btns.addWidget(self.verify_btn, stretch=1)
        layout.addLayout(btns)

    # ---- 源管理 ----
    def _add_sources(self, paths: list[str]) -> None:
        existing = {self.src_list.item(i).data(Qt.UserRole) for i in range(self.src_list.count())}
        for p in paths:
            ap = os.path.abspath(p)
            if ap in existing:
                continue
            item = QListWidgetItem(self._describe(p))
            item.setData(Qt.UserRole, ap)
            item.setToolTip(ap)
            self.src_list.addItem(item)
        self._refresh_src_count()
        self.configChanged.emit()

    def _describe(self, path: str) -> str:
        return f"📁 {path}" if os.path.isdir(path) else f"📄 {path}"

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择源文件", "")
        if files:
            self._add_sources(files)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择源文件夹", "")
        if d:
            self._add_sources([d])

    def _remove_selected_sources(self) -> None:
        for item in self.src_list.selectedItems():
            self.src_list.takeItem(self.src_list.row(item))
        self._refresh_src_count()
        self.configChanged.emit()

    def _select_all_sources(self) -> None:
        """全选源列表中的所有项。"""
        self.src_list.selectAll()

    def _reveal_source(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.UserRole)
        if not path or not os.path.exists(path):
            return
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError:
            pass

    def _refresh_src_count(self) -> None:
        n = self.src_list.count()
        self.src_count_lbl.setText(f"源：{n} 项")

    # ---- 目标 ----
    def _set_dest(self, path: str) -> None:
        self.dst_edit.setText(os.path.abspath(path))

    def _pick_dest(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择目标文件夹", "")
        if d:
            self._set_dest(d)

    def _pick_sscl(self) -> None:
        """导入 _SSCL.xml 场记文件，并自动填入 Job 名称。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择场记文件 (_SSCL.xml)",
            "", "SSCL 文件 (*_SSCL.xml);;XML 文件 (*.xml);;所有文件 (*)",
        )
        if path:
            self.sscl_edit.setText(os.path.abspath(path))
            # 自动生成 Job 名称：SSCL文件名(去掉_SSCL.xml) + 当前日期
            base = os.path.basename(path)
            if base.endswith("_SSCL.xml"):
                base = base[:-len("_SSCL.xml")]
            elif base.endswith(".xml"):
                base = base[:-4]
            today = date.today().isoformat()
            self.job_name_edit.setText(f"{base}_{today}")

    # ---- 对外取值 ----
    def get_sources(self) -> list[str]:
        return [self.src_list.item(i).data(Qt.UserRole) for i in range(self.src_list.count())]

    def get_dest(self) -> str:
        return self.dst_edit.text().strip()

    def get_job_name(self) -> str:
        name = self.job_name_edit.text().strip()
        return name or "Untitled"

    def get_conflict_policy(self) -> str:
        return self.policy_combo.currentData()

    def is_report_enabled(self) -> bool:
        return self.report_cb.isChecked()

    def is_rename_enabled(self) -> bool:
        """是否启用了场记自动重命名。"""
        return self.rename_cb.isChecked() and bool(self.get_sscl_path())

    def get_sscl_path(self) -> str:
        """返回已导入的 SSCL 文件路径（空字符串表示未导入）。"""
        return self.sscl_edit.text().strip()

    def set_running(self, running: bool) -> None:
        """运行状态提示。

        1.0.4 起不再禁用输入控件：DIT 需要同时往多块目标盘拷贝做
        双重备份——作业运行期间允许继续配置并添加新任务（不同目标盘
        并发执行，同一目标盘自动排队）。任务参数在点击开始时即已
        快照，运行中修改输入不影响已提交的任务。
        """
        self.start_btn.setText(
            "▶  开始拷贝（后台执行中）" if running else "▶  开始拷贝")

    def add_source_path(self, path: str) -> None:
        if path:
            self._add_sources([path])
