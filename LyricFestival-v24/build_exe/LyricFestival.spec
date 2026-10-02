# -*- mode: python ; coding: utf-8 -*-
# Build:  BUILD_EXE.bat   (or: pyinstaller --noconfirm --clean build_exe\LyricFestival.spec)
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP = os.path.join(ROOT, "app")

datas = [
    (os.path.join(APP, "static"), "app/static"),
    (os.path.join(APP, "cache"), "app/cache"),
    (os.path.join(APP, "mxm_cache"), "app/mxm_cache"),
    (os.path.join(APP, "vcrt"), "app/vcrt"),
]
binaries = []
hiddenimports = [
    "vcrt_preload", "cover_art", "forced_align", "sync_engine", "server", "media_oracle", "lrclib", "musixmatch", "lyrics_engine", "beat_oracle", "private_audio",
    "webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "clr_loader",
]

# Native libraries / data only. Packages with official hooks (webview, av,
# soundcard, onnxruntime, rapidfuzz, pythonnet) handle themselves; nothing
# here imports optional tool sub-packages during analysis.
for pkg in ("ctranslate2", "aiortc", "pylibsrtp", "google_crc32c", "clr_loader", "tokenizers"):
    binaries += collect_dynamic_libs(pkg)
datas += collect_data_files("faster_whisper")
datas += collect_data_files("clr_loader")
hiddenimports += collect_submodules("aiortc")
hiddenimports += collect_submodules("zeroconf")
hiddenimports += collect_submodules("winrt")
hiddenimports += ["faster_whisper", "faster_whisper.vad", "onnxruntime", "ctranslate2"]

excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "torch", "tensorflow", "transformers", "onnx",
    "onnxruntime.quantization", "onnxruntime.transformers", "onnxruntime.tools",
    "onnxruntime.datasets", "onnxruntime.training", "ctranslate2.converters",
    "huggingface_hub.cli", "huggingface_hub.inference", "webview.platforms.android",
    "webview.platforms.gtk", "webview.platforms.qt", "webview.platforms.cocoa",
]

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT, APP],
    binaries=binaries,
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
    a.binaries,
    a.datas,
    [],
    name="LyricFestival",
    icon=os.path.join(SPECPATH, "lyricfestival.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    console=False,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    bootloader_ignore_signals=False,
)
