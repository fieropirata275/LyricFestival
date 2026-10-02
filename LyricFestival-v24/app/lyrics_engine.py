from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
import unicodedata
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
from rapidfuzz.fuzz import ratio

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)

# ---------------------------------------------------------------- CUDA guard
# CTranslate2 aborts the whole process (native exit, no Python exception)
# when cuBLAS/cuDNN are missing at the first GPU transcription. The GPU path
# is therefore validated in a throw-away child process before it is used.
GPU_PROBE_ARG = "--lf-gpu-probe"
_CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
_CUDNN_DLLS = ("cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_ops_infer64_8.dll", "cudnn64_8.dll")


def register_cuda_dll_dirs():
    """Expose pip-installed NVIDIA runtime DLLs (nvidia-*-cu12) to CTranslate2."""
    import os, sys, site
    if os.name != "nt":
        return []
    roots = []
    try:
        roots += [Path(p) for p in site.getsitepackages()]
    except Exception:
        pass
    roots.append(Path(sys.prefix) / "Lib" / "site-packages")
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    added = []
    for r in roots:
        nv = r / "nvidia"
        if not nv.is_dir():
            continue
        for b in nv.glob("*/bin"):
            sb = str(b)
            if sb in added:
                continue
            os.environ["PATH"] = sb + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(sb)
            except Exception:
                pass
            added.append(sb)
    return added


def _find_on_path(names):
    import os
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    try:
        import importlib.util
        spec = importlib.util.find_spec("ctranslate2")
        if spec and spec.origin:
            dirs.insert(0, str(Path(spec.origin).parent))
    except Exception:
        pass
    for d in dirs:
        if not d:
            continue
        for n in names:
            try:
                if (Path(d) / n).is_file():
                    return str(Path(d) / n)
            except Exception:
                pass
    return ""


def gpu_probe_main():
    """Child-process entry: exit 0 only if a real CUDA transcription works."""
    register_cuda_dll_dirs()
    import ctranslate2
    if ctranslate2.get_cuda_device_count() < 1:
        return 3
    from faster_whisper import WhisperModel
    model = WhisperModel("turbo", device="cuda", compute_type="float16")
    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1,
                               vad_filter=False, word_timestamps=True,
                               condition_on_previous_text=False)
    list(segs)
    return 0


def cuda_whisper_usable(cache_dir: Path):
    """Returns (ok, reason). Never crashes the calling process."""
    import os, sys, subprocess
    register_cuda_dll_dirs()
    try:
        import ctranslate2
        ver = getattr(ctranslate2, "__version__", "?")
        if ctranslate2.get_cuda_device_count() < 1:
            return False, "no CUDA device"
    except Exception as exc:
        return False, f"ctranslate2: {exc}"
    cublas = _find_on_path(_CUDA_DLLS)
    cudnn = _find_on_path(_CUDNN_DLLS)
    if os.name == "nt" and (not cublas or not cudnn):
        missing = [n for n, f in (("cuBLAS 12", cublas), ("cuDNN 9", cudnn)) if not f]
        return False, "missing " + " + ".join(missing)

    key = f"{ver}|{cublas}|{cudnn}"
    f = Path(cache_dir) / "gpu_probe.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("key") == key:
            return bool(d.get("ok")), d.get("reason", "cached")
    except Exception:
        pass

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, GPU_PROBE_ARG]
    else:
        here = str(Path(__file__).resolve().parent)
        cmd = [sys.executable, "-c",
               f"import sys;sys.path.insert(0,{here!r});import lyrics_engine as l;sys.exit(l.gpu_probe_main())"]
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=900, errors="replace",
                            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        ok = cp.returncode == 0
        tail = ((cp.stderr or "") + (cp.stdout or "")).strip().splitlines()[-3:]
        reason = "GPU probe passed" if ok else f"GPU probe failed (exit {cp.returncode}): {' | '.join(tail)[:240]}"
    except subprocess.TimeoutExpired:
        return False, "GPU probe timeout"
    except Exception as exc:
        return False, f"GPU probe error: {exc}"
    try:
        f.write_text(json.dumps({"key": key, "ok": ok, "reason": reason}), encoding="utf-8")
    except Exception:
        pass
    return ok, reason


