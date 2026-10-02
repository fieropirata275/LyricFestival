"""Pin a current Microsoft C++ runtime (msvcp140 14.4x) into the process.

Some wheels ship an old private msvcp140.dll (winrt: 14.29). If that copy is
loaded first, native libraries built with a newer toolset (CTranslate2,
ONNX Runtime) bind to it and crash with an access violation inside
std::mutex. Loading the current runtime by full path before anything else
makes every later by-name request resolve to it.
"""
import os
import sys
from pathlib import Path

_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
         "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll")
_done = False


def _candidates():
    here = Path(__file__).resolve().parent
    out = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        out.append(Path(mei) / "app" / "vcrt")
    out.append(here / "vcrt")
    out.append(here / "app" / "vcrt")
    return out


def preload():
    global _done
    if _done or os.name != "nt":
        return
    _done = True
    import ctypes
    for d in _candidates():
        if not (d / "msvcp140.dll").is_file():
            continue
        for n in _DLLS:
            f = d / n
            if f.is_file():
                try:
                    ctypes.WinDLL(str(f))
                except OSError:
                    pass
        return


preload()
