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

    def clear(self):
        self.parts.clear();self.total=0

class LyricsTimingEngine:
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
            self.status="GPU ALIGNER WARMUP" if self.lines else "NO LYRICS"

    def on_seek(self,track_ms=None):
        # CRITICAL: do NOT erase learned calibration or wordmap.
        self.audio.clear()
        self.obs.clear()
        self.last_schedule_ms=-999999
        self.last_track_ms=int(track_ms or -1)
        if self.wordmap:
            self._fit_calibration_from_wordmap()
            self.status=f"SEEK REACQUIRE · KEEPING {len(self.wordmap)} WORDS"
        else:
            self.status="SEEK REACQUIRE · CALIBRATION KEPT"

    def _ensure_model(self):
        if self.model is not None:return
        from faster_whisper import WhisperModel

        # GPU path intentionally uses turbo. The user asked to prioritize
        # speed/accuracy and has hardware headroom.
        try:
            self.model=WhisperModel("turbo",device="cuda",compute_type="float16")
            self.device="cuda";self.compute="float16";self.model_name="turbo"
        except Exception:
            try:
                self.model=WhisperModel("small",device="cpu",compute_type="int8")
                self.device="cpu";self.compute="int8";self.model_name="small"
            except Exception:
                raise

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
        segs,_=self.model.transcribe(
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
        for seg in segs:
            for w in seg.words or []:
                text=(w.word or "").strip()
                n=norm(text)
                if not n:continue
                heard.append({
                    "text":text,"norm":n,
                    "start":chunk_start_ms+int(max(0,w.start)*1000),
                    "end":chunk_start_ms+int(max(0,w.end)*1000),
                    "prob":float(getattr(w,"probability",.8) or .8)
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



    async def force_provider_resync(self,track_ms,playing=True):
        """
        Manual Musixmatch/provider calibration.

        Original provider word timestamps are NEVER rewritten.

        Whisper listens to the recent audio, matches recognized words against
        the authoritative provider wordmap, and estimates a robust GLOBAL
        offset:

            actual_audio_time ~= provider_word_time + provider_offset_ms

        This is intentionally offset-only. RichSync's internal word spacing
        remains exactly as supplied by the provider.
        """
        if not (self.quality=="word_provider" and self.wordmap):
            return {
                "ok":False,
                "error":"not_authoritative",
                "message":"Musixmatch/provider word timing is not active",
                "timing":self.export(),
            }

        if not playing:
            return {
                "ok":False,
                "error":"not_playing",
                "message":"Playback is paused",
                "timing":self.export(),
            }

        async with self.worker_lock:
            self.status="MUSIXMATCH CALIBRATION · LISTENING…"

            seconds=8.0
            audio=self.audio.latest(seconds)
            if len(audio)<self.audio.sr*2.2:
                return {
                    "ok":False,
                    "error":"not_enough_audio",
                    "message":"Need a couple seconds of recent audio",
                    "timing":self.export(),
                }

            chunk_ms=int(len(audio)/self.audio.sr*1000)
            chunk_start=max(0,int(track_ms)-chunk_ms)

            try:
                loop=asyncio.get_running_loop()
                heard=await loop.run_in_executor(None,self._decode_sync,audio,chunk_start,True)
                if len(heard)<2:
                    self.status="MUSIXMATCH CALIBRATION · NO VOCALS"
                    return {
                        "ok":False,
                        "error":"no_vocals",
                        "heard":len(heard),
                        "timing":self.export(),
                    }

                provider_words=[]
                for item in self.wordmap.values():
                    n=norm(item.get("text",""))
                    if not n:continue
                    provider_words.append({
                        "line":int(item.get("line",0)),
                        "word":int(item.get("word",0)),
                        "text":item.get("text",""),
                        "norm":n,
                        "start":float(item.get("start_ms",0))+float(self.provider_offset_ms),
                        "raw_start":float(item.get("start_ms",0)),
                        "end":float(item.get("end_ms",0))+float(self.provider_offset_ms),
                    })
                provider_words.sort(key=lambda x:x["raw_start"])

                # Search only a broad neighborhood around the current track time.
                near=[
                    x for x in provider_words
                    if abs((x["raw_start"]+self.provider_offset_ms)-track_ms) <= 30000
                ]
                if len(near)<2:
                    near=provider_words
                if len(near)<2:
                    return {"ok":False,"error":"no_provider_words","timing":self.export()}

                n,m=len(near),len(heard)
                dp=[[0.0]*(m+1) for _ in range(n+1)]
                back=[[None]*(m+1) for _ in range(n+1)]

                for i in range(1,n+1):
                    for j in range(1,m+1):
                        sim=100 if near[i-1]["norm"]==heard[j-1]["norm"] else ratio(near[i-1]["norm"],heard[j-1]["norm"])
                        # Timing is a weak prior only. We explicitly allow a
                        # substantial provider offset because that is what this
                        # button is designed to recover.
                        td=abs(near[i-1]["start"]-heard[j-1]["start"])
                        temporal=max(-1.5,0.8-(td/2600.0))
                        match=dp[i-1][j-1]+(sim-58)/10.5+temporal
                        skip_p=dp[i-1][j]-.65
                        skip_h=dp[i][j-1]-.55
                        b=max(0.0,match,skip_p,skip_h)
                        dp[i][j]=b
                        if b==0:back[i][j]=None
                        elif b==match:back[i][j]=(i-1,j-1,"m",sim)
                        elif b==skip_p:back[i][j]=(i-1,j,"p",0)
                        else:back[i][j]=(i,j-1,"h",0)

                bi,bj=max(
                    ((i,j) for i in range(n+1) for j in range(m+1)),
                    key=lambda p:dp[p[0]][p[1]]
                )

                pairs=[]
                i,j=bi,bj
                while i>0 and j>0 and back[i][j] is not None and dp[i][j]>0:
                    pi,pj,k,sim=back[i][j]
                    if k=="m" and sim>=78:
                        pairs.append((i-1,j-1,sim))
                    i,j=pi,pj
                pairs.reverse()

                offsets=[]
                weighted=[]
                for pi,hj,sim in pairs:
                    p=near[pi];h=heard[hj]
                    prob=max(.45,float(h.get("prob",.8)))
                    conf=(sim/100.0)*prob
                    if conf<.68:continue
                    # Actual heard position minus ORIGINAL provider timestamp.
                    delta=float(h["start"])-float(p["raw_start"])
                    offsets.append(delta)
                    weighted.append((delta,conf))

                if len(weighted)<2:
                    self.status=f"MUSIXMATCH CALIBRATION · WEAK MATCH ({len(weighted)})"
                    return {
                        "ok":False,
                        "error":"weak_match",
                        "matches":len(weighted),
                        "heard":len(heard),
                        "timing":self.export(),
                    }

                vals=np.array([x for x,w in weighted],dtype=float)
                ws=np.array([w for x,w in weighted],dtype=float)

                med=float(np.median(vals))
                # Remove repeated-phrase outliers.
                keep=np.abs(vals-med)<900
                if int(np.sum(keep))>=2:
                    vals=vals[keep];ws=ws[keep]

                new_offset=float(np.average(vals,weights=ws))
                # Safety clamp: a provider correction larger than 4s is almost
                # certainly a bad phrase match.
                new_offset=float(max(-4000,min(4000,new_offset)))

                old=float(self.provider_offset_ms)
                self.provider_offset_ms=new_offset
                change=self.provider_offset_ms-old

                self.status=f"MUSIXMATCH LOCKED · {len(vals)} MATCHES · OFFSET {self.provider_offset_ms:+.0f}ms"

                return {
                    "ok":True,
                    "authoritative":True,
                    "matches":int(len(vals)),
                    "old_provider_offset_ms":round(old,1),
                    "provider_offset_ms":round(self.provider_offset_ms,1),
                    "offset_change_ms":round(change,1),
                    "message":"MUSIXMATCH LOCKED",
                    "timing":self.export(),
                }

            except Exception as exc:
                self.status=f"MUSIXMATCH CALIBRATION FAILED · {type(exc).__name__}"
                return {
                    "ok":False,
                    "error":type(exc).__name__,
                    "message":str(exc)[:180],
                    "timing":self.export(),
                }

    async def force_resync(self,track_ms,playing=True):
        """
        Manual emergency re-anchor.

        This is deliberately stronger than the background aligner:
        - ~8 seconds of recent WASAPI loopback
        - deep Whisper pass
        - much wider lyric search radius
        - direct robust affine fit from matched lyric words -> heard timestamps
        - clears stale local word timings only around the current region
        - commits strong current matches immediately

        Musixmatch/other authoritative word providers are never modified.
        """
        if self.quality=="word_provider" and self.wordmap:
            self.status="AUTHORITATIVE PROVIDER · RESYNC NOT NEEDED"
            return {
                "ok":True,
                "authoritative":True,
                "message":"Musixmatch/provider word timing is authoritative",
                "timing":self.export(),
            }

        if not playing:
            return {"ok":False,"error":"not_playing","message":"Playback is paused","timing":self.export()}
        if not self.lines or not self.flat:
            return {"ok":False,"error":"no_lyrics","message":"No lyrics available","timing":self.export()}

        async with self.worker_lock:
            self.status="MANUAL RESYNC · LISTENING…"
            seconds=8.0
            audio=self.audio.latest(seconds)
            if len(audio)<self.audio.sr*2.2:
                return {
                    "ok":False,
                    "error":"not_enough_audio",
                    "message":"Need a couple seconds of recent audio",
                    "timing":self.export(),
                }

            chunk_ms=int(len(audio)/self.audio.sr*1000)
            chunk_start=max(0,int(track_ms)-chunk_ms)

            try:
                loop=asyncio.get_running_loop()
                heard=await loop.run_in_executor(None,self._decode_sync,audio,chunk_start,True)
                if len(heard)<2:
                    self.status="MANUAL RESYNC · NO VOCALS FOUND"
                    return {"ok":False,"error":"no_vocals","heard":len(heard),"timing":self.export()}

                expected,prov=self._expected_index(track_ms)
                # ~260 lyric words is intentionally huge for a manual rescue.
                pairs=self._align(heard,track_ms,True,radius_override=260)
                strong=[p for p in pairs if p[2]>=78]
                if len(strong)<2:
                    self.status=f"MANUAL RESYNC · WEAK MATCH ({len(strong)})"
                    return {
                        "ok":False,
                        "error":"weak_match",
                        "matches":len(strong),
                        "heard":len(heard),
                        "timing":self.export(),
                    }

                # Robust fit raw lyric timing X -> actual heard timing Y.
                points=[]
                matched_indices=[]
                for global_i,hj,sim,time_dist in strong:
                    if global_i>=len(prov) or hj>=len(heard):continue
                    p=prov[global_i];h=heard[hj]
                    conf=min(1.0,(sim/100.0)*max(.45,float(h.get("prob",.8))))
                    if conf<.68:continue
                    points.append((float(p["raw_start"]),float(h["start"]),conf))
                    matched_indices.append((global_i,hj,sim))

                if len(points)<2:
                    self.status="MANUAL RESYNC · INSUFFICIENT MATCH"
                    return {"ok":False,"error":"insufficient_match","timing":self.export()}

                xs=np.array([x for x,y,w in points],dtype=float)
                ys=np.array([y for x,y,w in points],dtype=float)
                ws=np.array([w for x,y,w in points],dtype=float)

                # Median-offset first for robustness.
                offsets=ys-xs
                med=float(np.median(offsets))
                keep=np.abs(offsets-med)<1800
                if int(np.sum(keep))>=2:
                    xs=xs[keep];ys=ys[keep];ws=ws[keep]

                xm=np.average(xs,weights=ws);ym=np.average(ys,weights=ws)
                denom=np.sum(ws*(xs-xm)**2)
                slope=np.sum(ws*(xs-xm)*(ys-ym))/denom if denom>1 else 1.0
                slope=float(max(.965,min(1.035,slope)))
                intercept=float(ym-slope*xm)

                old_offset=float(self.offset_ms)
                old_speed=float(self.speed)

                # MANUAL means decisive: use the new fit nearly directly.
                self.speed=slope
                self.offset_ms=intercept

                # Remove stale local/cached exact timings around "now"; otherwise
                # preciseAt() could keep preferring old exact timestamps over the
                # freshly corrected LRCLIB affine clock.
                purge_from=float(track_ms)-14000
                purge_to=float(track_ms)+14000
                stale=[]
                for key,item in self.wordmap.items():
                    if item.get("provider")=="musixmatch":
                        continue
                    s=float(item.get("start_ms",0))
                    if purge_from<=s<=purge_to:
                        stale.append(key)
                for key in stale:
                    self.wordmap.pop(key,None)

                # Immediately commit current strong matched words.
                committed=0
                for global_i,hj,sim in matched_indices:
                    if global_i>=len(prov) or hj>=len(heard):continue
                    p=prov[global_i];h=heard[hj]
                    conf=min(1.0,(sim/100.0)*max(.45,float(h.get("prob",.8))))
                    if conf<.72:continue
                    hs=int(h["start"]);he=max(hs+45,int(h["end"]))
                    key=f'{p["line"]}:{p["word"]}'
                    self.wordmap[key]={
                        "line":p["line"],"word":p["word"],"text":p["text"],
                        "start_ms":hs,"end_ms":he,
                        "confidence":round(conf,3),
                        "provider":"manual_resync",
                    }
                    committed+=1

                self._repair_monotonic_order()
                self._save()
                self.obs.clear()
                self.last_schedule_ms=int(track_ms)
                self.pass_no+=1
                self.last_added=committed
                delta=round(self.offset_ms-old_offset,1)
                self.status=f"MANUAL RESYNC LOCKED · {len(points)} MATCHES · Δ{delta:+.0f}ms"

                return {
                    "ok":True,
                    "authoritative":False,
                    "matches":len(points),
                    "committed":committed,
                    "old_offset_ms":round(old_offset,1),
                    "new_offset_ms":round(self.offset_ms,1),
                    "offset_change_ms":delta,
                    "old_speed":round(old_speed,6),
                    "new_speed":round(self.speed,6),
                    "message":"LOCKED",
                    "timing":self.export(),
                }
            except Exception as exc:
                self.status=f"MANUAL RESYNC FAILED · {type(exc).__name__}"
                return {
                    "ok":False,
                    "error":type(exc).__name__,
                    "message":str(exc)[:180],
                    "timing":self.export(),
                }

    async def update(self,track_ms,playing=True):
        if not playing or not self.lines or self.quality=="word_provider":
            return

        # Schedule at ~5 Hz, latest-only.
        if track_ms-self.last_schedule_ms<190:return
        if self.worker_lock.locked():
            # Don't queue. The next call will use fresher audio.
            return

        async with self.worker_lock:
            self.last_schedule_ms=track_ms
            self.pass_no+=1
            deep=(self.pass_no%5==0)
            seconds=5.5 if deep else 2.4
            audio=self.audio.latest(seconds)
            minimum=.9 if not deep else 2.0
            if len(audio)<self.audio.sr*minimum:return

            chunk_ms=int(len(audio)/self.audio.sr*1000)
            chunk_start=max(0,int(track_ms)-chunk_ms)
            try:
                loop=asyncio.get_running_loop()
                heard=await loop.run_in_executor(None,self._decode_sync,audio,chunk_start,deep)
                expected,prov=self._expected_index(track_ms)
                pairs=self._align(heard,track_ms,deep)
                added=self._observe_pairs(pairs,heard,prov,deep)
                self.last_added=added
                mode="DEEP" if deep else "FAST"
                self.status=f"{self.device.upper()} {self.model_name.upper()} · {mode} #{self.pass_no} · REAL {len(self.wordmap)}/{len(self.flat)}"
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
            "audio_device":self.audio.device_name,
            "audio_error":self.audio.error,
        }
