import asyncio,json,time,webbrowser,socket,io,ipaddress,subprocess,os
import vcrt_preload  # noqa: F401  (must precede native imports)
from pathlib import Path
from aiohttp import web,WSMsgType
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf
import qrcode
import psutil
from media_oracle import MediaOracle
from lrclib import LrcLib
from musixmatch import Musixmatch
from lyrics_engine import LyricsTimingEngine
from beat_oracle import BeatOracle
from private_audio import PrivateAudioWebRTC
from cover_art import CoverArt

BASE=Path(__file__).resolve().parent
STATIC=Path(os.environ.get("LF_STATIC_DIR") or BASE/"static")
DATA=Path(os.environ.get("LF_DATA_DIR") or BASE)
EMBEDDED=bool(os.environ.get("LF_EMBEDDED"))
import warnings
warnings.filterwarnings("ignore",message="data discontinuity in recording")
try:
    import faulthandler,sys
    (DATA/"logs").mkdir(parents=True,exist_ok=True)
    _crash_fh=open(DATA/"logs"/"crash.log","a",encoding="utf-8")
    faulthandler.enable(file=_crash_fh,all_threads=True)
    sys.stdout.reconfigure(line_buffering=True);sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
oracle=MediaOracle()
lrclib=LrcLib(DATA/"cache")
mxm=Musixmatch(DATA/"mxm_cache")
timing=LyricsTimingEngine(DATA/"timing_cache")
beat_oracle=BeatOracle(timing.audio)
private_audio=PrivateAudioWebRTC()
covers=CoverArt(DATA/"cover_cache")

state={"media":{},"lyrics":{"ok":False,"provider":"none","quality":"none","lines":[],"wordmap":{}},"timing":{},"beat":{},"track_key":"","generation":0,"loading":False}
clients=set()
lyrics_task=None
bridge_task=None
align_task=None

mdns=None
mdns_info=None
LAN_IP="127.0.0.1"
MDNS_HOST="lyricfestival.local"
PORT=8765

def _private_score(ip):
    try:
        addr=ipaddress.ip_address(ip)
    except Exception:
        return -999

    if addr.is_loopback or addr.is_link_local or not isinstance(addr,ipaddress.IPv4Address):
        return -999

    score=0
    if addr.is_private:
        score+=100
    # Prefer common home/LAN RFC1918 ranges.
    if ip.startswith("192.168."):
        score+=40
    elif ip.startswith("10."):
        score+=35
    elif ip.startswith("172."):
        try:
            second=int(ip.split(".")[1])
            if 16<=second<=31: score+=30
        except Exception:
            pass
    return score

