# -*- mode: python ; coding: utf-8 -*-

import os
import re
import sys
from pathlib import Path


project_root = Path(SPECPATH)
source_file = project_root / "photo_viewer.py"
source_text = source_file.read_text(encoding="utf-8-sig")
version_match = re.search(r'^CURRENT_VERSION = "([^"]+)"', source_text, re.MULTILINE)
if not version_match:
    raise RuntimeError("CURRENT_VERSION not found in photo_viewer.py")
app_version = version_match.group(1)

is_macos = sys.platform == "darwin"
icon_file = project_root / ("bird.icns" if is_macos else "bird.ico")

a = Analysis(
    [str(source_file)],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "bird.ico"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if is_macos:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Water-redstart",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=os.environ.get("APPLE_CODESIGN_IDENTITY") or None,
        entitlements_file=None,
        icon=str(icon_file),
    )
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        name="Water-redstart",
    )
    app = BUNDLE(
        collected,
        name="Water-redstart.app",
        icon=str(icon_file),
        bundle_identifier="com.crowpaw.waterredstart",
        info_plist={
            "CFBundleDisplayName": "Water-redstart",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "NSHighResolutionCapable": True,
        },
    )
else:
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct,
    )
    version_tuple = tuple(int(part) for part in app_version.split("."))
    version_tuple = (version_tuple + (0, 0, 0, 0))[:4]
    windows_version = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version_tuple, prodvers=version_tuple, mask=0x3f,
            flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0),
        ),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Crowpaw"),
                StringStruct("FileDescription", "Water-redstart: the chirping viewer"),
                StringStruct("FileVersion", app_version),
                StringStruct("ProductName", "Water-redstart"),
                StringStruct("ProductVersion", app_version),
                StringStruct("OriginalFilename", f"v{app_version}.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Water-redstart",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        icon=str(icon_file),
        version=windows_version,
    )

