# PyInstaller spec. Build on Windows:
#     pip install pyinstaller
#     pyinstaller packaging/flow-bulk-generator.spec
#
# One-dir, not one-file. A one-file build of this app is roughly 280 MB and
# unpacks all of it to a temp directory on every launch; one-dir starts
# immediately and zips just as easily for sending to someone.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    # ensure_config() seeds a fresh machine from these on first run.
    (str(ROOT / "imgen" / "defaults" / "settings.toml"), "imgen/defaults"),
    (str(ROOT / "imgen" / "defaults" / "selectors.toml"), "imgen/defaults"),
]

# Playwright drives a bundled Node binary over a pipe; without its driver the
# browser layer cannot start at all, even though we only attach to an
# existing Chrome rather than downloading browsers.
datas += collect_data_files("playwright")
datas += collect_data_files("edge_tts")

# ffmpeg, if it was placed here before building. The app also finds a system
# install, so a bundle without it still works wherever ffmpeg is installed.
_ffmpeg_dir = ROOT / "packaging" / "ffmpeg"
if _ffmpeg_dir.is_dir():
    for _binary in _ffmpeg_dir.iterdir():
        if _binary.is_file():
            datas.append((str(_binary), "ffmpeg"))

hiddenimports = [
    "gspread",
    "openpyxl",
    "playwright.sync_api",
    "edge_tts",
]
# edge-tts reaches its service over websockets and discovers submodules late.
hiddenimports += collect_submodules("edge_tts")
hiddenimports += collect_submodules("google.auth")
hiddenimports += collect_submodules("google.oauth2")

# Present in the PySide6 wheel, never used here, and large.
excludes = [
    # piper is the offline speech fallback and costs ~100 MB with
    # onnxruntime, plus a 61 MB voice model that is not in the bundle
    # anyway. Everything this app does needs the network - it drives
    # Google Flow - so the fallback earns less than its weight here.
    # tts.piper_available() catches the ImportError and says so plainly.
    "piper",
    "onnxruntime",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtTest",
    "tkinter",
    "matplotlib",
    "numpy",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FlowBulkGenerator",
    debug=False,
    strip=False,
    upx=False,
    # No console: this is a GUI app, and the launcher reports startup
    # failures through a dialog and a file instead.
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FlowBulkGenerator",
)
