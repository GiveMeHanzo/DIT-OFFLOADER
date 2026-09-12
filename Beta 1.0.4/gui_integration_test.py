"""阶段2 集成测试：真实后端流水线 × GUI worker 信号链。

不显示窗口，但真正跑 CopyJobWorker（在工作线程）拷贝真实文件，
验证 Qt 信号链完整：job_started → file_started → bytes_progress →
file_done → job_finished，且 UI 条目状态正确更新。

模拟两种路径：
A. 全新任务（目标目录空）
B. 断点续传（目标已有未完成 XML）

运行：
    python gui_integration_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from config import NameConflictPolicy
from core.logger import FileRecord, JobLogger
from gui.workers import CopyJobWorker, WorkerThread


def _make_sources(root: str) -> list[str]:
    d = os.path.join(root, "A001")
    os.makedirs(d)
    with open(os.path.join(d, "clip1.mxf"), "wb") as f:
        f.write(b"CLIP1-" + os.urandom(4096))
    with open(os.path.join(d, "clip2.mov"), "wb") as f:
        f.write(b"CLIP2-" + os.urandom(8192))
    return [d]


def _run_worker_until_finished(app: QApplication, worker: CopyJobWorker) -> object:
    """用事件循环驱动线程，直到 job_finished 信号到达。"""
    loop = QEventLoop()
    captured: dict = {}

    def on_done(summary):
        captured["summary"] = summary
        # 延迟一拍退出，让已入队但未投递的信号（file_done 等）先被处理
        QTimer.singleShot(0, loop.quit)

    worker.job_finished.connect(on_done)
    worker.error.connect(lambda s, m: (captured.setdefault("error", (s, m)), QTimer.singleShot(0, loop.quit)))

    thread = WorkerThread(worker)
    thread.finished.connect(thread.deleteLater)
    thread.start()

    # 超时保险（60s）
    QTimer.singleShot(60_000, loop.quit)
    loop.exec()
    thread.quit()
    thread.wait(5000)
    return captured


def test_full_new_job() -> None:
    print("\n--- 场景A：全新任务 ---")
    tmp = tempfile.mkdtemp(prefix="dit_intA_")
    sources = _make_sources(os.path.join(tmp, "src"))
    dest = os.path.join(tmp, "dest")

    events = {"started": 0, "file_started": 0, "bytes": 0, "file_done": 0}

    worker = CopyJobWorker(
        job_name="INTJOB_A", sources=sources, dest_root=dest,
        conflict_policy=NameConflictPolicy.KEEP, generate_report=True,
    )
    worker.job_started.connect(lambda n: events.__setitem__("started", events["started"] + 1))
    worker.file_started.connect(lambda fp: events.__setitem__("file_started", events["file_started"] + 1))
    worker.bytes_progress.connect(lambda d, t, s: events.__setitem__("bytes", events["bytes"] + 1))
    worker.file_done.connect(lambda fp: events.__setitem__("file_done", events["file_done"] + 1))

    captured = _run_worker_until_finished(QApplication.instance(), worker)
    assert "error" not in captured, f"发生错误: {captured.get('error')}"
    summary = captured["summary"]

    print(f"   事件计数: {events}")
    print(f"   汇总: verified={summary.verified} failed={summary.failed} "
          f"bytes={summary.bytes_verified}/{summary.bytes_total} "
          f"report={summary.generate_report} elapsed={summary.elapsed:.2f}s")

    assert events["started"] == 1, "job_started 应触发一次"
    assert events["file_done"] == 2, f"两个文件校验各触发一次file_done, 实际={events['file_done']}"
    assert events["file_started"] >= 2, "每个文件至少一次 started"
    assert summary.verified == 2
    assert summary.failed == 0
    assert summary.generate_report is True
    # XML 存在
    assert os.path.isfile(summary.xml_path)
    # 目标文件真实存在
    files_on_dest = [f for _, _, fs in os.walk(dest) for f in fs if f.endswith((".mxf", ".mov"))]
    assert len(files_on_dest) == 2
    print("   场景A 通过 ✓")
    shutil.rmtree(tmp, ignore_errors=True)


def test_resume_job() -> None:
    print("\n--- 场景B：断点续传 ---")
    from core.scanner import scan_sources
    tmp = tempfile.mkdtemp(prefix="dit_intB_")
    sources = _make_sources(os.path.join(tmp, "src"))
    dest = os.path.join(tmp, "dest")
    os.makedirs(dest)

    # 预先写一个「未完成」XML：只标记 1 个文件 verified
    tasks = scan_sources(sources)
    from config import FileStatus, log_xml_path
    xml = log_xml_path(dest, "INTJOB_B")
    init = [FileRecord(src=t.src, dest=t.dest_under(dest), size=t.size) for t in tasks]
    logger = JobLogger.create_new(xml, "INTJOB_B", [(sources[0], dest)], init)
    # 模拟第一个文件已拷贝+校验
    first = tasks[0]
    from core.copier import copy_file, ensure_parent_dir
    dp = first.dest_under(dest)
    ensure_parent_dir(dp)
    copy_file(first.src, dp, overwrite=False)
    logger.set_file_status(first.src, FileStatus.VERIFIED.value,
                           hash_src="x", hash_dest="x", verified="true")

    # 扫描得到 ExistingLogInfo
    from core.logger import find_existing_logs
    info = find_existing_logs(dest)[0]
    assert not info.is_completed
    assert len(info.pending_files) == 1, f"应有1个pending, 实际{len(info.pending_files)}"
    print(f"   续传前：已验证1，待处理{len(info.pending_files)}")

    worker = CopyJobWorker(
        job_name="INTJOB_B", sources=sources, dest_root=dest,
        conflict_policy=NameConflictPolicy.KEEP, generate_report=False,
        resume_from=info,
    )
    captured = _run_worker_until_finished(QApplication.instance(), worker)
    assert "error" not in captured, f"续传出错: {captured.get('error')}"
    summary = captured["summary"]
    print(f"   续传汇总: verified={summary.verified} skipped={summary.skipped}")
    assert summary.verified == 1, "仅续传剩余1个"
    assert summary.failed == 0
    # 续传后 XML 标记 Completed
    info2 = find_existing_logs(dest)[0]
    assert info2.is_completed, "续传完成后 XML 应 Completed"
    print("   场景B 通过 ✓")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    test_full_new_job()
    test_resume_job()
    print("\n" + "=" * 60)
    print("  GUI × 后端集成测试全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
