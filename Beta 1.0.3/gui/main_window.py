"""三栏主窗口（深色风格 + 仅校验支持）。"""
from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox,
    QSplitter, QStatusBar, QVBoxLayout, QWidget,
)

from config import NameConflictPolicy
from gui.widgets.copy_panel import CopyPanel
from gui.widgets.drive_panel import DrivePanel
from gui.widgets.queue_panel import JobItemWidget, QueuePanel, _human_size
from gui.workers import (
    CopyJobWorker, JobSummary, ScanResult, ScanWorker,
    VerifyOnlyWorker, WorkerThread,
)

DARK_QSS = """
QMainWindow, QDialog, QMessageBox { background-color: #1b1e23; color: #e0e0e0; }
QWidget { font-family: 'Segoe UI','PingFang SC','Helvetica Neue',sans-serif;
          font-size: 12px; color: #e0e0e0; }
QMenu { background-color: #2a2d33; border: 1px solid #3e4248; padding: 4px 0; color: #e0e0e0; }
QMenu::item { padding: 5px 24px 5px 12px; }
QMenu::item:selected { background-color: #3a4a6b; }
QComboBox { background-color: #2a2d33; border: 1px solid #4a4e55; border-radius: 5px;
            padding: 5px 8px; color: #e0e0e0; }
QComboBox:hover { border-color: #5f9efa; }
QComboBox QAbstractItemView { background-color: #2a2d33; selection-background-color: #3a4a6b;
                               color: #e0e0e0; border: 1px solid #3e4248; }
QLineEdit { background-color: #25282e; border: 1px solid #4a4e55; border-radius: 5px;
            padding: 5px 7px; color: #e0e0e0; selection-background-color: #3a4a6b; }
QLineEdit:focus { border-color: #5f9efa; }
QListWidget, QTreeView { background-color: #25282e; border: 1px solid #3e4248;
                          border-radius: 6px; color: #e0e0e0; }
QListWidget::item:selected, QTreeView::item:selected { background-color: #3a4a6b; color: #fff; }
QScrollBar:vertical { background: #1b1e23; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #4a4e55; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #5f6470; }
QProgressBar { background-color: #25282e; border: 1px solid #3e4248; border-radius: 5px;
               text-align: center; height: 16px; color: #e0e0e0; font-size: 10px; }
QProgressBar::chunk { background-color: #4d8eff; border-radius: 4px; }
QPushButton { background-color: #303540; border: 1px solid #4a4e55; border-radius: 5px;
              padding: 5px 14px; color: #e0e0e0; }
QPushButton:hover { background-color: #3a4250; border-color: #5f9efa; }
QPushButton:pressed { background-color: #252a35; }
QPushButton:disabled { background-color: #24282e; color: #5f6470; border-color: #353840; }
QCheckBox { color: #c0c4cc; spacing: 6px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a4e55;
                       border-radius: 3px; background: #25282e; }
QCheckBox::indicator:checked { background: #4d8eff; border-color: #4d8eff; }
QFrame#section { background-color: #23272d; border: 1px solid #353840; border-radius: 8px; }
QSplitter::handle { background-color: #2a2d33; }
QSplitter::handle:horizontal { width: 2px; }
QStatusBar { background-color: #1b1e23; color: #888; border-top: 1px solid #2a2d33; }
QToolTip { background-color: #2a2d33; border: 1px solid #3e4248; color: #e0e0e0; padding: 3px 6px; }
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DIT OFFLOADER")
        self.resize(1180, 760)
        self._worker_threads: list[WorkerThread] = []
        self._active_workers: dict[str, CopyJobWorker | VerifyOnlyWorker] = {}
        self._items: dict[str, JobItemWidget] = {}
        self._threads: dict[str, WorkerThread] = {}       # job_name → 作业线程引用
        self._job_by_worker: dict[int, str] = {}           # id(worker) → job_name
        self._job_is_copy: dict[int, bool] = {}            # id(worker) → is_copy
        self._last_renamed_map: dict[str, str] = {}        # 最近一次重命名映射
        self._report_workers: dict[str, object] = {}       # job_name → ReportWorker
        self._report_threads: dict[str, WorkerThread] = {} # job_name → 报告线程
        self._job_by_report_worker: dict[int, str] = {}    # id(ReportWorker) → job_name
        self._pending_finalization: dict[str, dict] = {}   # 待报告完成后回填的终态数据
        self._build_ui()
        QApplication.instance().setStyleSheet(DARK_QSS)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setFixedHeight(42)
        header.setStyleSheet("background: #14171c; border-bottom: 1px solid #2a2d33;")
        hl = QVBoxLayout(header); hl.setContentsMargins(12, 0, 12, 0)
        lbl = QLabel("  DIT OFFLOADER V1.0.3 — 拷卡 · 校验 · 文件名同步                                                       工部尚书府 GiveMeHanzo 开发     github.com/GiveMeHanzo")
        lbl.setStyleSheet("color: white; font-size: 14px; font-weight: 700;"
                          "border: none; background: transparent;")
        hl.addWidget(lbl); root.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        self.drive_panel = DrivePanel()
        self.copy_panel = CopyPanel()
        self.queue_panel = QueuePanel()
        splitter.addWidget(self.drive_panel)
        splitter.addWidget(self.copy_panel)
        splitter.addWidget(self.queue_panel)
        splitter.setStretchFactor(0, 2); splitter.setStretchFactor(1, 4); splitter.setStretchFactor(2, 3)
        splitter.setSizes([260, 480, 440])
        root.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.drive_panel.pathSelected.connect(self.copy_panel.add_source_path)
        self.copy_panel.startRequested.connect(self._on_start_copy)
        self.copy_panel.verifyRequested.connect(self._on_start_verify)
        self.copy_panel.configChanged.connect(self._refresh_start_state)
        self._refresh_start_state()

    def _refresh_start_state(self) -> None:
        has_src = bool(self.copy_panel.get_sources())
        has_dst = bool(self.copy_panel.get_dest())
        # 如果勾选了重命名但未导入 SSCL 文件，禁用开始按钮
        rename_blocked = (
            self.copy_panel.rename_cb.isChecked()
            and not bool(self.copy_panel.get_sscl_path())
        )
        enabled = has_src and has_dst and not rename_blocked
        self.copy_panel.start_btn.setEnabled(enabled)
        self.copy_panel.verify_btn.setEnabled(has_src and has_dst)

    # ── 拷贝 ──
    def _on_start_copy(self) -> None:
        sources = self.copy_panel.get_sources()
        dest_root = self.copy_panel.get_dest()
        job_name = self.copy_panel.get_job_name()
        if not self._validate(sources, dest_root): return
        if not self._check_sources_not_empty(sources): return
        os.makedirs(dest_root, exist_ok=True)
        self._scan_worker = ScanWorker(dest_root)
        self._scan_thread = WorkerThread(self._scan_worker)
        self._scan_worker.finished_ok.connect(self._on_scan_done_copy)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._pending_job = (job_name, sources, dest_root)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.start()

    def _on_scan_done_copy(self, result: ScanResult) -> None:
        job_name, sources, dest_root = self._pending_job
        resume_from = None
        if result.existing:
            completed = [e for e in result.existing if e.is_completed]
            unfinished = [e for e in result.existing if not e.is_completed]
            if unfinished:
                names = "\n".join(f"  • {os.path.basename(e.xml_path)} ({len(e.pending_files)} 个待续传)" for e in unfinished)
                msg = QMessageBox(self); msg.setWindowTitle("检测到中断的任务")
                msg.setText("检测到目标目录存在未完成的拷贝任务：\n" + names)
                msg.setInformativeText("是否继续未完成的拷贝，还是开启全新任务？")
                cont = msg.addButton("继续未完成任务", QMessageBox.AcceptRole)
                new_btn = msg.addButton("开启全新任务", QMessageBox.RejectRole)
                msg.addButton("取消", QMessageBox.RejectRole); msg.exec()
                if msg.clickedButton() is cont and unfinished: resume_from = unfinished[0]
                elif msg.clickedButton() is new_btn: resume_from = None
                else: return
            elif completed:
                names = "\n".join(f"  • {os.path.basename(e.xml_path)}" for e in completed)
                msg = QMessageBox(self); msg.setWindowTitle("目标已备份过")
                msg.setText("目标文件夹已进行过备份：\n" + names)
                msg.setInformativeText("是否要在同一目录再次追加拷贝？")
                yes = msg.addButton("追加备份", QMessageBox.AcceptRole)
                msg.addButton("取消", QMessageBox.RejectRole); msg.exec()
                if msg.clickedButton() is not yes: return
        self._launch_copy_job(job_name, sources, dest_root, resume_from)

    def _launch_copy_job(self, job_name: str, sources: list[str], dest_root: str, resume_from) -> None:
        policy = NameConflictPolicy.KEEP if self.copy_panel.get_conflict_policy() == "keep" else NameConflictPolicy.SKIP
        gen = self.copy_panel.is_report_enabled()
        worker = CopyJobWorker(job_name=job_name, sources=sources, dest_root=dest_root,
                                conflict_policy=policy, generate_report=gen, resume_from=resume_from)
        self._setup_common(worker, job_name, dest_root, is_copy=True)

    # ── 仅校验 ──
    def _on_start_verify(self) -> None:
        sources = self.copy_panel.get_sources()
        dest_root = self.copy_panel.get_dest()
        job_name = self.copy_panel.get_job_name() + "_verify"
        if not self._validate(sources, dest_root): return
        if not self._check_sources_not_empty(sources): return
        os.makedirs(dest_root, exist_ok=True)
        worker = VerifyOnlyWorker(job_name=job_name, sources=sources, dest_root=dest_root,
                                   generate_report=self.copy_panel.is_report_enabled())
        self._setup_common(worker, job_name, dest_root, is_copy=False)

    # ── 公共 ──
    def _validate(self, sources, dest_root) -> bool:
        if not sources: QMessageBox.warning(self, "缺少源", "请先添加至少一个源文件或文件夹。"); return False
        if not dest_root: QMessageBox.warning(self, "缺少目标", "请选择目标文件夹。"); return False
        for s in sources:
            if not os.path.exists(s): QMessageBox.warning(self, "源不存在", f"源路径不存在：\n{s}"); return False
        return True

    def _check_sources_not_empty(self, sources: list[str]) -> bool:
        """快速预扫描：检测源路径中是否包含任何可拷贝文件。"""
        from core.scanner import scan_sources
        tasks = scan_sources(sources)
        if not tasks:
            QMessageBox.warning(self, "源为空", "所选的源路径中未找到任何可拷贝文件，请检查。")
            return False
        return True

    def _setup_common(self, worker, job_name: str, dest_root: str, *, is_copy: bool) -> None:
        thread = WorkerThread(worker)
        item = self.queue_panel.add_job(job_name, dest_root)
        self._items[job_name] = item
        item.cancelRequested.connect(lambda name=job_name: self._on_cancel(name))
        # 存储 id→job_name 映射，供 @Slot 方法通过 sender() 查找
        self._job_by_worker[id(worker)] = job_name
        self._job_is_copy[id(worker)] = is_copy
        # 直接连接到 MainWindow 的 @Slot 方法（MainWindow 是 QObject，在主线程）
        # Qt 自动检测线程差异并使用 QueuedConnection，确保在主线程执行
        worker.job_started.connect(self._on_worker_job_started)
        worker.file_started.connect(self._on_worker_file_started)
        worker.file_done.connect(self._on_worker_file_done)
        worker.bytes_progress.connect(self._on_worker_bytes_progress)
        worker.job_finished.connect(self._on_worker_job_finished)
        worker.error.connect(self._on_worker_error)
        self._active_workers[job_name] = worker
        self._worker_threads.append(thread)
        self._threads[job_name] = thread
        thread.finished.connect(thread.deleteLater)
        self.copy_panel.set_running(True)
        self.statusBar().showMessage(f"正在执行：{job_name}")
        thread.start()

    # ── Worker 信号槽（@Slot 装饰确保主线程执行）──

    @Slot(str)
    def _on_worker_job_started(self, _name_from_signal: str) -> None:
        name = self._job_by_worker.get(id(self.sender()), "")
        item = self._items.get(name)
        if item: item.set_status("Copying")

    @Slot(object)
    def _on_worker_file_started(self, fp: object) -> None:
        name = self._job_by_worker.get(id(self.sender()), "")
        item = self._items.get(name)
        if item:
            st = getattr(fp, "status", "")
            if st in ("copying", "Copying"): item.set_status("Copying")
            elif st in ("verifying", "Verifying"): item.set_status("Verifying")

    @Slot(object)
    def _on_worker_file_done(self, fp: object) -> None:
        pass  # 当前无需处理

    @Slot(object, object, str)
    def _on_worker_bytes_progress(self, done: object, total: object, _src: str) -> None:
        name = self._job_by_worker.get(id(self.sender()), "")
        item = self._items.get(name)
        if item: item.set_progress(int(done), int(total))

    @Slot(object)
    def _on_worker_job_finished(self, summary: object) -> None:
        worker = self.sender()
        name = self._job_by_worker.get(id(worker), "")
        is_copy = self._job_is_copy.get(id(worker), True)
        self._on_job_finished(name, summary, is_copy)

    @Slot(str, str)
    def _on_worker_error(self, src: str, msg: str) -> None:
        name = self._job_by_worker.get(id(self.sender()), "")
        self._on_error(name, src, msg)

    @Slot(str)
    def _on_scan_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "扫描失败", msg)

    def _on_job_finished(self, name: str, summary, is_copy: bool) -> None:
        try:
            item = self._items.get(name)
            # 先获取 failed 值（后续报告生成时需用）
            if is_copy:
                failed_val = summary.failed
                verified_val = summary.verified
                skipped_val = summary.skipped
                bytes_total = summary.bytes_total
                bytes_verified = summary.bytes_verified
                elapsed = summary.elapsed
                aborted = summary.aborted
            else:
                failed_val = summary.mismatched + summary.missing
                verified_val = summary.verified
                skipped_val = 0
                bytes_total = summary.bytes_total
                bytes_verified = summary.bytes_done
                elapsed = summary.elapsed
                aborted = summary.aborted

            # 先同步进度条到 100%，但不设 Done 状态（报告未生成完）
            if item:
                item.bar.setValue(100 if failed_val == 0 and not aborted else item.bar.value())
                item.bar.setFormat("100%")
                item.cancel_btn.setVisible(False)

            self._active_workers.pop(name, None)
            if not self._active_workers: self.copy_panel.set_running(False)
            if is_copy:
                s = summary; msg = f"任务完成：{name}  ✓{s.verified} ✗{s.failed} ⏭{s.skipped}  耗时 {s.elapsed:.1f}s"
            else:
                s = summary; msg = f"校验完成：{name}  源 {s.total_in_source} 个 ✓{s.verified} 缺失 {s.missing} 不一致 {s.mismatched}  耗时 {s.elapsed:.1f}s"
            self.statusBar().showMessage(msg, 12000)
            if not is_copy and getattr(summary, 'mismatched_files', None):
                limit = 10; show = summary.mismatched_files[:limit]
                more = f"\n… 共 {len(summary.mismatched_files)} 个" if len(summary.mismatched_files) > limit else ""
                QMessageBox.warning(self, "校验不完整", f"以下文件校验失败：\n" + "\n".join(show) + more)
            # 场记自动重命名（在报告生成之前执行；主线程，os.rename + XML 回写，快速）
            if is_copy and not aborted:
                self._maybe_rename_assets(summary)

            # ── 报告生成 ──
            # 移到后台线程，避免 probe/抽帧/HTML 构建卡住 UI。
            if summary.generate_report and not aborted:
                if item:
                    item.set_status("Generating Report")
                # 缓存本作业的最终状态数据，供报告完成槽回填
                self._pending_finalization[name] = {
                    "verified": verified_val, "failed": failed_val,
                    "skipped": skipped_val, "bytes_total": bytes_total,
                    "bytes_verified": bytes_verified, "elapsed": elapsed,
                    "aborted": aborted,
                }
                self._launch_report(summary)
            else:
                # 无需生成报告：直接收尾
                self._finalize_job(name, verified_val, failed_val, skipped_val,
                                   bytes_total, bytes_verified, elapsed, aborted)
        except Exception as e:
            self.statusBar().showMessage(f"任务收尾异常: {e}", 10000)
            # 异常路径也要释放作业线程
            self._finalize_job(name, 0, 1, 0, 0, 0, 0.0, True)

    def _launch_report(self, summary) -> None:
        """在后台线程启动 ReportWorker 生成报告。"""
        from gui.workers import ReportWorker
        sources = self.copy_panel.get_sources()
        renamed = self._last_renamed_map or None
        name = summary.job_name

        report_worker = ReportWorker(
            summary=summary, sources=sources, renamed_map=renamed,
        )
        report_thread = WorkerThread(report_worker)
        # 用 id(worker) → job_name 映射，槽里通过 sender() 取回 job 名（与
        # _on_worker_job_finished 同模式）。连接到 MainWindow 的 bound method，
        # Qt 据此判定 receiver 在主线程，自动用 QueuedConnection 投递——
        # 不能用 lambda（会被当 DirectConnection，槽跑在报告线程里导致
        # "Timers cannot be stopped from another thread" 等崩溃）。
        self._job_by_report_worker[id(report_worker)] = name
        self._report_workers[name] = report_worker
        self._report_threads[name] = report_thread
        report_worker.finished.connect(self._on_report_finished)
        report_worker.failed.connect(self._on_report_failed)
        report_thread.finished.connect(report_thread.deleteLater)
        report_thread.start()

    @Slot(str)
    def _on_report_finished(self, html_path: str) -> None:
        """后台报告生成完成：回填最终状态、退出作业线程（主线程执行）。"""
        worker = self.sender()
        name = self._job_by_report_worker.pop(id(worker), "") if worker is not None else ""
        fin = self._pending_finalization.pop(name, None)
        if fin:
            self._finalize_job(name, **fin)
        if name:
            self.statusBar().showMessage(
                f"报告已生成: {os.path.basename(html_path)}", 8000)
            self._cleanup_report_thread(name)

    @Slot(str)
    def _on_report_failed(self, msg: str) -> None:
        """后台报告生成失败：仍需回填终态并退出作业线程（主线程执行）。"""
        worker = self.sender()
        name = self._job_by_report_worker.pop(id(worker), "") if worker is not None else ""
        fin = self._pending_finalization.pop(name, None)
        if fin:
            # 报告失败不影响拷贝/校验本身的成败判定，按原结果回填
            self._finalize_job(name, **fin)
        if name:
            self.statusBar().showMessage(f"报告生成失败: {msg}", 8000)
            self._cleanup_report_thread(name)

    def _cleanup_report_thread(self, name: str) -> None:
        # 注意：WorkerThread 里已把 report_worker.finished/failed 连到了 thread.quit，
        # 因此 report 线程此时已（或正在）自行退出；这里只做 wait 收尾，
        # 不再调 quit()，避免对已经退出的线程重复操作。
        self._report_workers.pop(name, None)
        t = self._report_threads.pop(name, None)
        if t is not None and t.isRunning():
            t.wait(2000)

    def _finalize_job(
        self, name: str, verified: int, failed: int, skipped: int,
        bytes_total: int, bytes_verified: int, elapsed: float,
        aborted: bool,
    ) -> None:
        """回填作业项的最终状态、详情文字、提示音，并退出作业线程。"""
        item = self._items.get(name)
        if item:
            if aborted:
                item.set_status("Aborted")
            elif failed > 0:
                item.set_status("Error")
            else:
                item.set_status("Done")
            item.detail_lbl.setText(
                f"✓ {verified} 验证 · ✗ {failed} 失败 · ⏭ {skipped} 跳过 · "
                f"{_human_size(bytes_verified)} / {_human_size(bytes_total)} · "
                f"耗时 {elapsed:.1f}s")
        self._play_completion_sound()
        # 安全退出作业线程（报告生成在独立线程，不阻塞此处）
        t = self._threads.pop(name, None)
        if t is not None and t.isRunning():
            t.quit()
            t.wait(3000)

    def _on_error(self, name: str, src: str, msg: str) -> None:
        self.statusBar().showMessage(f"错误 [{name}]: {msg}", 8000)
        if src: QMessageBox.warning(self, "文件错误", f"{os.path.basename(src)}\n\n{msg}")
        else: QMessageBox.warning(self, "任务错误", msg)

    def _on_cancel(self, name: str) -> None:
        worker = self._active_workers.get(name)
        if worker: worker.request_cancel()
        self.statusBar().showMessage(f"已请求取消：{name}（等待当前文件完成）", 5000)

    def _maybe_rename_assets(self, summary) -> None:
        """如果启用了场记重命名，在拷贝完成后执行自动重命名。"""
        self._last_renamed_map = {}
        if not self.copy_panel.is_rename_enabled():
            return
        sscl_path = self.copy_panel.get_sscl_path()
        log_path = summary.xml_path
        dest_root = summary.dest_root

        if not os.path.isfile(sscl_path):
            self.statusBar().showMessage(f"场记文件不存在，跳过重命名: {sscl_path}", 8000)
            return
        if not os.path.isfile(log_path):
            self.statusBar().showMessage(f"日志文件不存在，跳过重命名: {log_path}", 8000)
            return

        try:
            from core.renamer import rename_media_assets
            result = rename_media_assets(sscl_path, log_path, dest_root)
            # 构建 renamed_map 供报告使用：{normcase(旧路径): 新路径}
            for entry in result.renamed:
                self._last_renamed_map[os.path.normcase(entry["old_path"])] = entry["new_path"]
            if result.status == "error":
                self.statusBar().showMessage(
                    f"场记重命名失败: {'; '.join(result.errors[:2])}", 10000
                )
                QMessageBox.warning(self, "重命名失败",
                    f"自动重命名出错：\n" + "\n".join(result.errors[:5]))
            else:
                self.statusBar().showMessage(
                    f"场记重命名完成: {result.renamed_count} 个文件已改名"
                    + (f", {result.not_found_count} 个未匹配" if result.not_found_count else "")
                    + (f", {result.error_count} 个失败" if result.error_count else ""),
                    12000,
                )
        except Exception as e:
            self.statusBar().showMessage(f"场记重命名异常: {e}", 8000)

    def _play_completion_sound(self) -> None:
        """任务完成提示音（跨平台）。"""
        try:
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            else:
                QApplication.beep()
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        running = [w for w in self._active_workers.values()]
        if running:
            ret = QMessageBox.question(self, "确认退出",
                f"还有 {len(running)} 个任务正在运行，确定退出吗？\n（运行中的文件会写为中断状态）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes: event.ignore(); return
            for w in running: w.request_cancel()
        for t in self._worker_threads:
            if t.isRunning(): t.quit(); t.wait(2000)
        # 等待后台报告线程退出
        for t in self._report_threads.values():
            if t.isRunning(): t.quit(); t.wait(2000)
        event.accept()


__all__ = ["MainWindow"]
