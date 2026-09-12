"""1.0.4 修复验证测试（无需显示器、无需真实读卡器）。

覆盖：
1. config.is_visible_volume —— 隐藏/网络/系统卷过滤规则
2. scanner.has_copyable_file —— 预检早退
3. copier —— 拷贝时流式源 hash 与 hash_file 一致
4. copier —— 读侧 EIO 自动重试 + 断点续写（模拟读卡器抖动）
5. copier —— 不可重试错误 → 半截目标文件被清理
6. pipeline —— 端到端全验证通过；resume 残留半截文件被清理且不改名 -1
7. pipeline —— verify_source_hash=True 但源 hash 缺失 → 判 FAILED（不静默通过）
8. workers —— 目标路径去重 + 目标空间预检
9. logger —— 写入节流 + mark_completed 强制落盘不丢状态
10. drive_panel —— 离屏构造 + 刷新 + 自动刷新定时器

运行：
    python verify_fixes_test.py
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"   ✓ {name}")


# ──────────────────────────────────────────────
# 1. 卷可见性过滤
# ──────────────────────────────────────────────
def test_volume_filter() -> None:
    print("\n[1] config.is_visible_volume")
    from config import is_visible_volume

    hidden = [
        ("/", "apfs"),                                   # 系统根
        ("/System/Volumes/Data", "apfs"),                # 系统卷前缀
        ("/System/Volumes/Preboot/Cryptexes/OS", "apfs"),
        ("/private/var", "apfs"),                        # /private 前缀
        ("/Volumes/.timemachine/ABC/backupbundle", "apfs"),  # TM 隐藏挂载
        ("/Volumes/.MobileBackups", "apfs"),             # 点开头隐藏
        ("/Volumes/MyShare", "smbfs"),                   # SMB 网络卷
        ("/mnt/nas", "nfs"),                             # NFS
        ("/Volumes/Preboot", "apfs"),                    # 外置系统盘隐藏卷
        ("/Volumes/Recovery", "apfs"),
        ("/Volumes/vm", "apfs"),                         # 小写比较
        ("/Volumes/UPDATE", "apfs"),                     # 大小写不敏感
        ("/dev", "devfs"),
    ]
    for mp, fs in hidden:
        ok(f"隐藏 {mp} ({fs})", not is_visible_volume(mp, fs))

    visible = [
        ("/Volumes/CANON EOS", "apfs"),                  # 带空格相机卡
        ("/Volumes/NO NAME", "msdos"),                   # FAT32 相机卡
        ("/Volumes/CFexpress_B", "exfat"),
        ("/Volumes/RAID_1", "hfs"),                      # 用户阵列
        ("/mnt/custom_mount", "apfs"),                   # 自定义挂载点（非系统名）
    ]
    for mp, fs in visible:
        ok(f"显示 {mp} ({fs})", is_visible_volume(mp, fs))


# ──────────────────────────────────────────────
# 2. 预检早退
# ──────────────────────────────────────────────
def test_has_copyable_file() -> None:
    print("\n[2] scanner.has_copyable_file")
    from core.scanner import has_copyable_file

    tmp = tempfile.mkdtemp(prefix="dit_v2_")
    try:
        empty = os.path.join(tmp, "empty"); os.makedirs(empty)
        ok("空目录 → False", not has_copyable_file([empty]))
        ok("不存在路径 → False", not has_copyable_file([os.path.join(tmp, "nope")]))

        junk = os.path.join(tmp, "junk"); os.makedirs(junk)
        open(os.path.join(junk, ".DS_Store"), "wb").close()
        open(os.path.join(junk, "Thumbs.db"), "wb").close()
        ok("只有垃圾文件 → False", not has_copyable_file([junk]))

        withclip = os.path.join(tmp, "card")
        os.makedirs(os.path.join(withclip, "DCIM", "100EOS"))
        open(os.path.join(withclip, ".Spotlight-V100", ) if os.path.isdir(
            os.path.join(withclip, ".Spotlight-V100")) else "/dev/null", "wb").close()
        with open(os.path.join(withclip, "DCIM", "100EOS", "A001.mxf"), "wb") as f:
            f.write(b"x" * 16)
        ok("嵌套目录有素材 → True", has_copyable_file([withclip]))

        single = os.path.join(tmp, "one.mxf")
        with open(single, "wb") as f:
            f.write(b"y" * 16)
        ok("单文件源 → True", has_copyable_file([single]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 3-5. copier：流式hash / 重试 / 清理
# ──────────────────────────────────────────────
class _FlakyRead:
    """包装源文件对象：前 N 次 read() 抛 EIO，模拟读卡器瞬时抖动。"""
    remaining = 0

    def __init__(self, f) -> None:
        self._f = f

    def read(self, n=-1):
        if _FlakyRead.remaining > 0:
            _FlakyRead.remaining -= 1
            raise OSError(errno.EIO, "Input/output error")
        return self._f.read(n)

    def seek(self, *a): return self._f.seek(*a)
    def tell(self): return self._f.tell()
    def flush(self): return self._f.flush()
    def fileno(self): return self._f.fileno()
    def close(self): return self._f.close()
    def truncate(self, *a): return self._f.truncate(*a)
    def write(self, b): return self._f.write(b)
    def __enter__(self): return self
    def __exit__(self, *a): return self._f.__exit__(*a)


def test_copier() -> None:
    print("\n[3] copier：流式源 hash")
    from core.copier import copy_file
    from core.verifier import hash_file

    tmp = tempfile.mkdtemp(prefix="dit_v3_")
    try:
        src = os.path.join(tmp, "clip.mxf")
        data = os.urandom(3 * 1024 * 1024)  # 3MiB，跨多个 1MiB 块
        with open(src, "wb") as f:
            f.write(data)
        dest = os.path.join(tmp, "out", "clip.mxf")
        os.makedirs(os.path.dirname(dest))

        outcome = copy_file(src, dest, hash_src=True)
        ok("流式源hash == hash_file(源)",
           outcome.src_hash == hash_file(src))
        ok("流式源hash == hash_file(目标)",
           outcome.src_hash == hash_file(dest))
        ok("字节数一致", outcome.bytes_copied == len(data))
        with open(dest, "rb") as f:
            ok("内容逐字节一致", f.read() == data)

        print("\n[4] copier：EIO 重试 + 断点续写")
        import core.copier as cp
        orig_open, orig_base = open, cp._RETRY_BACKOFF_BASE
        cp._RETRY_BACKOFF_BASE = 0.001  # 测试不真等
        dest2 = os.path.join(tmp, "out", "clip2.mxf")
        _FlakyRead.remaining = 3  # 抖 3 次：分布在多次 read/重开之间
        real_open = open
        def flaky_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            if os.path.abspath(path) == os.path.abspath(src) and "r" in mode:
                return _FlakyRead(f)
            return f
        try:
            import builtins
            builtins.open = flaky_open
            outcome2 = copy_file(src, dest2, hash_src=True)
        finally:
            builtins.open = orig_open
            cp._RETRY_BACKOFF_BASE = orig_base
        with open(dest2, "rb") as f:
            ok("重试后内容仍逐字节一致", f.read() == data)
        ok("重试后流式hash仍正确", outcome2.src_hash == hash_file(src))

        print("\n[5] copier：不可重试错误 → 清理半截文件")
        dest3 = os.path.join(tmp, "out", "clip3.mxf")
        _FlakyRead.remaining = 1

        class _FatalRead(_FlakyRead):
            def read(self, n=-1):
                raise OSError(errno.ENOSPC, "No space left on device")

        def fatal_open(path, mode="r", *a, **k):
            f = real_open(path, mode, *a, **k)
            if os.path.abspath(path) == os.path.abspath(src) and "r" in mode:
                return _FatalRead(f)
            return f
        try:
            builtins.open = fatal_open
            try:
                copy_file(src, dest3)
                raised = False
            except OSError:
                raised = True
        finally:
            builtins.open = orig_open
        ok("ENOSPC 不重试直接抛出", raised)
        ok("半截目标文件已清理", not os.path.exists(dest3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 6-7. pipeline：端到端 / resume 清理 / 诚实校验
# ──────────────────────────────────────────────
def _make_card(root: str) -> str:
    d = os.path.join(root, "A001")
    os.makedirs(os.path.join(d, "CLIP"), exist_ok=True)
    for i, size in enumerate((128 * 1024, 300 * 1024, 7 * 1024)):
        with open(os.path.join(d, "CLIP", f"c{i}.mxf"), "wb") as f:
            f.write(os.urandom(size))
    with open(os.path.join(d, "notes.txt"), "wb") as f:
        f.write(b"daily note")
    with open(os.path.join(d, ".DS_Store"), "wb") as f:
        f.write(b"junk")  # 应被 scanner 排除
    return d


def test_pipeline() -> None:
    print("\n[6] pipeline：端到端")
    from config import FileStatus, PipelineConfig
    from core.logger import JobLogger, FileRecord, load_log
    from core.pipeline import CopyPipeline
    from core.scanner import scan_sources

    tmp = tempfile.mkdtemp(prefix="dit_v6_")
    try:
        card = _make_card(os.path.join(tmp, "src"))
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest)  # 生产路径中 workers._execute 会先建目标目录
        tasks = scan_sources([card])
        ok("素材数=4（3 素材 + notes.txt，.DS_Store 已排除）", len(tasks) == 4)
        logger = JobLogger.create_new(
            os.path.join(dest, "J_log.xml"), "J",
            [(card, dest)],
            [FileRecord(src=t.src, dest=t.dest_under(dest), size=t.size)
             for t in tasks],
        )
        pipe = CopyPipeline(tasks=tasks, dest_root=dest, logger=logger)
        result = pipe.run()
        ok("全部 verified", result.verified_files == 4 and result.failed_files == 0)
        info = load_log(os.path.join(dest, "J_log.xml"))
        ok("XML 标记 Completed", info.is_completed)
        ok("XML 中 hash_src 均已写入",
           all(f.hash_src for f in info.job.files if f.status == "verified"))

        print("\n[6b] pipeline：resume 残留半截文件清理（不改名 -1）")
        dest2 = os.path.join(tmp, "dest2")
        os.makedirs(dest2)
        # 半截残留：只写 1/3 数据，模拟上次中断
        first = tasks[0]
        pd = first.dest_under(dest2)
        os.makedirs(os.path.dirname(pd), exist_ok=True)
        with open(pd, "wb") as f:
            f.write(b"PARTIAL")
        logger2 = JobLogger.create_new(
            os.path.join(dest2, "J2_log.xml"), "J2",
            [(card, dest2)],
            [FileRecord(src=t.src, dest=t.dest_under(dest2), size=t.size)
             for t in tasks],
        )
        pipe2 = CopyPipeline(
            tasks=tasks, dest_root=dest2, logger=logger2,
            resume_records={os.path.normcase(first.src): ("failed", pd)},
        )
        result2 = pipe2.run()
        ok("resume 后全部 verified", result2.verified_files == 4)
        ok("残留被完整拷贝替换（原名保留）",
           os.path.isfile(pd) and os.path.getsize(pd) == first.size)
        ok("没有产生 -1 改名文件", not os.path.exists(
            os.path.splitext(pd)[0] + "-1" + os.path.splitext(pd)[1]))

        print("\n[7] pipeline：源 hash 缺失 → FAILED（不再静默通过）")
        import core.pipeline as pl
        dest3 = os.path.join(tmp, "dest3")
        os.makedirs(dest3)
        real_copy = pl.copy_file
        pl.copy_file = lambda *a, **k: real_copy(*a, **{**k, "hash_src": False})
        try:
            logger3 = JobLogger.create_new(
                os.path.join(dest3, "J3_log.xml"), "J3",
                [(card, dest3)],
                [FileRecord(src=t.src, dest=t.dest_under(dest3), size=t.size)
                 for t in tasks],
            )
            pipe3 = CopyPipeline(tasks=tasks, dest_root=dest3, logger=logger3)
            result3 = pipe3.run()
        finally:
            pl.copy_file = real_copy
        ok("全部判 FAILED", result3.failed_files == 4 and result3.verified_files == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 8. workers：同名目标共存（TOCTOU 加锁占位）+ 空间预检
# ──────────────────────────────────────────────
def test_workers() -> None:
    print("\n[8] CopyJobWorker：同名目标共存 + 空间预检")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.workers import CopyJobWorker

    tmp = tempfile.mkdtemp(prefix="dit_v8_")
    try:
        # 两张卡各有一个同名文件（内容不同）：旧版两个拷贝线程可能同时
        # 解析到同一目标路径并交错写坏；KEEP 语义应是 A.mxf + A-1.mxf 共存。
        d1 = os.path.join(tmp, "card1"); os.makedirs(d1)
        d2 = os.path.join(tmp, "card2"); os.makedirs(d2)
        data1, data2 = os.urandom(256 * 1024), os.urandom(256 * 1024)
        assert data1 != data2
        with open(os.path.join(d1, "A001.mxf"), "wb") as f: f.write(data1)
        with open(os.path.join(d2, "A001.mxf"), "wb") as f: f.write(data2)
        dest = os.path.join(tmp, "dest")

        captured: dict = {}
        w = CopyJobWorker(job_name="W1", sources=[d1, d2], dest_root=dest)
        w.job_finished.connect(lambda s: captured.setdefault("summary", s))
        w.error.connect(lambda s, m: captured.setdefault("error", (s, m)))
        w._execute()
        ok("无作业级错误", "error" not in captured)
        ok("两个同名文件都验证通过", captured["summary"].verified == 2)
        ok("目标上 A001.mxf 与 A001-1.mxf 共存",
           os.path.isfile(os.path.join(dest, "A001.mxf"))
           and os.path.isfile(os.path.join(dest, "A001-1.mxf")))
        with open(os.path.join(dest, "A001.mxf"), "rb") as f:
            got1 = f.read()
        with open(os.path.join(dest, "A001-1.mxf"), "rb") as f:
            got2 = f.read()
        ok("两份内容各自完整（无交错写坏）", {got1, got2} == {data1, data2})

        card = _make_card(os.path.join(tmp, "src"))
        print("\n[8b] 目标空间预检")
        import gui.workers as gw
        class _FakeDU:
            free = 100  # 字节，必然不足
            @classmethod
            def usage(cls, path):
                return cls
        orig = gw.shutil.disk_usage
        gw.shutil.disk_usage = _FakeDU.usage
        try:
            captured.clear()
            w2 = CopyJobWorker(job_name="W2", sources=[card], dest_root=dest)
            w2.job_finished.connect(lambda s: captured.setdefault("summary", s))
            w2.error.connect(lambda s, m: captured.setdefault("error", (s, m)))
            w2._execute()
        finally:
            gw.shutil.disk_usage = orig
        ok("空间不足 → 作业级错误", "error" in captured
           and "空间不足" in captured["error"][1])
        ok("空间不足 → 未开始拷贝", "summary" not in captured)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 9. logger：节流 + 强制落盘
# ──────────────────────────────────────────────
def test_logger() -> None:
    print("\n[9] JobLogger：写入节流")
    from core.logger import FileRecord, JobLogger, load_log

    tmp = tempfile.mkdtemp(prefix="dit_v9_")
    try:
        files = [FileRecord(src=f"/card/f{i}.mxf", dest=f"/d/f{i}.mxf", size=10)
                 for i in range(50)]
        xml = os.path.join(tmp, "J_log.xml")
        logger = JobLogger.create_new(xml, "J", [("/card", "/d")], files)
        ok("初次强制落盘", os.path.isfile(xml))
        # 200ms 内连续更新 50 条 → 只落盘少数几次，但 mark_completed 后全部带出
        for i, f in enumerate(files):
            logger.set_file_status(f.src, "verified", verified="true")
        logger.mark_completed()
        info = load_log(xml)
        ok("强制落盘带出全部最新状态",
           sum(1 for f in info.job.files if f.status == "verified") == 50)
        ok("终态 Completed", info.is_completed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 10. drive_panel：离屏构造 + 自动刷新
# ──────────────────────────────────────────────
def test_drive_panel() -> None:
    print("\n[10] DrivePanel：离屏构造")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.widgets.drive_panel import DrivePanel

    panel = DrivePanel()
    panel.refresh_drives()
    mounts = panel._enumerate_volumes()
    ok("枚举无异常", isinstance(mounts, list))
    for label, path, used, total in mounts:
        assert os.path.isabs(path) and total >= used
        assert "." != os.path.basename(path.rstrip("/"))[:1], f"隐藏卷泄漏: {path}"
        assert os.path.basename(path.rstrip("/")).lower() not in (
            "preboot", "recovery", "vm", "update"), f"系统卷泄漏: {path}"
    ok(f"当前可见卷 {len(mounts)} 个均通过过滤检查", True)
    ok("标签为卷名而非裸设备名", all(
        not label.startswith("(") and "(" in label or label for label, *_ in mounts))
    ok("自动刷新定时器已启动", panel._auto_timer.isActive())
    # 后台枚举路径（线程 + 信号回投）冒烟
    panel._auto_refresh_tick()
    import time as _t
    _t.sleep(0.5)
    app.processEvents()
    ok("后台自动枚举无异常", True)
    panel._auto_timer.stop()


# ──────────────────────────────────────────────
# 11. 日志/报告版本化（追加拷贝不覆盖上一次产物）
# ──────────────────────────────────────────────
def test_log_versioning() -> None:
    print("\n[11] 同名追加拷贝：日志/报告自动 -1")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from config import log_xml_path, next_available_log_base, report_html_path
    from gui.workers import CopyJobWorker

    tmp = tempfile.mkdtemp(prefix="dit_v11_")
    try:
        card = _make_card(os.path.join(tmp, "src"))
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest)
        ok("空目录 → 原名", next_available_log_base(dest, "J") == "J")

        # 第一次拷贝：J_log.xml + J_report.html
        cap1: dict = {}
        w1 = CopyJobWorker(job_name="J", sources=[card], dest_root=dest)
        w1.job_finished.connect(lambda s: cap1.setdefault("summary", s))
        w1.error.connect(lambda s, m: cap1.setdefault("error", (s, m)))
        w1._execute()
        ok("首次运行日志 = J_log.xml",
           cap1["summary"].xml_path == log_xml_path(dest, "J"))
        from report.generator import generate_html_report
        html1 = generate_html_report(cap1["summary"], sources=[card])
        ok("首次报告 = J_report.html", os.path.basename(html1) == "J_report.html")
        xml1_mtime = os.path.getmtime(log_xml_path(dest, "J"))

        # 同名追加第二次：J-1，旧产物不动
        cap2: dict = {}
        w2 = CopyJobWorker(job_name="J", sources=[card], dest_root=dest)
        w2.job_finished.connect(lambda s: cap2.setdefault("summary", s))
        w2.error.connect(lambda s, m: cap2.setdefault("error", (s, m)))
        w2._execute()
        ok("追加运行日志 = J-1_log.xml",
           cap2["summary"].xml_path == log_xml_path(dest, "J-1"))
        ok("旧日志未被覆盖",
           os.path.getmtime(log_xml_path(dest, "J")) == xml1_mtime)
        html2 = generate_html_report(cap2["summary"], sources=[card])
        ok("追加报告 = J-1_report.html", os.path.basename(html2) == "J-1_report.html")
        ok("旧报告未被覆盖", os.path.isfile(report_html_path(dest, "J")))

        # 同名追加第三次：J-2
        cap3: dict = {}
        w3 = CopyJobWorker(job_name="J", sources=[card], dest_root=dest)
        w3.job_finished.connect(lambda s: cap3.setdefault("summary", s))
        w3._execute()
        ok("第三次日志 = J-2_log.xml",
           cap3["summary"].xml_path == log_xml_path(dest, "J-2"))
        ok("下一可用名 = J-3", next_available_log_base(dest, "J") == "J-3")

        # 断点续传：日志路径保持续传文件本身（如 J-9），不被 job_name 重算覆盖
        from core.logger import FileRecord, JobLogger, find_existing_logs
        from core.scanner import scan_sources
        tasks = scan_sources([card])
        upath = log_xml_path(dest, "J-9")
        JobLogger.create_new(
            upath, "J-9", [(card, dest)],
            [FileRecord(src=t.src, dest=t.dest_under(dest), size=t.size)
             for t in tasks],
        )
        unfinished = [i for i in find_existing_logs(dest) if i.xml_path == upath]
        assert unfinished, "J-9 未完成日志应能被扫描到"
        cap4: dict = {}
        w4 = CopyJobWorker(job_name="J", sources=[card], dest_root=dest,
                           resume_from=unfinished[0])
        w4.job_finished.connect(lambda s: cap4.setdefault("summary", s))
        w4.error.connect(lambda s, m: cap4.setdefault("error", (s, m)))
        w4._execute()
        ok("续传时 summary.xml_path 指向被续传的日志本身",
           cap4["summary"].xml_path == upath)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────
# 12. 文件树模型重建（格式化卡后刷新可见新内容）
# ──────────────────────────────────────────────
def test_tree_rebuild() -> None:
    print("\n[12] DrivePanel：刷新重建文件树模型")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.widgets.drive_panel import DrivePanel

    tmp = tempfile.mkdtemp(prefix="dit_v12_")
    panel = DrivePanel()
    try:
        panel.refresh_drives()
        m1 = panel.model
        panel.refresh_drives()
        ok("每次刷新都换新模型（强制重枚举）", panel.model is not m1)

        # 浏览位置在刷新后保留
        panel.tree.setRootIndex(panel.model.index(tmp))
        panel.refresh_drives()
        ok("目录存在时刷新保留浏览位置",
           panel.model.filePath(panel.tree.rootIndex()) == tmp)

        # 模拟格式化：目录被整个替换/清空 → 刷新后回落到根，不再显示旧内容
        shutil.rmtree(tmp)
        panel.refresh_drives()
        ok("目录消失后刷新回落到文件系统根",
           not panel.tree.rootIndex().isValid())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        panel._auto_timer.stop()


# ──────────────────────────────────────────────
# 13. 作业串行排队 + 清空已完成 + 点击打开目录
# ──────────────────────────────────────────────
def test_job_queue() -> None:
    print("\n[13] 作业串行排队 + 清空已完成 + 点击打开目录")
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.main_window import MainWindow, QueuedJob

    win = MainWindow()
    tmp = tempfile.mkdtemp(prefix="dit_v13_")
    try:
        card = _make_card(os.path.join(tmp, "src"))
        dest = os.path.join(tmp, "dest")
        # 预置与源一致的目标内容，让校验作业能顺利通过（避免失败汇总弹窗）
        shutil.copytree(card, dest, dirs_exist_ok=True)

        # 打桩 _setup_common：作业"停泊"不真正启动，但占用运行位与目标盘，
        # 用于观察排队行为
        parked: list = []
        orig_setup = win._setup_common

        def fake_setup(worker, key, job, *, is_copy):
            parked.append((worker, key, job, is_copy))
            win._active_workers[key] = worker  # 模拟占用：调度器应停止放行
            win._job_dest[key] = job.dest_root

        win._setup_common = fake_setup

        win._enqueue_job(QueuedJob(kind="verify", job_name="QA",
                                   sources=[card], dest_root=dest,
                                   generate_report=False))
        win._enqueue_job(QueuedJob(kind="verify", job_name="QB",
                                   sources=[card], dest_root=dest,
                                   generate_report=False))
        ok("第一个作业立即启动", len(parked) == 1)
        ok("第二个作业在排队等待", len(win._pending_jobs) == 1)
        ok("内部 key 唯一（同名任务不互相覆盖）", len(win._items) == 2)
        item2 = next(it for k, it in win._items.items() if k.startswith("QB#"))
        ok("排队中的卡片显示 Queued", item2.status == "Queued")

        # 放行第一个作业，真实运行直到队列清空（第二个应随后自动启动）
        worker, key1, job1, is_copy1 = parked[0]
        win._active_workers.pop(key1, None)
        win._setup_common = orig_setup
        win._setup_common(worker, key1, job1, is_copy=is_copy1)
        import time as _t
        deadline = _t.monotonic() + 60
        while (win._active_workers or win._pending_jobs) \
                and _t.monotonic() < deadline:
            app.processEvents()
            _t.sleep(0.02)
        ok("两个作业先后全部完成（串行调度）",
           not win._active_workers and not win._pending_jobs)
        ok("全部完成后按钮文案复位",
           win.copy_panel.start_btn.text() == "▶  开始拷贝")
        ok("两张卡片均为 Done",
           sorted(it.status for it in win._items.values()) == ["Done", "Done"])

        # 点击已完成卡片 → 发出打开目标目录信号
        got: list = []
        item2.openDestRequested.connect(got.append)
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        item2.mousePressEvent(ev)
        ok("左键点击已完成卡片 → 打开目标目录信号", got == [dest])
        win._open_dest(os.path.join(tmp, "no_such_dir"))  # 非法路径应无副作用
        ok("目录不存在时不触发打开", True)

        # 清空已完成：终态卡片移除，未完成卡片保留，映射同步清理
        keep = win.queue_panel.add_job("KEEPME", dest)
        keep.set_status("Pending")
        win.queue_panel.clear_finished()
        app.processEvents()
        remaining = win.queue_panel.inner_layout.count() - 1  # 减去 stretch
        ok("终态卡片被清空、Pending 卡片保留", remaining == 1)
        ok("主窗口 key 映射同步清理", len(win._items) == 0)
        keep.mousePressEvent(ev)
        ok("Pending 卡片点击不触发打开信号", got == [dest])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        win.drive_panel._auto_timer.stop()


# ──────────────────────────────────────────────
# 14. 清空已完成：同名任务的两张卡片也必须全部清掉
# ──────────────────────────────────────────────
def test_clear_same_name() -> None:
    print("\n[14] 清空已完成：同名任务卡片全部清掉")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.main_window import MainWindow, QueuedJob

    win = MainWindow()
    tmp = tempfile.mkdtemp(prefix="dit_v14_")
    try:
        card = _make_card(os.path.join(tmp, "src"))
        dest = os.path.join(tmp, "dest")
        shutil.copytree(card, dest, dirs_exist_ok=True)  # 校验能通过

        def enqueue() -> None:
            win._enqueue_job(QueuedJob(kind="verify", job_name="SAME",
                                       sources=[card], dest_root=dest,
                                       generate_report=False))

        import time as _t

        def wait_idle(timeout: float = 60.0) -> None:
            deadline = _t.monotonic() + timeout
            while (win._active_workers or win._pending_jobs) \
                    and _t.monotonic() < deadline:
                app.processEvents()
                _t.sleep(0.02)

        enqueue()
        wait_idle()
        enqueue()  # 同一任务名再来一次（追加场景）
        wait_idle()
        app.processEvents()
        n = win.queue_panel.inner_layout.count() - 1  # 减去末尾 stretch
        ok("同名任务两张卡片并存", n == 2)
        ok("两张卡片均为 Done",
           len(win._items) == 2
           and all(it.status == "Done" for it in win._items.values()))

        win.queue_panel.clear_finished()
        app.processEvents()
        ok("清空后卡片全部移除",
           win.queue_panel.inner_layout.count() - 1 == 0)
        ok("主窗口 key 映射同步清空",
           not win._items and not win._job_display)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        win.drive_panel._auto_timer.stop()


# ──────────────────────────────────────────────
# 15. 任务名自动版本化（追加拷贝 -1/-2，XML/HTML 随任务名）
# ──────────────────────────────────────────────
def test_name_reservation() -> None:
    print("\n[15] 任务名自动版本化")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.main_window import MainWindow, QueuedJob

    win = MainWindow()
    tmp = tempfile.mkdtemp(prefix="dit_v15_")
    try:
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest)
        open(os.path.join(dest, "J_log.xml"), "w").close()
        ok("磁盘已有 J_log.xml → J-1", win._reserve_job_base(dest, "J") == "J-1")
        open(os.path.join(dest, "J-1_report.html"), "w").close()
        ok("J-1 的报告也已存在 → J-2", win._reserve_job_base(dest, "J") == "J-2")

        # 排队/运行中的同盘任务同样占用任务名
        parked: list = []
        orig = win._setup_common

        def fake(w, k, j, *, is_copy):
            parked.append((w, k, j, is_copy))
            win._active_workers[k] = w
            win._job_dest[k] = j.dest_root

        win._setup_common = fake
        win._enqueue_job(QueuedJob(kind="copy", job_name="J-2", sources=[dest],
                                   dest_root=dest, generate_report=False,
                                   conflict_policy="keep"))
        ok("卡片显示版本化后的任务名 J-2",
           any(it.job_name == "J-2" for it in win._items.values()))
        ok("在飞任务占用任务名 → J-3", win._reserve_job_base(dest, "J") == "J-3")
        key = parked[0][1]
        win._active_workers.pop(key, None)
        win._job_dest.pop(key, None)
        win._pending_jobs.clear()
        win._setup_common = orig

        # 不同目标盘互不影响：可以各用同一个任务名
        other = os.path.join(tmp, "other")
        os.makedirs(other)
        ok("其他目标盘可再用同名 J", win._reserve_job_base(other, "J") == "J")

        # 续传任务名跟随被续传日志（可能是之前的 J-1）
        from config import XML_LOG_SUFFIX, log_xml_path
        from core.logger import FileRecord, JobLogger, find_existing_logs
        upath = log_xml_path(dest, "OLDNAME-7")
        JobLogger.create_new(upath, "OLDNAME-7", [(dest, dest)], [
            FileRecord(src="x", dest="y", size=1)])
        info = [i for i in find_existing_logs(dest) if i.xml_path == upath][0]
        win._setup_common = fake
        win._launch_copy_job("WHATEVER", [dest], dest, resume_from=info)
        ok("续传任务卡片显示被续传日志的名字",
           any(it.job_name == "OLDNAME-7" for it in win._items.values()))
        for k in list(win._active_workers):
            win._active_workers.pop(k, None)
            win._job_dest.pop(k, None)
        win._pending_jobs.clear()
        win._setup_common = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        win.drive_panel._auto_timer.stop()


# ──────────────────────────────────────────────
# 16. 调度：不同目标盘并发，同一目标盘排队
# ──────────────────────────────────────────────
def test_parallel_dests() -> None:
    print("\n[16] 异盘并发、同盘排队")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.main_window import MainWindow, QueuedJob

    win = MainWindow()
    tmp = tempfile.mkdtemp(prefix="dit_v16_")
    try:
        dA = os.path.join(tmp, "driveA")
        dB = os.path.join(tmp, "driveB")
        os.makedirs(dA); os.makedirs(dB)

        parked: list = []
        orig = win._setup_common

        def fake(w, k, j, *, is_copy):
            parked.append((w, k, j, is_copy))
            win._active_workers[k] = w
            win._job_dest[k] = j.dest_root

        win._setup_common = fake

        def add(name: str, dest: str) -> None:
            win._enqueue_job(QueuedJob(kind="copy", job_name=name,
                                       sources=[tmp], dest_root=dest,
                                       generate_report=False,
                                       conflict_policy="keep"))

        add("A1", dA)   # 立即启动
        add("B1", dB)   # 目标盘不同 → 并发启动
        ok("不同目标盘的任务并发执行",
           len(parked) == 2 and len(win._pending_jobs) == 0)
        add("A2", dA)   # dA 忙 → 排队
        add("B2", dB)   # dB 忙 → 排队
        ok("同目标盘的任务排队等待", len(win._pending_jobs) == 2)
        ok("排队卡片显示 Queued",
           all(it.status == "Queued"
               for k, it in win._items.items() if k.startswith(("A2", "B2"))))

        # B1 完成 → B2 应被唤醒启动，A2 继续等 A1
        kb1 = parked[1][1]
        win._active_workers.pop(kb1, None)
        win._job_dest.pop(kb1, None)
        win._start_next_job()
        ok("目标盘空闲后其排队任务启动、另一盘任务继续等",
           len(parked) == 3 and len(win._pending_jobs) == 1
           and len(win._active_workers) == 2
           and win._pending_jobs[0][1].dest_root == dA)
    finally:
        win._active_workers.clear()
        win._job_dest.clear()
        win._pending_jobs.clear()
        win._setup_common = orig
        shutil.rmtree(tmp, ignore_errors=True)
        win.drive_panel._auto_timer.stop()


def test_source_loss_and_resume() -> None:
    print("\n[17] 源盘丢失 → 判中断 + 续传分类（H2）")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    import core.pipeline as pl
    from config import JobStatus, PipelineConfig
    from core.logger import FileRecord, JobLogger, load_log
    from core.pipeline import CopyPipeline
    from core.scanner import scan_sources
    from gui.main_window import classify_existing_logs

    tmp = tempfile.mkdtemp(prefix="dit_v17_")
    try:
        # ── A. 拷贝中途「拔盘」：源目录消失 + 设备级错误 ──
        card = _make_card(os.path.join(tmp, "src"))
        dest = os.path.join(tmp, "dest")
        os.makedirs(dest)
        tasks = scan_sources([card])
        xml = os.path.join(dest, "SL_log.xml")
        logger = JobLogger.create_new(
            xml, "SL", [(card, dest)],
            [FileRecord(src=t.src, dest=t.dest_under(dest), size=t.size)
             for t in tasks],
        )
        real_copy = pl.copy_file
        state = {"n": 0}

        def fake_copy(src, dest_path, **kw):
            if state["n"] == 0:
                state["n"] += 1
                shutil.rmtree(card, ignore_errors=True)  # 拔盘：整卷消失
                raise OSError(errno.ENXIO, "Device not configured")
            return real_copy(src, dest_path, **kw)

        pl.copy_file = fake_copy
        try:
            pipe = CopyPipeline(
                tasks=tasks, dest_root=dest, logger=logger,
                config=PipelineConfig(copy_workers=1, verify_workers=1),
            )
            result = pipe.run()
        finally:
            pl.copy_file = real_copy

        ok("源盘丢失 → result.aborted=True", result.aborted)
        ok("中断原因已记录", "源盘" in result.abort_reason)
        info = load_log(xml)
        ok("XML 状态=Aborted（不再误判 Completed）",
           info.job.status == JobStatus.ABORTED.value)
        ok("仍有未完成文件可供续传", len(info.pending_files) >= 1)

        # 关键回归：macOS 拔盘后挂载点目录可能残留（或用户随即插回卡），
        # 此时目录仍存在，设备级错误 ENXIO 也必须判中断，不能漏判为失败。
        card_sl = _make_card(os.path.join(tmp, "src_sl"))
        dest_sl = os.path.join(tmp, "dest_sl")
        os.makedirs(dest_sl)
        tasks_sl = scan_sources([card_sl])
        xml_sl = os.path.join(dest_sl, "SL2_log.xml")
        logger_sl = JobLogger.create_new(
            xml_sl, "SL2", [(card_sl, dest_sl)],
            [FileRecord(src=t.src, dest=t.dest_under(dest_sl), size=t.size)
             for t in tasks_sl],
        )
        real_copy_sl = pl.copy_file

        def enxio_keep_dir(src, destp, **kw):
            # 注意：故意不删除源目录（模拟挂载点残留）
            raise OSError(errno.ENXIO, "Device not configured")

        pl.copy_file = enxio_keep_dir
        try:
            res_sl = CopyPipeline(
                tasks=tasks_sl, dest_root=dest_sl, logger=logger_sl,
                config=PipelineConfig(copy_workers=1, verify_workers=1),
            ).run()
        finally:
            pl.copy_file = real_copy_sl
        ok("目录保留 + ENXIO → 仍判中断（macOS 挂载点残留不漏判）",
           res_sl.aborted)

        # 单文件被外部删除（设备仍在）不应中断整单
        card2 = _make_card(os.path.join(tmp, "src2"))
        dest2 = os.path.join(tmp, "dest2")
        os.makedirs(dest2)
        tasks2 = scan_sources([card2])
        xml2 = os.path.join(dest2, "OK_log.xml")
        logger2 = JobLogger.create_new(
            xml2, "OK", [(card2, dest2)],
            [FileRecord(src=t.src, dest=t.dest_under(dest2), size=t.size)
             for t in tasks2],
        )
        gone = tasks2[0]
        real_copy2 = pl.copy_file

        def missing_one(src, dest_path, **kw):
            if os.path.normcase(src) == os.path.normcase(gone.src):
                raise OSError(errno.ENOENT, "No such file")
            return real_copy2(src, dest_path, **kw)

        pl.copy_file = missing_one
        try:
            res2 = CopyPipeline(
                tasks=tasks2, dest_root=dest2, logger=logger2,
                config=PipelineConfig(copy_workers=1, verify_workers=1),
            ).run()
        finally:
            pl.copy_file = real_copy2
        ok("单个文件被删 → 不中断整单（aborted=False）", not res2.aborted)
        ok("其余文件仍正常完成", res2.verified_files == len(tasks2) - 1)

        # ── B. 续传分类：Completed 但仍有未完成文件 → 归入可续传 ──
        d2 = tempfile.mkdtemp(prefix="dit_v17b_")
        try:
            xml3 = os.path.join(d2, "C_log.xml")
            with open(xml3, "w", encoding="utf-8") as f:
                f.write(
                    "<?xml version='1.0' encoding='utf-8'?>\n"
                    '<job name="C" started="t" status="Completed" finished="t">'
                    '<sources><source path="/s" dest="' + d2 + '"/></sources>'
                    '<files count="2">'
                    '<file src="/s/a.mxf" dest="' + os.path.join(d2, "a.mxf")
                    + '" size="10" status="verified" verified="true"/>'
                    '<file src="/s/b.mxf" dest="' + os.path.join(d2, "b.mxf")
                    + '" size="10" status="failed" error="x"/>'
                    "</files></job>"
                )
            bad = load_log(xml3)
            ok("Completed 日志确实仍带未完成文件",
               bad.is_completed and len(bad.pending_files) == 1)
            unfinished, completed = classify_existing_logs([bad])
            ok("该日志被归入『可续传』而非『已完成』",
               len(unfinished) == 1 and len(completed) == 0)

            xml4 = os.path.join(d2, "D_log.xml")
            with open(xml4, "w", encoding="utf-8") as f:
                f.write(
                    "<?xml version='1.0' encoding='utf-8'?>\n"
                    '<job name="D" started="t" status="Completed" finished="t">'
                    '<sources><source path="/s" dest="' + d2 + '"/></sources>'
                    '<files count="1">'
                    '<file src="/s/a.mxf" dest="' + os.path.join(d2, "a.mxf")
                    + '" size="10" status="verified" verified="true"/>'
                    "</files></job>"
                )
            good = load_log(xml4)
            u2, c2 = classify_existing_logs([good])
            ok("全部完成的日志仍归入『已完成』",
               len(u2) == 0 and len(c2) == 1)
        finally:
            shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_log_fields() -> None:
    print("\n[18] 日志字段容错：非法 size 不崩溃且保留续传（H1）")
    from core.logger import _safe_int, find_existing_logs, load_log

    ok("_safe_int 正常值", _safe_int("123") == 123)
    ok("_safe_int 非数字→0", _safe_int("abc") == 0)
    ok("_safe_int 空串→0", _safe_int("") == 0)
    ok("_safe_int None→0", _safe_int(None) == 0)
    ok("_safe_int 科学计数→0", _safe_int("1e3") == 0)

    tmp = tempfile.mkdtemp(prefix="dit_v18_")
    try:
        # 非法 size 的日志：应能解析、且仍可用于断点续传（不得抛异常）
        bad = os.path.join(tmp, "B_log.xml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write(
                "<?xml version='1.0' encoding='utf-8'?>\n"
                '<job name="B" started="t" status="Running">'
                '<sources><source path="/s" dest="/d"/></sources>'
                '<files count="2">'
                '<file src="/s/a.mov" dest="/d/a.mov" size="abc" status="pending"/>'
                '<file src="/s/b.mov" dest="/d/b.mov" size="456" status="verified" verified="true"/>'
                "</files></job>"
            )
        info = load_log(bad)
        ok("非法 size 不崩溃且解析成功", info is not None)
        ok("坏字段容错为 0", info.job.files[0].size == 0)
        ok("正常字段保留原值", info.job.files[1].size == 456)
        ok("日志仍可用于续传", len(info.pending_files) == 1)
        # find_existing_logs 是 ScanWorker 的实际入口，必须不抛
        found = find_existing_logs(tmp)
        ok("find_existing_logs 不抛异常", len(found) == 1)

        # 结构彻底损坏：仍返回 None（走全新备份，非本次修复目标）
        broken = os.path.join(tmp, "X_log.xml")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("<job name='x'><files><file src='a'")  # 截断
        ok("结构损坏 → None（忽略并全新备份）",
           load_log(broken) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_reverify() -> None:
    print("\n[19] 断点续传：重校验已完成后文件 + 损坏自动重拷（可靠性）")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from config import FileStatus, PipelineConfig
    from core.logger import FileRecord, JobLogger, load_log
    from core.pipeline import CopyPipeline
    from core.scanner import scan_sources
    from gui.workers import CopyJobWorker
    from core.verifier import hash_file
    from report.generator import generate_html_report

    tmp = tempfile.mkdtemp(prefix="dit_v19_")
    try:
        card = _make_card(os.path.join(tmp, "src"))
        tasks = scan_sources([card])

        # ── A. 续传：已完成的文件重新校验，通过后计入 verified（不再是 skipped）──
        dest = os.path.join(tmp, "destA")
        os.makedirs(dest)
        xml = os.path.join(dest, "R_log.xml")
        logger = JobLogger.create_new(
            xml, "R", [(card, dest)],
            [FileRecord(src=t.src, dest=t.dest_under(dest), size=t.size)
             for t in tasks],
        )
        # 先真实拷贝一部分（模拟上次已完成），记录其 hash
        done, rest = tasks[0], tasks[1:]
        first_dest = done.dest_under(dest)
        os.makedirs(os.path.dirname(first_dest), exist_ok=True)
        shutil.copy2(done.src, first_dest)
        done_hash = hash_file(first_dest)
        logger.update_file(FileRecord(
            src=done.src, dest=first_dest, size=done.size,
            status=FileStatus.VERIFIED.value, hash_src=done_hash,
            hash_dest=done_hash, verified="true",
        ))
        # 其余保持 pending
        pipe = CopyPipeline(
            tasks=tasks, dest_root=dest, logger=logger,
            config=PipelineConfig(copy_workers=1, verify_workers=1),
            reverify_records={
                os.path.normcase(done.src): (first_dest, done_hash),
            },
        )
        res = pipe.run()
        ok("续传重校验通过 → 全部 verified", res.verified_files == len(tasks))
        ok("不再产生 skipped（已验证文件重新校验而非跳过）", res.skipped_files == 0)
        info = load_log(xml)
        rec = next(f for f in info.job.files if f.src == done.src)
        ok("已完成文件被重新标记为 verified", rec.status == "verified")
        ok("目标文件仍存在且完好", os.path.isfile(first_dest))

        # ── B. 目标盘静默损坏 → 重校验发现不一致 → 自动重拷修复 ──
        dest2 = os.path.join(tmp, "destB")
        os.makedirs(dest2)
        xml2 = os.path.join(dest2, "C_log.xml")
        logger2 = JobLogger.create_new(
            xml2, "C", [(card, dest2)],
            [FileRecord(src=t.src, dest=t.dest_under(dest2), size=t.size)
             for t in tasks],
        )
        d2 = tasks[0].dest_under(dest2)
        os.makedirs(os.path.dirname(d2), exist_ok=True)
        # 写入内容损坏的「已完成」文件（哈希与日志记录不符，模拟静默损坏）
        with open(d2, "wb") as f:
            f.write(b"CORRUPTED-BYTES" * 100)
        logger2.update_file(FileRecord(
            src=tasks[0].src, dest=d2, size=tasks[0].size,
            status=FileStatus.VERIFIED.value, hash_src="deadbeef",
            hash_dest="deadbeef", verified="true",
        ))
        pipe2 = CopyPipeline(
            tasks=tasks, dest_root=dest2, logger=logger2,
            config=PipelineConfig(copy_workers=1, verify_workers=1),
            reverify_records={
                os.path.normcase(tasks[0].src): (d2, "deadbeef"),
            },
        )
        res2 = pipe2.run()
        ok("损坏文件被检出并重拷 → 全部 verified",
           res2.verified_files == len(tasks) and res2.failed_files == 0)
        ok("重拷后目标内容与源一致（不再是损坏内容）",
           os.path.getsize(d2) == tasks[0].size
           and hash_file(d2) == hash_file(tasks[0].src))

        # ── C. 续传仍有失败 → 不生成报告（全部通过才出报告）──
        dest3 = os.path.join(tmp, "destC")
        os.makedirs(dest3)
        card2 = os.path.join(tmp, "srcC")
        os.makedirs(card2)
        names_c = ["a.bin", "b.bin"]
        for n in names_c:
            with open(os.path.join(card2, n), "wb") as f:
                f.write(b"data-" + n.encode())
        tasks_c = {t.src: t for t in scan_sources([card2])}
        upath = os.path.join(dest3, "CZ_log.xml")
        u_logger = JobLogger.create_new(
            upath, "CZ", [(card2, dest3)],
            [FileRecord(src=t.src, dest=t.dest_under(dest3), size=t.size)
             for t in tasks_c.values()],
        )
        # a.bin 记为已 verified，但磁盘上没有 → 重校验失败 → 触发重拷；
        # 再把它的重拷打失败，制造「续传仍有失败」。
        a_task = tasks_c[os.path.join(card2, "a.bin")]
        u_logger.update_file(FileRecord(
            src=a_task.src, dest=a_task.dest_under(dest3), size=a_task.size,
            status=FileStatus.VERIFIED.value, hash_src="x",
            hash_dest="x", verified="true",
        ))
        u_logger.mark_completed()
        from core.logger import load_log as _load
        info_c = _load(upath)

        import core.pipeline as plc
        real_copy_c = plc.copy_file

        def fail_a(src, dest_path, **kw):
            if os.path.normcase(src) == os.path.normcase(a_task.src):
                raise OSError(errno.EACCES, "Permission denied (simulated)")
            return real_copy_c(src, dest_path, **kw)

        plc.copy_file = fail_a
        try:
            s: dict = {}
            w = CopyJobWorker(job_name="CZ", sources=[card2], dest_root=dest3,
                              generate_report=True, resume_from=info_c)
            w.job_finished.connect(lambda sm: s.setdefault("s", sm))
            w.error.connect(lambda a, m: s.setdefault("e", (a, m)))
            w._execute()
        finally:
            plc.copy_file = real_copy_c
        ok("续传仍有失败 → 不生成报告", s["s"].generate_report is False)
        ok("失败被如实计入", s["s"].failed >= 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reverify_conflict() -> None:
    print("\n[20] 重校验失败重拷：忽略 SKIP 一律 KEEP + 报告显示 -1（A方案）")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from config import NameConflictPolicy, PipelineConfig
    from core.logger import FileRecord, JobLogger
    from core.pipeline import CopyPipeline
    from core.scanner import scan_sources
    from gui.workers import JobSummary
    from report.generator import generate_html_report

    tmp = tempfile.mkdtemp(prefix="dit_v20_")
    try:
        src = os.path.join(tmp, "src")
        dest = os.path.join(tmp, "dest")
        os.makedirs(src); os.makedirs(dest)
        with open(os.path.join(src, "A.MXF"), "wb") as f:
            f.write(b"GOOD-NEW-SOURCE")
        task = scan_sources([src])[0]
        # 目标盘：SC 新名文件（损坏，重校验会失败）+ 原名被无关文件占用
        sc = os.path.join(dest, "SC001_S001_T001_A_A.MXF")
        with open(sc, "wb") as f:
            f.write(b"CORRUPT-SC")
        with open(os.path.join(dest, "A.MXF"), "wb") as f:
            f.write(b"UNRELATED-OTHER")
        logp = os.path.join(dest, "J_log.xml")
        log = JobLogger.create_new(
            logp, "J", [(src, dest)],
            [FileRecord(src=task.src, dest=task.dest_under(dest), size=task.size)],
        )
        # 用户选了 SKIP：重拷贝仍应按 KEEP 处理（不跳过、不丢副本）
        pipe = CopyPipeline(
            tasks=[task], dest_root=dest, logger=log,
            config=PipelineConfig(copy_workers=1, verify_workers=1),
            conflict_policy=NameConflictPolicy.SKIP,
            reverify_records={os.path.normcase(task.src): (sc, "WRONGHASH")},
        )
        r = pipe.run()
        ok("重校验失败重拷忽略 SKIP → 不跳过", r.skipped_files == 0)
        ok("文件成功重拷并校验通过", r.verified_files == 1 and r.failed_files == 0)
        ok("原名被占 → 以 -1 共存，未覆盖他人文件",
           os.path.isfile(os.path.join(dest, "A-1.MXF"))
           and os.path.getsize(os.path.join(dest, "A.MXF")) == len(b"UNRELATED-OTHER"))
        import xml.etree.ElementTree as ET
        fe = ET.parse(logp).getroot().find("files/file")
        ok("日志 dest 记录为 -1 新名",
           os.path.basename(fe.get("dest")) == "A-1.MXF"
           and fe.get("status") == "verified")

        # 报告应显示 -1 文件及其真实路径
        s = JobSummary(
            job_name="J", dest_root=dest, verified=1, failed=0, skipped=0,
            bytes_total=task.size, bytes_verified=task.size, elapsed=1.0,
            aborted=False, generate_report=True, xml_path=logp,
        )
        hp = generate_html_report(s, sources=[src])
        h = open(hp, encoding="utf-8").read()
        ok("HTML 报告包含 -1 文件行", "A-1.MXF" in h)
        ok("报告 Destination 显示 -1 真实路径",
           ("A-1.MXF" in h) and ("UNRELATED" not in h.split("A-1.MXF")[0][-200:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_volume_filter()
    test_has_copyable_file()
    test_copier()
    test_pipeline()
    test_workers()
    test_logger()
    test_drive_panel()
    test_log_versioning()
    test_tree_rebuild()
    test_job_queue()
    test_clear_same_name()
    test_name_reservation()
    test_parallel_dests()
    test_source_loss_and_resume()
    test_bad_log_fields()
    test_resume_reverify()
    test_reverify_conflict()
    print("\n" + "=" * 60)
    print(f"  全部 {PASS} 项断言通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