def detect_lan_ip():
    """
    Prefer the IPv4 interface that Windows actually uses for its default route.
    This avoids selecting host-only adapters such as VirtualBox 192.168.56.x.

    Order:
      1. PowerShell Get-NetRoute/Get-NetIPAddress default-route interface
      2. psutil active private adapters
      3. hostname addresses
      4. loopback
    """
    # Windows authoritative route/interface selection.
    if os.name=="nt":
        ps_script=r"""
$routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
  Where-Object { $_.State -eq 'Alive' } |
  Sort-Object RouteMetric, InterfaceMetric
foreach ($r in $routes) {
  $ip = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue |
    Where-Object {
      $_.IPAddress -notlike '127.*' -and
      $_.IPAddress -notlike '169.254.*'
    } |
    Select-Object -First 1
  if ($ip) {
    $alias = (Get-NetAdapter -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue).Name
    Write-Output ($ip.IPAddress + '|' + $alias)
    break
  }
}
"""
        try:
            cp=subprocess.run(
                ["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-Command",ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0),
            )
            if cp.returncode==0:
                first=(cp.stdout or "").strip().splitlines()
                if first:
                    parts=first[0].strip().split("|",1)
                    ip=parts[0].strip()
                    alias=parts[1].strip() if len(parts)>1 else "default route"
                    if _private_score(ip)>0:
                        print(f"[LAN] default route adapter: {alias} -> {ip}")
                        return ip
        except Exception as exc:
            print(f"[LAN] default route probe warning: {type(exc).__name__}: {exc}")

    # Fallback enumeration.
    candidates=[]
    try:
        addrs=psutil.net_if_addrs()
        stats=psutil.net_if_stats()
        for ifname,entries in addrs.items():
            st=stats.get(ifname)
            if st is not None and not st.isup:
                continue
            lname=ifname.lower()
            adapter_penalty=0
            if any(x in lname for x in (
                "loopback","docker","vmware","virtualbox","vethernet",
                "hyper-v","tailscale","zerotier","wireguard","wsl",
                "host-only","nat network"
            )):
                adapter_penalty=100
            # Common generic names can still be virtual; penalize suspicious
            # RFC1918 host-only ranges below.
            for e in entries:
                if e.family!=socket.AF_INET:
                    continue
                ip=e.address
                score=_private_score(ip)-adapter_penalty

                # 192.168.56.0/24 is the classic VirtualBox host-only default.
                if ip.startswith("192.168.56."):
                    score-=120

                if score>0:
                    candidates.append((score,ip,ifname))
    except Exception as exc:
        print(f"[LAN] psutil enumeration warning: {type(exc).__name__}: {exc}")

    if candidates:
        candidates.sort(reverse=True)
        score,ip,ifname=candidates[0]
        print(f"[LAN] fallback adapter: {ifname} -> {ip}")
        return ip

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _private_score(ip)>0 and not ip.startswith("192.168.56."):
                print(f"[LAN] hostname fallback -> {ip}")
                return ip
    except Exception:
        pass

    print("[LAN] no private IPv4 interface found; using loopback only")
    return "127.0.0.1"

async def register_mdns():
    global mdns,mdns_info,LAN_IP
    LAN_IP=detect_lan_ip()
    if LAN_IP.startswith("127."):
        print("[mDNS] skipped because no LAN IPv4 address was found")
        return

    try:
        mdns=AsyncZeroconf()
        mdns_info=ServiceInfo(
            "_http._tcp.local.",
            "LyricFestival._http._tcp.local.",
            addresses=[socket.inet_aton(LAN_IP)],
            port=PORT,
            properties={
                b"path":b"/",
                b"name":b"LyricFestival",
                b"rave":b"true",
            },
            server="lyricfestival.local.",
        )
        await mdns.async_register_service(mdns_info,allow_name_change=False)
        print(f"[mDNS] advertised {MDNS_HOST} -> {LAN_IP}:{PORT}")
    except Exception as exc:
        print(f"[mDNS] warning: {type(exc).__name__}: {exc}")
        try:
            if mdns:
                await mdns.async_close()
        except Exception:
            pass
        mdns=None
        mdns_info=None

async def unregister_mdns():
    global mdns,mdns_info
    try:
        if mdns and mdns_info:
            await mdns.async_unregister_service(mdns_info)
    except Exception:
        pass
    try:
        if mdns:
            await mdns.async_close()
    except Exception:
        pass
    mdns=None
    mdns_info=None

def join_urls():
    local=f"http://{MDNS_HOST}:{PORT}/"
    lan=f"http://{LAN_IP}:{PORT}/"
    return local,lan

async def broadcast(o):
    text=json.dumps(o,ensure_ascii=False)
    dead=[]
    for ws in list(clients):
        try:await ws.send_str(text)
        except:dead.append(ws)
    for ws in dead:clients.discard(ws)

