# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller spec for PhotoSweep (Windows & macOS)."""

import sys
from PyInstaller.utils.hooks import collect_all

is_macos = sys.platform == "darwin"

datas = [
    ("static", "static"),
    ("models/face_detection_yunet_2023mar.onnx", "models"),
]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "cv2",
]

for pkg in ("pillow_heif",):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception:
        pass

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoSweep",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=not is_macos,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PhotoSweep",
)

if is_macos:
    app = BUNDLE(
        coll,
        name="PhotoSweep.app",
        bundle_identifier="com.photosweep.app",
        info_plist={
            "CFBundleName": "PhotoSweep",
            "CFBundleDisplayName": "PhotoSweep",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
