import asyncio,json,re,random,string,time
from pathlib import Path
import aiohttp

WORD_RE=re.compile(r"[^\W_]+(?:['’][^\W_]+)?",re.UNICODE)

class Musixmatch:
    BASE="https://apic-desktop.musixmatch.com/ws/1.1"
    APP_ID="web-desktop-app-v1.0"
    def __init__(self,cache:Path):
        self.cache=cache;self.cache.mkdir(parents=True,exist_ok=True)
        self.token_file=self.cache/"token.json"
        self.last={}
    async def _get(self,s,ep,p):
        p=dict(p);p.setdefault("app_id",self.APP_ID);p.setdefault("t","".join(random.choice(string.ascii_lowercase+string.digits) for _ in range(8)))
        async with s.get(f"{self.BASE}/{ep}",params=p) as r:
            try:d=await r.json(content_type=None)
            except Exception:d={}
            d["_http"]=r.status;return d
    def _status(self,d):
        try:return int(d["message"]["header"]["status_code"])
        except:return 0
    def _body(self,d):
        try:return d["message"]["body"]
        except:return {}
    async def token(self,s):
        try:
            t=json.loads(self.token_file.read_text("utf-8")).get("token","")
            if t:return t
        except:pass
        d=await self._get(s,"token.get",{"user_language":"en"})
        if self._status(d)==200:
            t=self._body(d).get("user_token","")
            if t and not t.startswith("UpgradeOnly"):
                self.token_file.write_text(json.dumps({"token":t,"time":time.time()}),"utf-8");return t
        self.last={"token_status":self._status(d),"http":d.get("_http",0)}
        return ""
    def _rich(self,raw):
        try:items=json.loads(raw) if isinstance(raw,str) else raw
        except:return [],{}
        lines=[];wordmap={}
        def nw(s):return re.sub(r"[^a-z0-9]+","",s.lower())
        for item in items or []:
            ts=float(item.get("ts",0) or 0);te=float(item.get("te",ts+4) or ts+4);chunks=item.get("l") or []
            text=(item.get("x") or "").strip() or "".join(str(c.get("c","")) for c in chunks).strip()
            if not text:continue
            li=len(lines);lines.append({"t":int(ts*1000),"text":text})
            toks=WORD_RE.findall(text);timed=[]
            for ci,ch in enumerate(chunks):
                ct=WORD_RE.findall(str(ch.get("c","")))
                start=ts+float(ch.get("o",0) or 0)
                end=te if ci+1>=len(chunks) else ts+float(chunks[ci+1].get("o",0) or 0)
                end=max(start+.04,end)
                if ct:
                    step=(end-start)/len(ct)
                    for ti,t in enumerate(ct):timed.append((t,start+ti*step,start+(ti+1)*step))
            cur=0
            for wi,w in enumerate(toks):
                hit=None
                for j in range(cur,min(cur+5,len(timed))):
                    if nw(timed[j][0])==nw(w):hit=j;break
                if hit is None and cur<len(timed):hit=cur
                if hit is None:continue
                t,s,e=timed[hit];cur=hit+1
                wordmap[f"{li}:{wi}"]={"line":li,"word":wi,"text":w,"start_ms":int(s*1000),"end_ms":int(e*1000),"confidence":1.0,"provider":"musixmatch"}
        return lines,wordmap
    async def get(self,title,artist,album="",duration_ms=0):
        async with aiohttp.ClientSession(headers={"User-Agent":"Mozilla/5.0"},timeout=aiohttp.ClientTimeout(total=8)) as s:
            tok=await self.token(s)
            if not tok:return None
            p={"format":"json","namespace":"lyrics_richsynched","subtitle_format":"lrc","q_artist":artist,"q_track":title,"q_album":album or "","usertoken":tok}
            d=await self._get(s,"macro.subtitles.get",p)
            self.last={"macro_status":self._status(d),"macro_http":d.get("_http",0)}
            if self._status(d)!=200:return None
            try:m=self._body(d)["macro_calls"]
            except:return None
            track={}
            try:
                c=m["matcher.track.get"]
                if int(c["message"]["header"]["status_code"])==200:track=c["message"]["body"].get("track",{})
            except:pass
            if not track:return None
            cid=track.get("commontrack_id")
            if not cid:return None
            rd=await self._get(s,"track.richsync.get",{"commontrack_id":cid,"usertoken":tok})
            self.last.update({"rich_status":self._status(rd),"rich_http":rd.get("_http",0)})
            if self._status(rd)==200:
                try:raw=self._body(rd)["richsync"]["richsync_body"]
                except:raw=""
                if raw:
                    lines,wm=self._rich(raw)
                    if lines and wm:
                        self.last["result"]="richsync"
                        return {"ok":True,"provider":"musixmatch","quality":"word","lines":lines,"wordmap":wm}
            return None
