# -*- mode: python ; coding: utf-8 -*-
import sys

_is_mac = sys.platform == 'darwin'
_icon_file = 'icon.icns' if _is_mac else 'icon.ico'
_app_name = 'DIT OFFLOADER'

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[(_icon_file, '.')],
    hiddenimports=['xxhash', 'psutil'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

if _is_mac:
    # ── macOS: onedir → .app 捆绑包（目录结构）──────────────────────
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='DIT OFFLOADER',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity='-',
        entitlements_file='entitlements.plist',
        icon=_icon_file,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='DIT OFFLOADER',
    )
    app = BUNDLE(
        coll,
        name='DIT OFFLOADER.app',
        icon=_icon_file,
        bundle_identifier='com.ditoffloader.app',
        info_plist={
            'NSHighResolutionCapable': 'True',
            'LSMinimumSystemVersion': '13.0',
            'CFBundleShortVersionString': '1.0.4',
            'CFBundleVersion': '1.0.4.0',
        },
    )
else:
    # ── Windows: onefile → 单 exe ──────────────────────────────────
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='DIT OFFLOADER',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity='-',
        entitlements_file='entitlements.plist',
        icon=[_icon_file],
    )
