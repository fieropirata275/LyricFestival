from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, asdict

@dataclass
class OracleState:
    found: bool = False
    source: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    position_ms: int = 0
    duration_ms: int = 0
    raw_position_ms: int = 0
    sampled_perf_ms: float = 0.0
    track_epoch: int = 0
    seek_epoch: int = 0
    playback_epoch: int = 0
    sequence: int = 0
    event: str = "init"
    clock_mode: str = "init"
    raw_delta_ms: int = 0

def _ms(v):
    if v is None:
        return 0
    try:
        return max(0, int(v.total_seconds()*1000))
    except Exception:
        pass
    try:
        return max(0, int(v.duration/10000))
    except Exception:
        return 0

class MediaOracle:
    """
    Event-driven Spotify/Windows media tracker.

    Fast path:
      - keep one selected GSMTC session
      - subscribe to media/timeline/playback events
      - sample only that session's lightweight timeline every ~25 ms
      - monotonic clock bridges stale/repeated Windows timeline values
      - push state changes through an asyncio queue/WebSocket

    No Spotify Web API is used for realtime tracking.
    """
    def __init__(self):
        self.manager = None
        self.session = None
        self.state = OracleState(sampled_perf_ms=time.perf_counter()*1000)
        self.events = asyncio.Queue(maxsize=1024)
        self._tokens = []
        self._dirty_media = True
        self._dirty_playback = True
        self._dirty_timeline = True
        self._session_dirty = True
        self._last_raw = None
        self._anchor_pos = 0.0
        self._anchor_perf = time.perf_counter()
        self._last_playing = False
        self._last_track_key = ""
        self._stopping = False

    async def start(self):
        import winrt.windows.media.control as media
        self.manager = await media.GlobalSystemMediaTransportControlsSessionManager.request_async()
        self._subscribe_manager()
        await self._select_session(force=True)
        asyncio.create_task(self._loop())

    def _subscribe_manager(self):
        if not self.manager:
            return
        pairs = [
            ("add_current_session_changed","remove_current_session_changed"),
            ("add_sessions_changed","remove_sessions_changed"),
        ]
        for addn, remn in pairs:
            try:
                add = getattr(self.manager, addn)
                rem = getattr(self.manager, remn)
                token = add(lambda *a: self._manager_event())
                self._tokens.append((rem, token))
            except Exception:
                pass

    def _manager_event(self):
        self._session_dirty = True

    def _unsubscribe_session(self):
        keep=[]
        for rem, tok in self._tokens:
            # manager tokens don't belong to session; crude but safe: session
            # tokens are tagged through closures below, so only keep manager funcs.
            if getattr(rem, "__self__", None) is self.manager:
                keep.append((rem,tok))
            else:
                try: rem(tok)
                except Exception: pass
        self._tokens = keep

    def _subscribe_session(self, session):
        self._unsubscribe_session()
        self.session = session
        if not session:
            return
        handlers = [
            ("add_media_properties_changed","remove_media_properties_changed","media"),
            ("add_playback_info_changed","remove_playback_info_changed","playback"),
            ("add_timeline_properties_changed","remove_timeline_properties_changed","timeline"),
        ]
        for addn, remn, kind in handlers:
            try:
                add=getattr(session,addn); rem=getattr(session,remn)
                def cb(*args, _kind=kind):
                    if _kind=="media": self._dirty_media=True
                    elif _kind=="playback": self._dirty_playback=True
                    else: self._dirty_timeline=True
                tok=add(cb)
                self._tokens.append((rem,tok))
            except Exception:
                pass
        self._dirty_media=self._dirty_playback=self._dirty_timeline=True

    async def _select_session(self, force=False):
        if not self.manager:
            return
        if self.session and not (force or self._session_dirty):
            return
        self._session_dirty=False
        try:
            sessions=list(self.manager.get_sessions())
        except Exception:
            sessions=[]
        current=None
        try:
            current=self.manager.get_current_session()
        except Exception:
            pass

        best=None
        best_score=-1
        for s in sessions:
            try:
                src=(s.source_app_user_model_id or "")
                score=100 if "spotify" in src.lower() else 0
                if current is not None and src==(current.source_app_user_model_id or ""):
                    score+=30
                try:
                    pb=s.get_playback_info()
                    import winrt.windows.media.control as media
                    if pb.playback_status==media.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING:
                        score+=20
                except Exception:
                    pass
                if score>best_score:
                    best_score=score; best=s
            except Exception:
                pass

        if best is not self.session:
            self._subscribe_session(best)
            self._last_raw=None
            self._last_track_key=""

    async def _read_metadata(self):
        if not self.session:
            return "","",""
        try:
            p=await self.session.try_get_media_properties_async()
            return (p.title or "").strip(), (p.artist or "").strip(), (p.album_title or "").strip()
        except Exception:
            return "","",""

    def _read_playback(self):
        if not self.session:
            return False
        try:
            import winrt.windows.media.control as media
            pb=self.session.get_playback_info()
            return pb.playback_status==media.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
        except Exception:
            return False

    def _read_timeline(self):
        if not self.session:
            return 0,0
        try:
            tl=self.session.get_timeline_properties()
            raw=_ms(tl.position)
            dur=max(_ms(tl.end_time),_ms(tl.max_seek_time))
            return raw,dur
        except Exception:
            return 0,0

    def _continuous_clock(self, raw, playing, track_key):
        """
        Convert coarse/stale GSMTC timeline samples into a stable high-resolution
        playback clock.

        CRITICAL invariant:
          An unchanged raw GSMTC timestamp can NEVER be interpreted as a seek.

        Seek detection is based on an actual CHANGE in raw_position, not on the
        difference between a stale raw value and our continuously advancing clock.
        """
        now=time.perf_counter()
        predicted=self._anchor_pos + ((now-self._anchor_perf)*1000 if self._last_playing else 0)

        # Initial/new-track anchor.
        if self._last_raw is None or track_key!=self._last_track_key:
            self._anchor_pos=float(raw)
            self._anchor_perf=now
            self._last_raw=int(raw)
            self._last_playing=bool(playing)
            self._last_track_key=track_key
            return self._anchor_pos, "track_anchor", False

        # Play/pause transition.
        if bool(playing)!=bool(self._last_playing):
            # Pause should freeze close to whichever value is most recent.
            # Resume anchors from the newest authoritative raw position, but
            # never invents a backwards seek.
            if playing:
                self._anchor_pos=max(float(raw),float(predicted))
            else:
                # At pause, raw is often fresher than during playback.
                if abs(float(raw)-float(predicted)) <= 1200:
                    self._anchor_pos=float(raw)
                else:
                    self._anchor_pos=float(predicted)
            self._anchor_perf=now
            self._last_raw=int(raw)
            self._last_playing=bool(playing)
            return self._anchor_pos, "playback_anchor", False

        previous_raw=int(self._last_raw)
        raw=int(raw)
        raw_delta=raw-previous_raw

        # ================================================================
        # THE v5 FIX:
        # If Windows repeats the exact same timeline.position, it is stale.
        # It is NOT a seek, regardless of how far our monotonic clock has
        # advanced since Windows last refreshed it.
        # ================================================================
        if playing and raw_delta == 0:
            return float(predicted), "stale_raw_bridge", False

        # Only a REAL CHANGE in the raw GSMTC position may imply a seek.
        #
        # Normal Spotify/GSMTC updates can arrive in coarse jumps of roughly
        # hundreds of ms or ~1 second, so forward seek detection needs a much
        # larger threshold. A backwards movement is much more diagnostic.
        seeked=False

        if playing:
            # User dragging timeline backwards.
            if raw_delta < -350:
                seeked=True

            # User jumping substantially forwards.
            # Normal coarse publication should not jump this far in one sample.
            elif raw_delta > 2800:
                seeked=True

            if seeked:
                self._anchor_pos=float(raw)
                self._anchor_perf=now
                self._last_raw=raw
                return self._anchor_pos, "real_seek_anchor", True

            # Genuine non-seek raw update.
            #
            # Compare it with our monotonic prediction only for PLL correction.
            # This comparison is NEVER used to classify a seek.
            error=float(raw)-float(predicted)

            # Update last_raw now that Windows actually supplied a new value.
            self._last_raw=raw

            # A fresh raw sample can move our prediction forward significantly.
            # Small backwards errors are treated as publication latency and only
            # very gently influence the running clock.
            if error > 80:
                corrected=float(predicted)+min(error*.42,180.0)
                mode="pll_forward"
            elif error < -80:
                corrected=float(predicted)+max(error*.035,-8.0)
                mode="pll_stale_backward"
            else:
                corrected=float(predicted)+error*.10
                mode="pll_fine"

            # Absolute monotonic guarantee inside the clock itself.
            corrected=max(corrected,float(self._anchor_pos))

            self._anchor_pos=corrected
            self._anchor_perf=now
            return corrected, mode, False

        # Paused: raw position is authoritative and may legally change if the
        # user seeks while paused. Detect that as a seek as well.
        if not playing:
            paused_seek = abs(raw_delta) > 250
            self._last_raw=raw
            self._anchor_pos=float(raw)
            self._anchor_perf=now
            return self._anchor_pos, ("paused_seek" if paused_seek else "paused"), paused_seek

        return float(predicted), "monotonic_bridge", False

    async def _publish(self, event):
        self.state.sequence+=1
        self.state.event=event
        payload=asdict(self.state)
        try:
            self.events.put_nowait(payload)
        except asyncio.QueueFull:
            try: self.events.get_nowait()
            except Exception: pass
            try: self.events.put_nowait(payload)
            except Exception: pass

    async def _loop(self):
        last_meta_refresh=0.0
        last_payload=None

        while not self._stopping:
            await self._select_session()

            if not self.session:
                if self.state.found:
                    self.state.found=False
                    await self._publish("session_lost")
                await asyncio.sleep(.05)
                continue

            now=time.perf_counter()
            meta_due=self._dirty_media or (now-last_meta_refresh)>1.5
            if meta_due:
                title,artist,album=await self._read_metadata()
                last_meta_refresh=now
                self._dirty_media=False
            else:
                title,artist,album=self.state.title,self.state.artist,self.state.album

            playing=self._read_playback()
            raw,duration=self._read_timeline()
            track_key=f"{artist}|{title}|{album}"

            old_track=f"{self.state.artist}|{self.state.title}|{self.state.album}"
            track_changed=bool(title and artist and track_key!=old_track and old_track!="||")
            playback_changed=playing!=self.state.playing

            prev_raw_for_debug=self._last_raw
            pos,clock_mode,seeked=self._continuous_clock(raw,playing,track_key)
            raw_delta_debug=0 if prev_raw_for_debug is None else int(raw)-int(prev_raw_for_debug)

            if track_changed:
                self.state.track_epoch+=1
                event="track_changed"
            elif seeked:
                self.state.seek_epoch+=1
                event="seek"
            elif playback_changed:
                self.state.playback_epoch+=1
                event="playback"
            else:
                event="tick"

            # HARD MONOTONIC GUARANTEE:
            # During normal playback on the same track, published media time
            # may never move backwards. Small PLL/SMTC corrections are allowed
            # only forward. A backwards jump is legal only when classified as
            # an actual seek above.
            if (
                playing
                and not track_changed
                and not seeked
                and self.state.found
                and self.state.title == title
                and self.state.artist == artist
            ):
                pos=max(float(pos),float(self.state.position_ms))

            self.state.found=bool(title or artist)
            try:self.state.source=self.session.source_app_user_model_id or ""
            except Exception:self.state.source=""
            self.state.title=title
            self.state.artist=artist
            self.state.album=album
            self.state.playing=playing
            self.state.position_ms=max(0,int(pos))
            self.state.raw_position_ms=max(0,int(raw))
            self.state.duration_ms=max(0,int(duration))
            self.state.sampled_perf_ms=time.perf_counter()*1000
            self.state.clock_mode=clock_mode
            self.state.raw_delta_ms=raw_delta_debug

            # Push high-rate clock packets, but metadata/epoch changes are immediate.
            compact=(
                self.state.title,self.state.artist,self.state.playing,
                self.state.position_ms//20,self.state.track_epoch,
                self.state.seek_epoch,self.state.playback_epoch
            )
            if event!="tick" or compact!=last_payload:
                await self._publish(event)
                last_payload=compact

            self._dirty_playback=False
            self._dirty_timeline=False
            await asyncio.sleep(.025)

    def snapshot(self):
        return asdict(self.state)

    async def next_event(self):
        return await self.events.get()
