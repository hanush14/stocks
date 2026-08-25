# PyInstaller spec - produces a genuine standalone ledger.exe
#
# This must run ON WINDOWS. PyInstaller bundles the host OS's Python runtime,
# so a Linux build produces a Linux binary, never a .exe. There is no supported
# cross-compilation path; anyone offering one is shipping you something broken.
#
#   py -m pip install pyinstaller
#   py -m PyInstaller packaging\\ledger.spec
#
# Result: dist\\ledger.exe  (~10 MB, no Python install needed to run it)

block_cipher = None

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ledger.cli", "ledger.store", "ledger.schema",
        "ledger.parse.ptr",
        "ledger.sources.base", "ledger.sources.house", "ledger.sources.edgar",
        "ledger.analytics.performance", "ledger.analytics.ranking",
        "ledger.analytics.signals", "ledger.analytics.stats",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Optional PDF/OCR backends. Excluded so the base exe stays small; install
    # them alongside and they are picked up at runtime instead.
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "PIL"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="ledger",
    console=True,
    upx=False,
    debug=False,
    strip=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