async def load_lyrics(gen,key,m):
    result=None
    try:result=await mxm.get(m.get("title",""),m.get("artist",""),m.get("album",""),m.get("duration_ms",0))
    except asyncio.CancelledError:return
    except:result=None
    if not result:
        try:result=await lrclib.get(m.get("title",""),m.get("artist",""),m.get("album",""),m.get("duration_ms",0))
        except asyncio.CancelledError:return
        except:result={"ok":False,"provider":"none","quality":"none","lines":[],"wordmap":{}}
    if gen!=state["generation"] or key!=state["track_key"]:return
    state["lyrics"]=result
    state["loading"]=False
    timing.begin_track(key,result.get("lines",[]),result.get("provider","none"),result.get("wordmap",{}))
    state["timing"]=timing.export()
    await broadcast({"type":"lyrics","generation":gen,"track_key":key,"lyrics":result,"timing":state["timing"]})

async def switch_track(m):
    global lyrics_task
    key=f'{m.get("artist","")}|{m.get("title","")}|{m.get("album","")}'
    if key==state["track_key"]:return
    state["track_key"]=key;state["generation"]+=1;gen=state["generation"];state["loading"]=True
    state["lyrics"]={"ok":False,"provider":"none","quality":"loading","lines":[],"wordmap":{}}
    state["timing"]={}
    if lyrics_task and not lyrics_task.done():lyrics_task.cancel()
    await broadcast({"type":"lyrics_reset","generation":gen,"track_key":key})
    lyrics_task=asyncio.create_task(load_lyrics(gen,key,dict(m)))
    asyncio.create_task(covers.get(m.get("artist",""),m.get("title",""),m.get("album","")))

async def oracle_bridge():
    while True:
        ev=await oracle.next_event()
        state["media"]=ev
        if ev.get("event")=="track_changed" or not state["track_key"]:
            if ev.get("title") and ev.get("artist"):await switch_track(ev)
        elif ev.get("event")=="seek":
            timing.on_seek(ev.get('position_ms'))
            state["timing"]=timing.export()
            await broadcast({"type":"timing_reset","timing":state["timing"]})
        await broadcast({"type":"media","media":ev})

def _wordmap_sig(wm):
    try:
        return (len(wm),int(sum(float(v.get("start_ms",0)) for v in wm.values())),
                int(sum(float(v.get("end_ms",0)) for v in wm.values())))
    except Exception:
        return (len(wm),0,0)

async def align_loop():
    # Delta timing push: only when something changed, at most ~4 Hz, and the
    # (potentially large) wordmap is only re-sent when it actually changed.
    last_push=0.0
    last_sig=None
    last_wm=None
    while True:
        m=oracle.snapshot()
        if state["lyrics"].get("ok") and m.get("playing"):
            p=int(m.get("position_ms",0))
            sp=float(m.get("sampled_perf_ms",0))
            if sp:p+=int(max(0,time.perf_counter()*1000-sp))
            await timing.update(p,True,ref_perf=time.perf_counter())
            t=timing.export()
            state["timing"]=t
            now=time.perf_counter()
            if now-last_push>=.25:
                wm_sig=_wordmap_sig(t.get("wordmap") or {})
                sig=(t.get("status"),t.get("offset_ms"),t.get("speed"),t.get("provider_offset_ms"),t.get("quality"),wm_sig,id(t.get("tt")))
                if sig!=last_sig:
                    last_push=now;last_sig=sig
                    if wm_sig==last_wm:
                        payload={k:v for k,v in t.items() if k!="wordmap"}
                        payload["wordmap_same"]=True
                    else:
                        last_wm=wm_sig;payload=t
                    await broadcast({"type":"timing","timing":payload})
        else:
            last_sig=None;last_wm=None
        await asyncio.sleep(.025)


async def beat_loop():
    last_state_push=0.0
    while True:
        m=oracle.snapshot()
        p=float(m.get("position_ms",0) or 0)
        sp=float(m.get("sampled_perf_ms",0) or 0)
        if sp and m.get("playing"):
            p+=max(0.0,time.perf_counter()*1000-sp)

        events=beat_oracle.update(
            p,
            bool(m.get("playing")),
            m.get("track_epoch"),
        )

        state["beat"]=beat_oracle.state.export()

        for ev in events:
            await broadcast({"type":"beat","beat":ev})

        now=time.perf_counter()
        if now-last_state_push>=0.35:
            last_state_push=now
            await broadcast({"type":"beat_state","beat":state["beat"]})

        await asyncio.sleep(.016)

