"""阶段2 GUI 冒烟测试（无需显示器）。

使用 QT_QPA_PLATFORM=offscreen 在无头环境下实例化主窗口，
验证：
1. 所有 GUI 模块可正常 import
2. 主窗口、三栏 widget 能构造
3. 信号连接（drive→copy, start）无异常
4. 后端 worker 可构造（不真正启动线程，避免无头环境出问题）
5. 模拟一次完整的 worker 回调链，确认 UI 更新逻辑无报错

运行：
    python gui_smoke_test.py
"""
from __future__ import annotations

import os
import sys

# 无头渲染
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def banner(t: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {t}")
    print("=" * 60)


def main() -> int:
    banner("测试1: 导入所有 GUI 模块")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow
    from gui.widgets.copy_panel import CopyPanel
    from gui.widgets.drive_panel import DrivePanel
    from gui.widgets.queue_panel import JobItemWidget, QueuePanel
    from gui.workers import (
        CopyJobWorker,
        FileProgress,
        JobSummary,
        ScanResult,
        ScanWorker,
        WorkerThread,
    )
    print("   所有模块导入成功 ✓")

    banner("测试2: 实例化主窗口与三栏")
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    assert win.drive_panel is not None
    assert win.copy_panel is not None
    assert win.queue_panel is not None
    print("   主窗口 + 三栏构造成功 ✓")

    banner("测试3: 驱动器枚举")
    win.drive_panel.refresh_drives()
    print("   驱动器枚举调用无异常 ✓")

    banner("测试4: 源池增删 + 取值")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="dit_gui_")
    src_dir = os.path.join(tmp, "A001")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "clip.mxf"), "wb") as f:
        f.write(b"x" * 100)
    win.copy_panel.add_source_path(src_dir)
    win.copy_panel.add_source_path(src_dir)  # 重复应被忽略
    assert win.copy_panel.get_sources() == [src_dir], "源池应去重"
    print(f"   源池去重 OK：{win.copy_panel.get_sources()} ✓")
    # 设置目标
    dest = os.path.join(tmp, "dest")
    win.copy_panel.add_source_path  # noop
    win.copy_panel._set_dest(dest)  # type: ignore[attr-defined]
    assert win.copy_panel.get_dest() == os.path.abspath(dest)
    print(f"   目标设置 OK：{win.copy_panel.get_dest()} ✓")

    banner("测试5: Start 状态联动")
    win._refresh_start_state()
    assert win.copy_panel.start_btn.isEnabled(), "有源+目标，Start 应可用"
    print("   Start 按钮联动 OK ✓")

    banner("测试6: 队列条目 + 状态/进度/结果更新")
    item = win.queue_panel.add_job("JOB_GUI", dest)
    item.set_status("Copying")
    item.set_progress(50, 100)
    item.set_status("Verifying")
    item.set_result(
        verified=4, failed=0, skipped=1,
        bytes_total=1000, bytes_verified=900, elapsed=3.5, aborted=False,
    )
    assert item.status_lbl.text().startswith("✅")
    print("   队列条目状态机更新 OK ✓")

    banner("测试7: worker 构造（不启动线程）")
    worker = CopyJobWorker(
        job_name="JOB_GUI", sources=[src_dir], dest_root=dest,
        generate_report=False,
    )
    assert worker.job_name == "JOB_GUI"
    print("   CopyJobWorker 构造 OK ✓")

    banner("全部 GUI 冒烟测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
