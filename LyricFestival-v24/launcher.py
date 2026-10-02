"""LyricFestival desktop launcher.

Starts the engine in-process, shows the stage in a native window and tears
everything down (web server, WASAPI capture, WebRTC peers, mDNS, child
processes) as soon as the window is closed.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# Must run before any native extension is imported.
try:
    sys.path.insert(0, str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "app"))
    import vcrt_preload  # noqa: F401
except Exception:
    pass

APP_NAME = "LyricFestival"
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = RES / "app"
IS_WIN = os.name == "nt"

DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
LOG_DIR = DATA / "logs"


# --------------------------------------------------------------- utilities
def msgbox(text: str, title: str = APP_NAME, flags: int = 0x40) -> int:
    if IS_WIN:
        try:
            return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)
        except Exception:
            pass
    print(f"{title}: {text}")
    return 1


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / "lyricfestival.log"
    try:
        if log.exists() and log.stat().st_size > 2_000_000:
            old = LOG_DIR / "lyricfestival.prev.log"
            old.unlink(missing_ok=True)
            log.rename(old)
    except Exception:
        pass
    fh = open(log, "a", encoding="utf-8", buffering=1, errors="replace")
    fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · start =====\n")
    if sys.stdout is None or FROZEN:
        sys.stdout = fh
    if sys.stderr is None or FROZEN:
        sys.stderr = fh
    return log


def seed_data_dir() -> None:
    """First run: move bundled caches/token into the writable data folder."""
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ("cache", "mxm_cache", "timing_cache"):
        dst = DATA / name
        dst.mkdir(parents=True, exist_ok=True)
        src = APP_DIR / name
        if not src.is_dir():
            continue
        for f in src.iterdir():
            if f.is_file() and not (dst / f.name).exists():
                try:
                    (dst / f.name).write_bytes(f.read_bytes())
                except Exception:
                    pass


def single_instance() -> object | None:
    if not IS_WIN:
        return object()
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, "Local\\LyricFestival.Singleton")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def port_busy(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


_JOB = None


def bind_children_to_lifetime() -> None:
    """Put this process in a Job Object with KILL_ON_JOB_CLOSE.

    Every process spawned from here (PowerShell probes, WebView2 helpers,
    fallback browser window) is terminated by Windows the moment this
    process ends, even if it crashes or is killed from Task Manager.
    """
    global _JOB
    if not IS_WIN:
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            _JOB = job
            print("[desktop] child processes bound to app lifetime")
    except Exception as exc:
        print(f"[desktop] job object unavailable: {exc}")


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.error = ""
        self.server = None
        self.thread = threading.Thread(target=self._run, name="LF-ENGINE", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            self.error = traceback.format_exc()
            print(self.error)
        finally:
            self.ready.set()
            self.finished.set()

    async def _main(self) -> None:
        from aiohttp import web

        sys.path.insert(0, str(APP_DIR))
        os.chdir(DATA)
        import server  # noqa: E402  (bundled app/server.py)

        self.server = server
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        app = server.build_app()
        runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=2.0)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        local, lan = server.join_urls()
        print(f"[engine] local {URL} · LAN {lan} · mDNS {local}")
        self.ready.set()
        await self.stop_event.wait()
        print("[engine] stopping…")
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=6)
        except Exception as exc:
            print(f"[engine] cleanup timeout/error: {exc}")
        print("[engine] stopped")

    def stop(self, timeout: float = 8.0) -> None:
        if self.loop and self.stop_event and not self.finished.is_set():
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:
                pass
        self.finished.wait(timeout)
        try:
            if self.server:
                self.server.stop_audio()
        except Exception:
            pass


# ------------------------------------------------------------------ window
SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#050507;color:#fff;font-family:Segoe UI,Inter,system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;overflow:hidden}
.w{text-align:center}.r{width:84px;height:84px;margin:0 auto 26px;border-radius:50%;border:2px solid rgba(255,255,255,.08);
border-top-color:#d7ff57;animation:s 1s linear infinite;box-shadow:0 0 40px rgba(215,255,87,.25)}
@keyframes s{to{transform:rotate(360deg)}}
h1{margin:0;font-size:34px;font-weight:900;letter-spacing:-.04em}h1 span{color:#d7ff57}
p{margin:12px 0 0;font:600 11px/1 Consolas,monospace;letter-spacing:.4em;opacity:.45}
</style></head><body><div class="w"><div class="r"></div><h1>LYRIC <span>FESTIVAL</span></h1><p>STARTING ENGINE</p></div></body></html>"""

ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#050507;color:#fff;font-family:Segoe UI,system-ui,sans-serif;padding:48px}
h1{font-size:26px;margin:0 0 12px}p{opacity:.7}pre{white-space:pre-wrap;background:#111118;padding:18px;border-radius:12px;
font:12px/1.5 Consolas,monospace;color:#ff9fb2;max-height:60vh;overflow:auto}
</style></head><body><h1>LyricFestival could not start</h1><p>Log: {log}</p><pre>{err}</pre></body></html>"""


class Api:
    # Only public methods are exposed to JavaScript; the window handle is kept
    # private so the bridge never tries to walk the native window object.
    def __init__(self) -> None:
        self._window = None

    def toggle_fullscreen(self) -> bool:
        if self._window is not None:
            self._window.toggle_fullscreen()
        return True


def find_browser() -> str | None:
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        cands += [
            Path(base) / "Microsoft/Edge/Application/msedge.exe",
            Path(base) / "Google/Chrome/Application/chrome.exe",
            Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def webview2_installed() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
    except Exception:
        return False
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
    ]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def run_window(engine: Engine, log_path: Path) -> None:
    """Blocks until the user closes the stage."""
    try:
        if os.environ.get("LF_BROWSER_MODE") or not webview2_installed():
            raise RuntimeError("WebView2 runtime not available (or browser mode forced)")
        import webview  # pywebview · Edge WebView2

        api = Api()
        win = webview.create_window(
            "LyricFestival",
            html=SPLASH,
            width=1440,
            height=880,
            min_size=(900, 560),
            background_color="#050507",
            text_select=False,
            js_api=api,
        )
        api._window = win

        def boot():
            engine.ready.wait(90)
            if engine.error or engine.finished.is_set():
                err = (engine.error or "Engine exited during startup").replace("<", "&lt;")
                win.load_html(ERROR_PAGE.replace("{log}", str(log_path)).replace("{err}", err))
                return
            win.load_url(URL)

        webview.start(
            boot,
            gui="edgechromium",
            private_mode=False,
            storage_path=str(DATA / "webview"),
            debug=False,
        )
        return
    except Exception:
        print("[desktop] WebView2 window unavailable, falling back:\n" + traceback.format_exc())

    engine.ready.wait(90)
    if engine.error:
        msgbox(f"LyricFestival could not start.\n\nLog: {log_path}", flags=0x10)
        return

    browser = find_browser()
    if browser:
        profile = DATA / "browser"
        profile.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([
            browser,
            f"--app={URL}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            "--autoplay-policy=no-user-gesture-required",
        ])
        proc.wait()
        return

    import webbrowser

    webbrowser.open(URL)
    msgbox("LyricFestival is running in your browser.\n\nPress OK to stop LyricFestival.")


def selftest() -> int:
    """Headless check of the bundled native stack through the real Whisper worker."""
    out = open(os.environ.get("LF_SELFTEST_LOG") or "selftest.log", "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = out
    try:
        import faulthandler
        faulthandler.enable(file=out, all_threads=True)
        sys.path.insert(0, str(APP_DIR))
        import numpy as np
        import lyrics_engine
        sc = lyrics_engine.WhisperSidecar(Path(os.environ.get("TEMP", ".")))
        rng = np.random.default_rng(1)
        audio = (rng.standard_normal(16000 * 4) * 0.05).astype(np.float32)
        for vad in (False, True):
            words = sc.transcribe(audio, beam_size=1, vad_filter=vad, word_timestamps=True,
                                  condition_on_previous_text=False)
            print(f"transcribe vad={vad} -> {len(words)} words via {sc.desc}")
        res = sc.align(audio, [["hello", "world", "again"], ["second", "line"]], "en")
        print("forced align ->", [round(r["score"], 3) for r in res], len(res[0]["words"]), "tokens")
        sc.close()
        import beat_oracle, private_audio, media_oracle, sync_engine, forced_align  # noqa: F401
        print("SELFTEST OK")
        return 0
    except Exception:
        traceback.print_exc()
        return 2


# -------------------------------------------------------------------- main
def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--lf-selftest":
        return selftest()
    if len(sys.argv) > 1 and sys.argv[1] == "--lf-whisper-worker":
        sys.path.insert(0, str(APP_DIR))
        import lyrics_engine
        return lyrics_engine.whisper_worker_main() or 0
    if len(sys.argv) > 1 and sys.argv[1] == "--lf-gpu-probe":
        sys.path.insert(0, str(APP_DIR))
        import lyrics_engine
        return lyrics_engine.gpu_probe_main()
    log_path = setup_logging()
    try:
        import faulthandler
        faulthandler.enable(file=open(LOG_DIR / "crash.log", "a", encoding="utf-8"), all_threads=True)
    except Exception:
        pass
    guard = single_instance()
    if guard is None:
        msgbox("LyricFestival is already running.")
        return 0
    if port_busy(PORT):
        msgbox(
            f"Port {PORT} is already in use.\n\n"
            "Close any other LyricFestival window or console (server.py) and try again.",
            flags=0x30,
        )
        return 1

    bind_children_to_lifetime()
    seed_data_dir()
    os.environ["LF_DATA_DIR"] = str(DATA)
    os.environ["LF_EMBEDDED"] = "1"
    os.environ["LF_STATIC_DIR"] = str(APP_DIR / "static")

    engine = Engine()
    engine.start()
    try:
        run_window(engine, log_path)
    finally:
        print("[desktop] window closed · shutting down")
        engine.stop()
        print("[desktop] bye")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        # Hard exit: releases the WASAPI threads, Whisper executor and the
        # Job Object (which kills any remaining child process).
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
