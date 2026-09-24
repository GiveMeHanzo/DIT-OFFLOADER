"""DIT Offload 程序入口。

启动前检查依赖与外部工具，缺失时给出清晰提示而非崩溃。
自动检测 FFmpeg 安装路径（windows winget/scoop/choco / macOS brew）。

运行：
    python main.py
    python main.py --help
"""
from __future__ import annotations

import os
import shutil
import sys


def _ensure_project_root_on_path() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)


def _check_dependencies() -> list[str]:
    missing: list[str] = []
    try:
        import xxhash  # noqa: F401
    except ImportError:
        missing.append("xxhash  (pip install xxhash)")
    try:
        import psutil  # noqa: F401
    except ImportError:
        missing.append("psutil  (pip install psutil)")
    try:
        import PySide6  # noqa: F401
    except ImportError:
        missing.append("PySide6  (pip install PySide6)")
    return missing


def _ensure_ffmpeg_path() -> None:
    """将常见 FFmpeg 安装路径加入 PATH（winget/scoop/choco/brew）。"""
    ffmpeg_bin = ""
    if shutil.which("ffmpeg"):
        return  # 已在 PATH

    # 按平台探测
    if sys.platform == "win32":
        candidates = [
            # winget (Gyan)
            os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
            ),
            # scoop
            os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
            # chocolatey
            r"C:\ProgramData\chocolatey\bin",
            # 手动安装
            r"C:\ffmpeg\bin",
        ]
        target = "ffmpeg.exe"
    else:
        candidates = [
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "/usr/bin",
        ]
        target = "ffmpeg"

    for base in candidates:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            depth = root.replace(base, "").count(os.sep)
            if depth > 4:
                dirs.clear()
                continue
            if target in files:
                ffmpeg_bin = root
                break
        if ffmpeg_bin:
            break

    if ffmpeg_bin:
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")


def _check_external_tools() -> list[str]:
    missing: list[str] = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def _resolve_icon_path() -> str:
    """获取图标路径：开发环境取项目根目录，PyInstaller 打包后取 _MEIPASS。

    Windows 用 icon.ico（PyInstaller --icon 嵌入 exe 资源，同时 _MEIPASS 一份用于
    setWindowIcon）；macOS 用 icon.icns。
    """
    if sys.platform == "darwin":
        filename = "icon.icns"
    else:
        filename = "icon.ico"

    if getattr(sys, "frozen", False):
        candidate = os.path.join(sys._MEIPASS, filename)
        if os.path.isfile(candidate):
            return candidate
    candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if os.path.isfile(candidate):
        return candidate
    return ""


def main() -> int:
    _ensure_project_root_on_path()

    # ── 帮助 ──
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("DIT OFFLOADER — 拷卡  校验  同步文件名")
        print("用法: python main.py")
        print("选项: -h, --help  显示此帮助")
        print()
        print("启动前会自动检查以下依赖：")
        print("  Python: xxhash, psutil, PySide6")
        print("  外部:   ffmpeg, ffprobe（报告功能需要，缺失不阻断主流程）")
        return 0

    # ── 冰冻环境启动提示 ──
    # macOS: PyInstaller 解压 + Qt 加载需要数秒，期间没有任何视觉反馈。
    # 保留终端窗口输出启动进度，GUI 就绪后再关闭，避免用户误以为卡死双击多次。
    _frozen = getattr(sys, "frozen", False)
    if _frozen:
        print("DIT OFFLOADER is starting…", file=sys.stderr)
        print("", file=sys.stderr)

    # ── Python 依赖 ──
    if not _frozen:
        miss = _check_dependencies()
        if miss:
            sys.stderr.write("缺少以下 Python 依赖，请先安装：\n")
            for m in miss:
                sys.stderr.write(f"  - {m}\n")
            sys.stderr.write("\n或运行：python -m pip install -r requirements.txt\n")
            return 2

    # ── FFmpeg 路径探测 ──
    # 冰冻环境（macOS .app bundle）的 PATH 缺少 brew 路径，导致 shutil.which
    # 找不到 ffmpeg/ffprobe。这里显式检查常见安装目录。
    # Windows 打包版同样需要：从资源管理器/更新器等父进程启动时，继承的
    # PATH 可能是安装 ffmpeg（winget/scoop/choco）之前的旧值，shutil.which
    # 会探测失败——显式补探测，保证报告缩略图/元数据功能不静默缺失。
    # 开发环境走完整 os.walk 探测以覆盖 winget/scoop/choco。
    if _frozen and sys.platform == "darwin":
        ffmpeg_bin = ""
        for base in ("/opt/homebrew/bin", "/usr/local/bin"):
            if os.path.isfile(os.path.join(base, "ffmpeg")):
                ffmpeg_bin = base
                break
        if ffmpeg_bin:
            os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
    else:
        _ensure_ffmpeg_path()

    # ── 全局异常勾子 ──
    import traceback as _tb
    _orig = sys.excepthook
    def _hook(etype, value, tb):
        _tb.print_exception(etype, value, tb)
        _orig(etype, value, tb)
    sys.excepthook = _hook

    # ── 外部工具警告（仅开发环境，打包版静默）──
    if not _frozen:
        ext = _check_external_tools()
        if ext:
            print(f"[警告] 以下外部工具未找到，报告抽帧/元数据功能将不可用："
                  f" {', '.join(ext)}", file=sys.stderr)
            print("  macOS:   brew install ffmpeg", file=sys.stderr)
            print("  Windows: winget install Gyan.FFmpeg", file=sys.stderr)

    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import QLockFile, QStandardPaths
    from gui.main_window import MainWindow

    # ── 单实例锁 ──
    _lock_path = os.path.join(
        QStandardPaths.writableLocation(QStandardPaths.TempLocation),
        "DIT_Offload.single_instance.lock",
    )
    _lock = QLockFile(_lock_path)
    if not _lock.tryLock(0):
        # 已有实例在运行，退出（不弹窗、不抢焦点）
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName("DIT OFFLOADER")
    app.setOrganizationName("工部尚书府")

    # 设置窗口图标（开发环境直接读文件，PyInstaller 打包后从 _MEIPASS 取）
    _icon = _resolve_icon_path()
    if _icon:
        app.setWindowIcon(QIcon(_icon))

    window = MainWindow()
    if _icon:
        window.setWindowIcon(QIcon(_icon))
    window.show()

    # ── GUI 就绪，关闭终端 ──
    # macOS: 把进程从后台模式切换到常规 GUI 应用（Dock 显示图标），
    # 同时关闭 stderr 使终端窗口不再接收输出。
    if _frozen and sys.platform == "darwin":
        print("GUI ready.", file=sys.stderr)
        sys.stderr.close()
        try:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(0)
        except Exception:
            pass

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
