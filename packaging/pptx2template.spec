# -*- mode: python ; coding: utf-8 -*-
# Build the standalone app:   pyinstaller packaging/pptx2template.spec
#   Windows -> dist/pptx2template.exe          (no console window; closes itself when the browser tab is gone)
#   macOS   -> dist/pptx2template.app
#   Linux   -> dist/pptx2template              (run from a terminal or a .desktop file)
# PyInstaller only builds for the OS it runs on; CI builds all three (.github/workflows/release.yml).
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "packaging"
ICON = str(PKG / ("icon.ico" if sys.platform == "win32" else "icon.png"))

a = Analysis(
    [str(PKG / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "pptx2template" / "ui" / "static"), "pptx2template/ui/static")],
    hiddenimports=["pptx2template.ui.server", "yaml"],
    excludes=["tkinter", "pytest", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="pptx2template", console=False, icon=ICON)
    coll = COLLECT(exe, a.binaries, a.datas, name="pptx2template")
    app = BUNDLE(
        coll,
        name="pptx2template.app",
        icon=ICON,
        bundle_identifier="io.github.pptx2template",
        info_plist={"CFBundleShortVersionString": "0.1.0", "NSHighResolutionCapable": True},
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="pptx2template",
        console=sys.platform != "win32",
        icon=ICON,
        upx=False,
    )
