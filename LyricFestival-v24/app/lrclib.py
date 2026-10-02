import asyncio, json, re
from pathlib import Path
import aiohttp

LRC_RE=re.compile(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s?(.*)$")

def parse_lrc(text):
    out=[]
    for raw in (text or "").splitlines():
        m=LRC_RE.match(raw.strip())
        if not m: continue
        frac=(m.group(3) or "0")+"000"
        t=int(m.group(1))*60000+int(m.group(2))*1000+int(frac[:3])
        txt=m.group(4).strip()
        if txt: out.append({"t":t,"text":txt})
    return sorted(out,key=lambda x:x["t"])

class LrcLib:
    def __init__(self,cache_dir:Path):
        self.cache_dir=cache_dir;self.cache_dir.mkdir(parents=True,exist_ok=True)
        self.headers={"User-Agent":"LyricFestival-Rewrite/1.0"}
    def _path(self,title,artist):
        safe=re.sub(r"[^a-zA-Z0-9._-]+","_",f"{artist}__{title}")[:170]
        return self.cache_dir/(safe+".json")
    async def get(self,title,artist,album="",duration_ms=0):
        path=self._path(title,artist)
        if path.exists():
            try:return json.loads(path.read_text("utf-8"))
            except Exception:pass
        params={"track_name":title,"artist_name":artist}
        if album:params["album_name"]=album
        if duration_ms:params["duration"]=int(round(duration_ms/1000))
        async with aiohttp.ClientSession(headers=self.headers,timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get("https://lrclib.net/api/get",params=params) as r:
                if r.status==200:
                    d=await r.json()
                    lines=parse_lrc(d.get("syncedLyrics") or "")
                    if lines:
                        out={"ok":True,"provider":"lrclib","quality":"line","lines":lines,"wordmap":{}}
                        path.write_text(json.dumps(out,ensure_ascii=False),"utf-8")
                        return out
            async with s.get("https://lrclib.net/api/search",params={"track_name":title,"artist_name":artist}) as r:
                if r.status==200:
                    items=await r.json()
                    for d in items:
                        lines=parse_lrc(d.get("syncedLyrics") or "")
                        if lines:
                            out={"ok":True,"provider":"lrclib","quality":"line","lines":lines,"wordmap":{}}
                            path.write_text(json.dumps(out,ensure_ascii=False),"utf-8")
                            return out
        return {"ok":False,"provider":"lrclib","quality":"none","lines":[],"wordmap":{}}