async def ws_handler(req):
    ws=web.WebSocketResponse(heartbeat=20);await ws.prepare(req);clients.add(ws)
    await ws.send_json({"type":"snapshot","media":oracle.snapshot(),"lyrics":state["lyrics"],"timing":state["timing"],"beat":state["beat"],"generation":state["generation"],"track_key":state["track_key"]})
    try:
        async for msg in ws:
            if msg.type==WSMsgType.TEXT and msg.data=="ping":
                await ws.send_str('{"type":"pong"}')
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        clients.discard(ws)
        try:
            await ws.close()
        except Exception:
            pass
    return ws


async def webrtc_offer(req):
    try:
        d=await req.json()
        sdp=str(d.get("sdp",""))
        typ=str(d.get("type","offer"))
        if not sdp:
            return web.json_response({"ok":False,"error":"missing_sdp"},status=400)
        answer=await private_audio.offer(sdp,typ)
        return web.json_response({"ok":True,**answer})
    except Exception as exc:
        return web.json_response({
            "ok":False,
            "error":type(exc).__name__,
            "message":str(exc)[:240],
        },status=500)

async def private_audio_status(req):
    return web.json_response(private_audio.status())

async def index(req):return web.FileResponse(STATIC/"index.html")
async def debug(req):return web.json_response({"state":state,"musixmatch":mxm.last,"beat":beat_oracle.state.export(),"private_audio":private_audio.status()})

async def join_info(req):
    local,lan=join_urls()
    return web.json_response({
        "hostname":MDNS_HOST,
        "port":PORT,
        "mdns_url":local,
        "lan_url":lan,
        "lan_ip":LAN_IP,
    })





async def musixmatch_resync(req):
    m=oracle.snapshot()
    if not state["lyrics"].get("ok"):
        return web.json_response(
            {"ok":False,"error":"no_lyrics","message":"No lyrics loaded"},
            status=409
        )

    p=int(m.get("position_ms",0))
    sp=float(m.get("sampled_perf_ms",0))
    if sp and m.get("playing"):
        p+=int(max(0,time.perf_counter()*1000-sp))
    ref_perf=time.perf_counter()

    await broadcast({
        "type":"mxm_resync",
        "phase":"scanning",
        "position_ms":p,
    })

    result=await timing.force_provider_resync(p,bool(m.get("playing")),ref_perf=ref_perf)
    state["timing"]=timing.export()

    await broadcast({"type":"timing","timing":state["timing"]})
    await broadcast({
        "type":"mxm_resync",
        "phase":"locked" if result.get("ok") else "failed",
        "result":result,
    })

    return web.json_response(
        result,
        status=200 if result.get("ok") else 409
    )

async def manual_resync(req):
    m=oracle.snapshot()
    if not state["lyrics"].get("ok"):
        return web.json_response({"ok":False,"error":"no_lyrics","message":"No lyrics loaded"},status=409)

    p=int(m.get("position_ms",0))
    sp=float(m.get("sampled_perf_ms",0))
    if sp and m.get("playing"):
        p+=int(max(0,time.perf_counter()*1000-sp))
    ref_perf=time.perf_counter()

    # Tell every connected screen that a manual lock-on has started.
    await broadcast({"type":"resync","phase":"scanning","position_ms":p})

    result=await timing.force_resync(p,bool(m.get("playing")),ref_perf=ref_perf)
    state["timing"]=timing.export()

    await broadcast({
        "type":"timing",
        "timing":state["timing"],
    })
    await broadcast({
        "type":"resync",
        "phase":"locked" if result.get("ok") else "failed",
        "result":result,
    })

    status=200 if result.get("ok") else 409
    return web.json_response(result,status=status)