# ------------------------------------------------------------ Whisper sidecar
# Whisper runs in its own process. A native fault inside CTranslate2/OpenMP
# (driver, CPU dispatch, missing DLL...) kills only that worker; the engine,
# the stage and every LAN companion keep running, and the next configuration
# (GPU -> CPU int8 -> CPU float32) is tried automatically.
WHISPER_WORKER_ARG = "--lf-whisper-worker"


def _worker_env_guard():
    import os
    cores = os.cpu_count() or 4
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("KMP_AFFINITY", "disabled")
    os.environ.setdefault("OMP_NUM_THREADS", str(max(2, min(4, cores // 3))))
    # Rendering and audio always win against background transcription.
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def _preload_own_dlls():
    """Load the package-local DLLs by full path before anything else can
    resolve a same-named copy (Windows 11 ships its own onnxruntime.dll)."""
    import os, ctypes, importlib.util
    if os.name != "nt":
        return
    try:
        import vcrt_preload  # noqa: F401
    except Exception:
        pass
    for pkg, names in (("ctranslate2", ("libiomp5md.dll", "ctranslate2.dll")),
                       ("onnxruntime", ("capi/onnxruntime_providers_shared.dll", "capi/onnxruntime.dll"))):
        try:
            spec = importlib.util.find_spec(pkg)
            if not spec or not spec.submodule_search_locations:
                continue
            base = Path(list(spec.submodule_search_locations)[0])
            for n in names:
                f = base / n
                if f.is_file():
                    try:
                        os.add_dll_directory(str(f.parent))
                    except Exception:
                        pass
                    ctypes.WinDLL(str(f))
        except Exception as exc:
            print(f"[whisper-worker] preload {pkg}: {exc}", flush=True)


def whisper_worker_main():
    """Child-process entry point (spawned by WhisperSidecar)."""
    import os, sys, traceback
    log = os.environ.get("LF_WHISPER_LOG")
    if log:
        try:
            fh = open(log, "a", encoding="utf-8", buffering=1, errors="replace")
            sys.stdout = sys.stderr = fh
            import faulthandler
            faulthandler.enable(file=fh, all_threads=True)
        except Exception:
            pass
    _worker_env_guard()
    register_cuda_dll_dirs()
    _preload_own_dlls()
    from multiprocessing.connection import Listener
    port = int(os.environ["LF_WHISPER_PORT"])
    key = bytes.fromhex(os.environ["LF_WHISPER_KEY"])
    cfg = json.loads(os.environ["LF_WHISPER_CFG"])
    model_name, device, compute = cfg[:3]
    use_vad = bool(cfg[3]) if len(cfg) > 3 else True
    with Listener(("127.0.0.1", port), authkey=key) as lst:
        conn = lst.accept()
    print(f"[whisper-worker] loading {model_name} on {device}/{compute}", flush=True)
    try:
        from faster_whisper import WhisperModel
        threads = int(os.environ.get("OMP_NUM_THREADS", "4")) if device == "cpu" else 0
        model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads)
    except Exception as exc:
        traceback.print_exc()
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        return 1
    conn.send(("ready", f"{device} {model_name} {compute}{'' if use_vad else ' novad'}"))
    print("[whisper-worker] ready", flush=True)
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return 0
        if not msg or msg[0] == "q":
            return 0
        if msg[0] == "a":
            _, audio, cands, lang = msg
            try:
                import forced_align
                conn.send(("ok", forced_align.align_candidates(model, audio, cands, lang or "en")))
            except Exception as exc:
                traceback.print_exc()
                conn.send(("error", f"{type(exc).__name__}: {exc}"))
            continue
        _, audio, kw = msg
        if not use_vad:
            kw = dict(kw, vad_filter=False)
            kw.pop("vad_parameters", None)
        try:
            segs, _ = model.transcribe(audio, **kw)
            out = []
            for seg in segs:
                for w in seg.words or []:
                    out.append((w.word or "", float(w.start or 0), float(w.end or 0),
                                float(getattr(w, "probability", .8) or .8)))
            conn.send(("ok", out))
        except Exception as exc:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class WhisperUnavailable(RuntimeError):
    pass


class WhisperSidecar:
    def __init__(self, cache_dir: Path):
        import threading
        self.cache = Path(cache_dir)
        self.lock = threading.Lock()
        self.proc = None
        self.conn = None
        self.plan = None
        self.idx = 0
        self.crashes = 0
        self.desc = "unloaded"
        self.cfg = None
        self.disabled = ""

    def _log_path(self):
        import os
        base = Path(os.environ.get("LF_DATA_DIR") or self.cache.parent) / "logs"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return str(base / "whisper.log")

    def _make_plan(self):
        import os
        ok, reason = cuda_whisper_usable(self.cache)
        print(f"[whisper] CUDA: {'yes' if ok else 'no'} ({reason})", flush=True)
        cpu_model = os.environ.get("LF_WHISPER_CPU_MODEL") or "small"
        plan = []
        if ok:
            plan.append(("turbo", "cuda", "float16", True))
        # VAD (onnxruntime) is dropped before giving up on Whisper entirely.
        plan += [(cpu_model, "cpu", "int8", True), (cpu_model, "cpu", "int8", False),
                 (cpu_model, "cpu", "float32", False)]
        return plan

    def _kill(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.conn = None
        self.proc = None

    def _spawn(self, cfg):
        import os, sys, socket, secrets, subprocess, time as _t
        from multiprocessing.connection import Client
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        key = secrets.token_bytes(16)
        env = dict(os.environ, LF_WHISPER_PORT=str(port), LF_WHISPER_KEY=key.hex(),
                   LF_WHISPER_CFG=json.dumps(cfg), LF_WHISPER_LOG=self._log_path())
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, WHISPER_WORKER_ARG]
        else:
            here = str(Path(__file__).resolve().parent)
            cmd = [sys.executable, "-c",
                   f"import sys;sys.path.insert(0,{here!r});import lyrics_engine as l;sys.exit(l.whisper_worker_main())"]
        print(f"[whisper] starting worker {cfg}", flush=True)
        self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        deadline = _t.time() + 120
        while True:
            if self.proc.poll() is not None:
                raise WhisperUnavailable(f"worker exited during start (code {self.proc.returncode})")
            try:
                self.conn = Client(("127.0.0.1", port), authkey=key)
                break
            except OSError:
                if _t.time() > deadline:
                    raise WhisperUnavailable("worker did not come up")
                _t.sleep(.25)
        # Model load may include a first-time download: wait while alive.
        while not self.conn.poll(1.0):
            if self.proc.poll() is not None:
                raise WhisperUnavailable(f"worker crashed while loading (code {self.proc.returncode})")
        kind, info = self.conn.recv()
        if kind != "ready":
            raise WhisperUnavailable(f"model load failed: {info}")
        self.desc = info
        self.cfg = cfg
        print(f"[whisper] ready: {info}", flush=True)

    def ensure(self):
        if self.disabled:
            raise WhisperUnavailable(self.disabled)
        if self.proc and self.proc.poll() is None and self.conn:
            return
        self._kill()
        if self.plan is None:
            self.plan = self._make_plan()
        while self.idx < len(self.plan):
            cfg = self.plan[self.idx]
            try:
                self._spawn(cfg)
                return
            except Exception as exc:
                print(f"[whisper] {cfg} unusable: {exc}", flush=True)
                self._kill()
                self.idx += 1
        self.disabled = "Whisper unavailable on this PC (see logs/whisper.log)"
        raise WhisperUnavailable(self.disabled)

    def align(self, audio, candidates, language="en"):
        return self._request(("a", np.asarray(audio, dtype=np.float32), candidates, language))

    def transcribe(self, audio, **kw):
        return self._request(("t", np.asarray(audio, dtype=np.float32), kw))

    def _request(self, msg):
        import time as _t
        with self.lock:
            self.ensure()
            try:
                self.conn.send(msg)
                deadline = _t.time() + 180
                while not self.conn.poll(.5):
                    if self.proc.poll() is not None or _t.time() > deadline:
                        raise WhisperUnavailable("worker died during transcription")
                kind, data = self.conn.recv()
            except (EOFError, OSError, WhisperUnavailable) as exc:
                self.crashes += 1
                print(f"[whisper] worker lost ({exc}); crash #{self.crashes} on {self.cfg}", flush=True)
                self._kill()
                if self.crashes >= 2:
                    self.crashes = 0
                    self.idx += 1
                raise WhisperUnavailable(str(exc))
            if kind != "ok":
                raise RuntimeError(data)
            self.crashes = 0
            return data

    def close(self):
        with self.lock:
            try:
                if self.conn:
                    self.conn.send(("q",))
            except Exception:
                pass
            self._kill()

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch in "'’").replace("’", "'")

def line_words(lines):
    out=[]
    for li,row in enumerate(lines or []):
        for wi,w in enumerate(WORD_RE.findall(row.get("text",""))):
            out.append({
                "line":li, "word":wi, "text":w, "norm":norm(w),
                "line_t":int(row.get("t",0))
            })
    return out

class AudioCapture:
    def __init__(self, sr=16000, seconds=20):
        self.sr=sr
        self.max=sr*seconds
        self.parts=deque()
        self.total=0
        self.thread=None
        self.stop=False
        self.error=""
        self.device_name=""
        self.last_perf=0.0   # perf_counter() when the newest sample arrived

    def start(self):
        import threading
        if self.thread and self.thread.is_alive(): return
        self.stop=False
        self.thread=threading.Thread(target=self._worker,daemon=True,name="LF-WASAPI")
        self.thread.start()

    def _worker(self):
        try:
            import soundcard as sc
            speaker=sc.default_speaker()
            sname=(getattr(speaker,"name","") or "").lower()
            ranked=[]
            for mic in sc.all_microphones(include_loopback=True):
                name=(getattr(mic,"name","") or "").lower()
                score=100 if getattr(mic,"isloopback",False) else 0
                if sname and (sname in name or name in sname): score+=60
                if "loopback" in name: score+=25
                ranked.append((score,mic))
            ranked.sort(key=lambda x:x[0],reverse=True)
            if not ranked: raise RuntimeError("No WASAPI loopback device")
            mic=ranked[0][1]
            self.device_name=getattr(mic,"name","loopback")
            # 40 ms capture blocks.
            with mic.recorder(samplerate=self.sr,channels=2,blocksize=640) as rec:
                while not self.stop:
                    b=rec.record(numframes=640)
                    if b.ndim==2:b=b.mean(axis=1)
                    self._append(np.asarray(b,dtype=np.float32))
        except Exception as e:
            self.error=f"{type(e).__name__}: {e}"

    def _append(self,a):
        self.parts.append(a.copy());self.total+=len(a)
        self.last_perf=time.perf_counter()
        while self.total>self.max and self.parts:
            h=self.parts[0];extra=self.total-self.max
            if extra>=len(h):
                self.parts.popleft();self.total-=len(h)
            else:
                self.parts[0]=h[extra:].copy();self.total-=extra;break

    def latest(self,seconds):
        if not self.parts:return np.zeros(0,dtype=np.float32)
        arr=np.concatenate(list(self.parts))
        return arr[-max(1,int(self.sr*seconds)):].copy()

    def latest_with_time(self,seconds):
        """Recent audio plus the perf_counter() instant of its last sample."""
        parts=list(self.parts);t=self.last_perf
        if not parts:return np.zeros(0,dtype=np.float32),t
        arr=np.concatenate(parts)
        return arr[-max(1,int(self.sr*seconds)):].copy(),t

    def clear(self):
        self.parts.clear();self.total=0

from sync_engine import ForcedSync


class LyricsTimingEngine(ForcedSync):
    """
    Aggressive low-latency forced aligner.

    Fast pass:
      ~2.4 s audio window, scheduled ~5 Hz.

    Deep pass:
      ~5.5 s context every few cycles to repair ambiguous fast-pass matches.

    Important:
      - only one decode runs at a time
      - requests are latest-only: no backlog can accumulate
      - matches need temporal/order validation
      - repeated overlapping Whisper observations are combined by consensus
      - seek keeps learned calibration and wordmap
    """
    def __init__(self,cache_dir:Path):
        self.cache=Path(cache_dir);self.cache.mkdir(parents=True,exist_ok=True)
        self.audio=AudioCapture();self.audio.start()

        self.model=None
        self.device="unloaded"
        self.compute="unloaded"
        self.model_name="unloaded"

        self.track_key=""
        self.lines=[]
        self.flat=[]
        self.wordmap={}
        self.provider="none"
        self.quality="none"
        self.status="idle"
        self.provider_offset_ms=0.0

        self.offset_ms=0.0
        self.speed=1.0

        # Candidate observations before commitment.
        # "line:word" -> deque[(start,end,confidence,observed_at)]
        self.obs=defaultdict(lambda:deque(maxlen=8))
        self.pass_no=0
        self.last_schedule_ms=-999999
        self.worker_lock=asyncio.Lock()

        # decoded window fingerprint to avoid redundant work on pause/stalls
        self.last_track_ms=-1
        self.last_added=0

    def _cache_path(self,key):
        safe=re.sub(r"[^a-zA-Z0-9._-]+","_",key)[:180]
        return self.cache/(safe+".wordmap.json")

    def _load_cache(self,key):
        try:
            d=json.loads(self._cache_path(key).read_text("utf-8"))
            wm=d.get("wordmap",{})
            if wm:return wm
        except Exception:pass
        return {}

    def _save(self):
        if not self.track_key or not self.wordmap:return
        try:
            self._cache_path(self.track_key).write_text(json.dumps({
                "version":2,"wordmap":self.wordmap,
                "offset_ms":self.offset_ms,"speed":self.speed,
                "saved_at":time.time()
            },ensure_ascii=False),"utf-8")
        except Exception:pass

    def begin_track(self,key,lines,provider="lrclib",wordmap=None):
        self.track_key=key
        self.lines=lines or []
        self.flat=line_words(self.lines)
        self.provider=provider
        self.provider_offset_ms=0.0
        self.obs.clear()
        self.audio.clear()
        self.pass_no=0
        self.last_schedule_ms=-999999
        self.last_track_ms=-1
        self.last_added=0

        supplied=dict(wordmap or {})
        cached=self._load_cache(key)

        if supplied:
            self.wordmap=supplied
            self.offset_ms=0.0
            self.speed=1.0
            self.quality="word_provider"
            self.status=f"AUTHORITATIVE WORD SYNC · {len(self.wordmap)}"
        elif cached:
            self.wordmap=cached
            self.quality="cached_word"
            self._fit_calibration_from_wordmap()
            self.status=f"CACHED REAL SYNC · {len(self.wordmap)}"
        else:
            self.wordmap={}
            self.offset_ms=0.0;self.speed=1.0
            self.quality="aggressive_live" if self.lines else "none"
            self.status="FORCED ALIGNER WARMUP" if self.lines else "NO LYRICS"
        self._fs_reset()

    def on_seek(self,track_ms=None):
        # CRITICAL: do NOT erase learned calibration or wordmap.
        self.audio.clear()
        self.obs.clear()
        self.last_schedule_ms=-999999
        self.last_track_ms=int(track_ms or -1)
        if self.wordmap and self.quality!="word_provider":
            self._fit_calibration_from_wordmap()
            self.status=f"SEEK REACQUIRE · KEEPING {len(self.wordmap)} WORDS"
        else:
            self.status="SEEK REACQUIRE · CALIBRATION KEPT"
        self._fs_on_seek()

    def _ensure_model(self):
        if self.model is None:
            self.model=WhisperSidecar(self.cache)
        self.model.ensure()
        cfg=self.model.cfg or ("unloaded","unloaded","unloaded")
        self.model_name,self.device,self.compute=cfg[:3]

    def _provisional_wordmap(self):
        out=[]
        for li,line in enumerate(self.lines):
            start=float(line.get("t",0))
            end=float(self.lines[li+1].get("t",start+4200)) if li+1<len(self.lines) else start+4200
            words=WORD_RE.findall(line.get("text",""))
            if not words:continue
            weights=[max(.8,min(3.2,len(norm(w))*.22+.65)) for w in words]
            total=sum(weights);acc=0.0;span=max(500.0,end-start)
            for wi,(w,wt) in enumerate(zip(words,weights)):
                rs=start+span*(acc/total);acc+=wt;re_=start+span*(acc/total)
                out.append({
                    "line":li,"word":wi,"text":w,"norm":norm(w),
                    "raw_start":rs,"raw_end":re_,
                    "start":self.offset_ms+self.speed*rs,
                    "end":self.offset_ms+self.speed*re_
                })
        return out

    def _expected_index(self,track_ms):
        prov=self._provisional_wordmap()
        if not prov:return 0,prov
        idx=min(range(len(prov)),key=lambda i:abs(prov[i]["start"]-track_ms))
        return idx,prov

    def _decode_sync(self,audio,chunk_start_ms,deep=False):
        self._ensure_model()
        # Fast pass uses minimal search. Deep pass increases beam slightly.
        beam=2 if deep else 1
        best=2 if deep else 1
        words=self.model.transcribe(
            audio,
            beam_size=beam,
            best_of=best,
            temperature=0.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms":90 if not deep else 140},
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        heard=[]
        for word,start,end,prob in words:
            text=(word or "").strip()
            n=norm(text)
            if not n:continue
            heard.append({
                "text":text,"norm":n,
                "start":chunk_start_ms+int(max(0,start)*1000),
                "end":chunk_start_ms+int(max(0,end)*1000),
                "prob":float(prob or .8)
            })
        return heard

    def _align(self,heard,track_ms,deep=False,radius_override=None):
        if not heard or not self.flat:return []

        expected,prov=self._expected_index(track_ms)
        radius=int(radius_override) if radius_override is not None else (95 if deep else 42)
        lo=max(0,expected-radius);hi=min(len(prov),expected+radius)
        cand=prov[lo:hi]
        if not cand:return []

        n,m=len(cand),len(heard)
        dp=[[0.0]*(m+1) for _ in range(n+1)]
        back=[[None]*(m+1) for _ in range(n+1)]

        for i in range(1,n+1):
            for j in range(1,m+1):
                sim=100 if cand[i-1]["norm"]==heard[j-1]["norm"] else ratio(cand[i-1]["norm"],heard[j-1]["norm"])
                # Reward timing proximity too. This prevents repeated phrases
                # elsewhere in the song stealing the current audio.
                time_dist=abs(cand[i-1]["start"]-heard[j-1]["start"])
                temporal=max(-2.0,1.2-(time_dist/1800.0))
                match=dp[i-1][j-1]+(sim-60)/11.0+temporal
                skipL=dp[i-1][j]-.72
                skipH=dp[i][j-1]-.58
                b=max(0.0,match,skipL,skipH)
                dp[i][j]=b
                if b==0:back[i][j]=None
                elif b==match:back[i][j]=(i-1,j-1,"m",sim,time_dist)
                elif b==skipL:back[i][j]=(i-1,j,"l",0,0)
                else:back[i][j]=(i,j-1,"h",0,0)

        bi,bj=max(((i,j) for i in range(n+1) for j in range(m+1)),key=lambda p:dp[p[0]][p[1]])
        pairs=[]
        i,j=bi,bj
        while i>0 and j>0 and back[i][j] is not None and dp[i][j]>0:
            pi,pj,k,sim,td=back[i][j]
            if k=="m" and sim>=74:
                pairs.append((lo+i-1,j-1,sim,td))
            i,j=pi,pj
        pairs.reverse()
        return pairs

    def _neighbor_bounds(self,line,word):
        # Return committed previous end and next start around this lyric word.
        prev_end=None;next_start=None
        for step in range(1,8):
            k=f"{line}:{word-step}"
            if k in self.wordmap:
                prev_end=float(self.wordmap[k]["end_ms"]);break
        for step in range(1,8):
            k=f"{line}:{word+step}"
            if k in self.wordmap:
                next_start=float(self.wordmap[k]["start_ms"]);break
        return prev_end,next_start

    def _observe_pairs(self,pairs,heard,prov,deep=False):
        accepted=0
        for global_i,hj,sim,time_dist in pairs:
            p=prov[global_i];h=heard[hj]
            conf=min(1.0,(sim/100.0)*max(.4,h["prob"]))

            # Strict temporal sanity around already-committed neighbors.
            prev_end,next_start=self._neighbor_bounds(p["line"],p["word"])
            hs=float(h["start"]);he=max(hs+40,float(h["end"]))
            if prev_end is not None and hs < prev_end-160:
                continue
            if next_start is not None and he > next_start+160:
                continue

            # Prevent one observed instant being assigned to many different
            # lyric words across overlapping chunks.
            collision=False
            for key,item in self.wordmap.items():
                if key==f'{p["line"]}:{p["word"]}':continue
                if abs(float(item["start_ms"])-hs)<90:
                    collision=True;break
            if collision:
                continue

            key=f'{p["line"]}:{p["word"]}'
            self.obs[key].append((hs,he,conf,time.time()))

            vals=list(self.obs[key])
            starts=[x[0] for x in vals]
            ends=[x[1] for x in vals]
            confs=[x[2] for x in vals]

            # Deep pass or repeated confirmations can commit.
            spread=(max(starts)-min(starts)) if len(starts)>1 else 999
            strong=max(confs)>=.93
            consensus=(len(vals)>=2 and spread<=160)
            if not (strong or consensus or (deep and max(confs)>=.84)):
                continue

            start=statistics.median(starts[-4:])
            end=statistics.median(ends[-4:])
            confidence=max(confs)

            old=self.wordmap.get(key)
            # Don't flap an already-stable timestamp for tiny noisy differences.
            if old and old.get("provider")=="local_alignment" and old.get("confidence",0)>=confidence and abs(old["start_ms"]-start)<140:
                continue

            self.wordmap[key]={
                "line":p["line"],"word":p["word"],"text":p["text"],
                "start_ms":int(start),"end_ms":int(max(start+40,end)),
                "confidence":round(confidence,3),
                "provider":"local_alignment"
            }
            accepted+=1

        if accepted:
            self._repair_monotonic_order()
            self._fit_calibration_from_wordmap()
            self._save()
        return accepted

    def _repair_monotonic_order(self):
        # Reject/repair impossible repeated timestamps inside each line.
        grouped=defaultdict(list)
        for key,item in self.wordmap.items():
            grouped[int(item["line"])].append((int(item["word"]),key,item))
        for li,rows in grouped.items():
            rows.sort()
            last_end=None
            bad=[]
            for wi,key,item in rows:
                s=int(item["start_ms"]);e=max(s+40,int(item["end_ms"]))
                if last_end is not None and s < last_end-80:
                    # The newer/less confident mapping is almost certainly an
                    # overlap hallucination; remove rather than duplicate words.
                    bad.append(key);continue
                item["end_ms"]=e
                last_end=e
            for key in bad:
                self.wordmap.pop(key,None)

    def _fit_calibration_from_wordmap(self):
        prov={(x["line"],x["word"]):x for x in self._provisional_wordmap()}
        pts=[]
        for item in self.wordmap.values():
            key=(int(item["line"]),int(item["word"]))
            # _provisional_wordmap already includes current correction, so derive
            # raw LRC word time directly instead.
            li,wi=key
            if li>=len(self.lines):continue
            words=WORD_RE.findall(self.lines[li]["text"])
            if wi>=len(words):continue
            start=float(self.lines[li]["t"])
            end=float(self.lines[li+1]["t"]) if li+1<len(self.lines) else start+4200
            weights=[max(.8,min(3.2,len(norm(w))*.22+.65)) for w in words]
            total=sum(weights)
            raw=start+(end-start)*(sum(weights[:wi])/total)
            pts.append((raw,float(item["start_ms"]),float(item.get("confidence",.8))))
        if len(pts)<2:return

        # Robustly trim extreme residuals from bad lyric/provider lines.
        preliminary=[y-x for x,y,w in pts]
        med=statistics.median(preliminary)
        trimmed=[p for p,r in zip(pts,preliminary) if abs(r-med)<2200]
        if len(trimmed)<2:return

        xs=np.array([x for x,y,w in trimmed],float)
        ys=np.array([y for x,y,w in trimmed],float)
        ws=np.array([max(.25,w) for x,y,w in trimmed],float)
        xm=np.average(xs,weights=ws);ym=np.average(ys,weights=ws)
        denom=np.sum(ws*(xs-xm)**2)
        slope=np.sum(ws*(xs-xm)*(ys-ym))/denom if denom>1 else 1.0
        slope=float(max(.965,min(1.035,slope)))
        intercept=float(ym-slope*xm)

        # Fast but damped adaptation.
        self.speed=self.speed*.70+slope*.30
        self.offset_ms=self.offset_ms*.62+intercept*.38



    async def force_provider_resync(self,track_ms,playing=True,ref_perf=None):
        """Manual Musixmatch calibration: global offset only, word spacing untouched."""
        if not (self.quality=="word_provider" and self.wordmap):
            return {"ok":False,"error":"not_authoritative","message":"Musixmatch/provider word timing is not active","timing":self.export()}
        if not playing:
            return {"ok":False,"error":"not_playing","message":"Playback is paused","timing":self.export()}
        try:
            return await self.forced_resync(track_ms,ref_perf if ref_perf is not None else time.perf_counter(),provider=True)
        except Exception as exc:
            self.status=f"MUSIXMATCH CALIBRATION FAILED · {type(exc).__name__}"
            return {"ok":False,"error":type(exc).__name__,"message":str(exc)[:180],"timing":self.export()}

    async def force_resync(self,track_ms,playing=True,ref_perf=None):
        """Manual emergency re-anchor: wide hypothesis search + hard lock."""
        if self.quality=="word_provider" and self.wordmap:
            self.status="AUTHORITATIVE PROVIDER · RESYNC NOT NEEDED"
            return {"ok":True,"authoritative":True,"message":"Musixmatch/provider word timing is authoritative","timing":self.export()}
        if not playing:
            return {"ok":False,"error":"not_playing","message":"Playback is paused","timing":self.export()}
        if not self.lines or not self.flat:
            return {"ok":False,"error":"no_lyrics","message":"No lyrics available","timing":self.export()}
        try:
            return await self.forced_resync(track_ms,ref_perf if ref_perf is not None else time.perf_counter())
        except Exception as exc:
            self.status=f"MANUAL RESYNC FAILED · {type(exc).__name__}"
            return {"ok":False,"error":type(exc).__name__,"message":str(exc)[:180],"timing":self.export()}

    async def update(self,track_ms,playing=True,ref_perf=None):
        if not playing or not self.lines:
            return
        try:
            await self.forced_update(track_ms,ref_perf if ref_perf is not None else time.perf_counter())
        except Exception as e:
            self.status=f"ALIGN RETRY · {type(e).__name__}"

    def export(self):
        return {
            "quality":self.quality,
            "provider":self.provider,
            "provider_offset_ms":round(self.provider_offset_ms,1),
            "authoritative":bool(self.quality=="word_provider" and self.wordmap),
            "status":self.status,
            "wordmap":self.wordmap,
            "real_words":len(self.wordmap),
            "total_words":len(self.flat),
            "offset_ms":round(self.offset_ms,1),
            "speed":round(self.speed,6),
            "device":self.device,
            "model":self.model_name,
            "compute":self.compute,
            "pass_no":self.pass_no,
            "last_added":self.last_added,
            "tt":self.token_times_all(),
            "lock_sigma_ms":round(float(self.kf.sigma),1) if hasattr(self,"kf") else None,
            "align_score":round(float(getattr(self,"last_score",0.0)),3),
            "language":getattr(self,"lang","en"),
            "ms_per_syllable":round(float(getattr(self,"ms_per_syl",240.0)),1),
            "audio_device":self.audio.device_name,
            "audio_error":self.audio.error,
        }
