# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder, windowed build for Windows x64."""

from pathlib import Path

project_root = Path(SPECPATH).resolve().parent
hiddenimports = [
    "oci.tenant_manager_control_plane",
    "oci.core",
    "oci.identity",
    "oci.limits",
    "oci.monitoring",
    "pyzipper",
    "windnd",
]
a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "coverage", "customtkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OCIBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OCIBot",
)