async def cover(req):
    q=req.rel_url.query
    try:
        f=await covers.get(q.get("artist",""),q.get("title",""),q.get("album",""))
    except Exception:
        f=None
    if not f:
        return web.Response(status=204,headers={"Cache-Control":"no-store"})
    return web.FileResponse(f,headers={"Cache-Control":"max-age=86400","Content-Type":"image/jpeg"})

async def join_qr(req):
    local,lan=join_urls()
    # Prefer the memorable .local name. The LAN fallback is printed next to it
    # in the UI in case a client OS has mDNS disabled.
    target=local
    img=qrcode.make(target)
    buf=io.BytesIO()
    img.save(buf,format="PNG")
    return web.Response(
        body=buf.getvalue(),
        content_type="image/png",
        headers={"Cache-Control":"no-store"},
    )

async def startup(app):
    await register_mdns()
    try:
        await oracle.start()
    except Exception as exc:
        # Keep the stage, LAN companions and beat engine alive even when the
        # Windows media-session API is unavailable (N editions, policy, etc).
        print(f"[oracle] media session unavailable: {type(exc).__name__}: {exc}")
    app["bridge"]=asyncio.create_task(oracle_bridge())
    app["align"]=asyncio.create_task(align_loop())
    app["beat"]=asyncio.create_task(beat_loop())
    if not EMBEDDED:
        async def op():
            await asyncio.sleep(.7);webbrowser.open("http://127.0.0.1:8765")
        app["open"]=asyncio.create_task(op())

async def cleanup(app):
    for k in ("bridge","align","beat","open"):
        t=app.get(k)
        if t:t.cancel()
    for ws in list(clients):
        try:await ws.close()
        except Exception:pass
    clients.clear()
    if lyrics_task and not lyrics_task.done():lyrics_task.cancel()
    await private_audio.close()
    await unregister_mdns()
    stop_audio()

async def close_clients(app):
    # on_shutdown runs before the listener waits for open connections, so
    # guests are released immediately instead of stalling the shutdown.
    for ws in list(clients):
        try:await ws.close(code=1001,message=b"engine stopping")
        except Exception:pass
    clients.clear()

def stop_audio():
    try:timing.audio.stop=True
    except Exception:pass
    try:
        if timing.model is not None:timing.model.close()
    except Exception:pass
    try:private_audio.capture.close()
    except Exception:pass

def build_app():
    app=web.Application()
    app.router.add_get("/",index);app.router.add_get("/ws",ws_handler);app.router.add_get("/api/debug",debug);app.router.add_get("/api/join",join_info);app.router.add_post("/api/webrtc/offer",webrtc_offer);app.router.add_get("/api/private-audio/status",private_audio_status);app.router.add_post("/api/resync",manual_resync);app.router.add_post("/api/musixmatch-resync",musixmatch_resync);app.router.add_get("/join-qr.png",join_qr);app.router.add_get("/api/cover",cover)
    app.router.add_static("/static/",STATIC,show_index=False)
    app.on_startup.append(startup);app.on_shutdown.append(close_clients);app.on_cleanup.append(cleanup)
    return app

def main():
    app=build_app()
    local,lan=join_urls()
    print("LyricFestival VISUAL RAVE // local: http://127.0.0.1:8765")
    print(f"LAN               // {lan}")
    print(f"mDNS              // {local}")
    print("If Windows Firewall asks, allow Python on Private networks.")
    try:
        web.run_app(app,host="0.0.0.0",port=PORT,print=None)
    except Exception:
        import traceback
        err=traceback.format_exc()
        print(err)
        try:
            with open(DATA/"logs"/"crash.log","a",encoding="utf-8") as fh:
                fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] engine crashed\n{err}")
        except Exception:
            pass
        raise

if __name__=="__main__":main()
