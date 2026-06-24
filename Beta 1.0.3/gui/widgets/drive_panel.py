"""左侧栏：驱动器 / 文件浏览器（深色风格）。

显示本地磁盘、外接阵列、读卡器（通过 psutil 跨平台枚举）。
点击卷/文件夹时发出 pathSelected，供中间栏使用。
刷新按钮可重新枚举（热插拔读卡器后）。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import psutil
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from gui.widgets.copy_panel import _AnimButton as AnimButton

from config import EXCLUDED_FS_TYPES, EXCLUDED_MOUNTPOINTS, _EXCLUDED_MOUNTPOINT_PREFIXES


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def _human_size2(used: float, total: float) -> str:
    return f"{_human_size(used)} / {_human_size(total)}"


class DrivePanel(QWidget):
    """左侧驱动器/文件浏览器面板。"""

    pathSelected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("Drives")
        title.setStyleSheet("font-weight: 600; font-size: 13px; color: #c0c4d0;")
        layout.addWidget(title)

        self.refresh_btn = AnimButton("⟳  刷新")
        self.refresh_btn.clicked.connect(self.refresh_drives)
        layout.addWidget(self.refresh_btn)

        self.drives_label = QLabel("已挂载卷：")
        self.drives_label.setStyleSheet("color: #707580; font-size: 11px;")
        layout.addWidget(self.drives_label)

        self._drives_host = QWidget()
        self._drives_layout = QVBoxLayout(self._drives_host)
        self._drives_layout.setContentsMargins(0, 0, 0, 0)
        self._drives_layout.setSpacing(4)
        layout.addWidget(self._drives_host)

        layout.addSpacing(8)

        tree_label = QLabel("文件浏览器：")
        tree_label.setStyleSheet("color: #707580; font-size: 11px;")
        layout.addWidget(tree_label)

        # 返回上级目录按钮
        nav_row = QHBoxLayout()
        self.up_btn = QPushButton("⬆  上级目录")
        self.up_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 3px 8px; "
            "background-color: #2a2d33; border: 1px solid #3e4248; "
            "border-radius: 4px; color: #c0c4cc; font-size: 11px; }"
            "QPushButton:hover { background-color: #353a44; border-color: #5f9efa; }"
        )
        self.up_btn.setCursor(Qt.PointingHandCursor)
        self.up_btn.clicked.connect(self._go_up)
        nav_row.addWidget(self.up_btn)
        nav_row.addStretch()
        layout.addLayout(nav_row)

        self.tree = QTreeView()
        self.model = QFileSystemModel()
        self.model.setRootPath("")
        self.tree.setModel(self.model)
        self.tree.setColumnWidth(0, 220)
        for col in range(1, 4):
            self.tree.setColumnHidden(col, True)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._on_tree_clicked)
        # 启用拖拽：从左侧树拖到中间源列表
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragOnly)
        layout.addWidget(self.tree, stretch=1)

        self.refresh_drives()

    def refresh_drives(self) -> None:
        while self._drives_layout.count():
            item = self._drives_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        mounts = self._enumerate_volumes()
        if not mounts:
            note = QLabel("（未检测到可写卷）")
            note.setStyleSheet("color: #606570; font-size: 11px;")
            self._drives_layout.addWidget(note)
            return

        for label, path, used, total in mounts:
            btn = QPushButton(f"💾  {label}\n     {_human_size2(used, total)}")
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 6px 8px; "
                "background-color: #2a2d33; border: 1px solid #3e4248; "
                "border-radius: 6px; color: #e0e0e0; }"
                "QPushButton:hover { background-color: #353a44; border-color: #5f9efa; }"
            )
            btn.setFixedHeight(46)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, p=path: self._select_path(p))
            self._drives_layout.addWidget(btn)

        self._drives_layout.addStretch()

    def _enumerate_volumes(self) -> list[tuple[str, str, float, float]]:
        result: list[tuple[str, str, float, float]] = []
        for part in psutil.disk_partitions(all=False):
            # 过滤系统内部卷：精确命中 → 跳过
            if part.mountpoint in EXCLUDED_MOUNTPOINTS:
                continue
            # 过滤系统内部卷：路径前缀命中（macOS /System/Volumes/* 等）
            if part.mountpoint.startswith(_EXCLUDED_MOUNTPOINT_PREFIXES):
                continue
            # 过滤系统文件系统类型（devfs, proc, tmpfs 等）
            if (part.fstype or "").lower() in EXCLUDED_FS_TYPES:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            label = self._volume_label(part)
            result.append((label, part.mountpoint, usage.used, usage.total))
        return result

    def _volume_label(self, part) -> str:
        if sys.platform == "win32":
            name = part.device
            opt = part.opts or ""
            tag = " (可移动)" if "removable" in opt.lower() else ""
            return f"{name}{tag}"
        else:
            name = os.path.basename(part.device) or part.mountpoint
            return f"/{name}"

    def _select_path(self, path: str) -> None:
        self.tree.setRootIndex(self.model.index(path))
        # 不再发射 pathSelected——单击驱动器仅导航，不添加到拷贝列表

    def _on_tree_clicked(self, index) -> None:
        path = self.model.filePath(index)
        if path and os.path.isdir(path):
            self.tree.setRootIndex(index)
            # 不发射 pathSelected——单击仅导航

    def _go_up(self) -> None:
        """导航到当前根目录的上级目录。"""
        current_root = self.model.filePath(self.tree.rootIndex())
        parent_dir = os.path.dirname(current_root)
        if parent_dir and parent_dir != current_root:
            self.tree.setRootIndex(self.model.index(parent_dir))


__all__ = ["DrivePanel"]
