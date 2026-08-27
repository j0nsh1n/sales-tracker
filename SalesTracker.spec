# PyInstaller spec for the GUI build (Windows .exe or Linux ELF).
# Runtime stays stdlib-only. This file is used only when packaging.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("tkinter")
hiddenimports += [
    "sales_tracker",
    "salestracker",
    "salestracker.models",
    "salestracker.store",
    "salestracker.cli",
    "salestracker.ui",
    "salestracker.ui.gui",
]

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["test_sales_tracker"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SalesTracker",
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
    codesign_identity=None,
    entitlements_file=None,
)
