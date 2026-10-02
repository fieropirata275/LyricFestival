from __future__ import annotations

import asyncio
import fractions
import threading
import time
from collections import deque

import numpy as np
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame


class StereoLoopbackCapture:
    """
    Dedicated music-quality WASAPI loopback capture.

    The Whisper/Beat Oracle capture remains untouched (16 kHz mono).
    This capture is 48 kHz stereo in 20 ms blocks for WebRTC/Opus.
    """

    def __init__(self, sr=48000, channels=2, block_ms=20, seconds=3):
        self.sr=int(sr)
        self.channels=int(channels)
        self.block_frames=max(240,int(self.sr*block_ms/1000))
        self.max_blocks=max(20,int(seconds*1000/block_ms))
        self.blocks=deque(maxlen=self.max_blocks)   # (seq, ndarray[frames,2])
        self.seq=0
        self.lock=threading.Lock()
        self.thread=None
        self.stop_flag=False
        self.device_name=""
        self.error=""

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag=False
        self.thread=threading.Thread(target=self._worker,daemon=True,name="LF-WEBRTC-WASAPI")
        self.thread.start()

    def close(self):
        self.stop_flag=True

    def _worker(self):
        try:
            import soundcard as sc
            speaker=sc.default_speaker()
            speaker_name=(getattr(speaker,"name","") or "").lower()

            ranked=[]
            for mic in sc.all_microphones(include_loopback=True):
                name=(getattr(mic,"name","") or "").lower()
                score=0
                if getattr(mic,"isloopback",False):
                    score+=120
                if speaker_name and (speaker_name in name or name in speaker_name):
                    score+=70
                if "loopback" in name:
                    score+=30
                ranked.append((score,mic))

            ranked.sort(key=lambda x:x[0],reverse=True)
            if not ranked:
                raise RuntimeError("No WASAPI loopback device found")

            mic=ranked[0][1]
            self.device_name=getattr(mic,"name","loopback")

            with mic.recorder(
                samplerate=self.sr,
                channels=self.channels,
                blocksize=self.block_frames,
            ) as rec:
                while not self.stop_flag:
                    b=rec.record(numframes=self.block_frames)
                    b=np.asarray(b,dtype=np.float32)

                    if b.ndim==1:
                        b=np.repeat(b[:,None],2,axis=1)
                    elif b.shape[1]==1:
                        b=np.repeat(b,2,axis=1)
                    elif b.shape[1]>2:
                        b=b[:,:2]

                    # Clamp only; no DSP or gain modification.
                    b=np.clip(b,-1.0,1.0)

                    with self.lock:
                        self.seq+=1
                        self.blocks.append((self.seq,b.copy()))

        except Exception as exc:
            self.error=f"{type(exc).__name__}: {exc}"

    def read_after(self,last_seq):
        """
        Return the earliest available block newer than last_seq.
        If the consumer fell far behind, jump close to live edge instead of
        accumulating latency.
        """
        with self.lock:
            if not self.blocks:
                return None

            newest=self.blocks[-1][0]
            if last_seq is None:
                # New listeners start right at the live edge.
                return self.blocks[-1]

            # More than 3 blocks behind (~60ms): discard backlog.
            if newest-last_seq>3:
                return self.blocks[-1]

            for seq,b in self.blocks:
                if seq>last_seq:
                    return seq,b
            return None

    def status(self):
        with self.lock:
            q=len(self.blocks)
            seq=self.seq
        return {
            "ok":not bool(self.error),
            "device":self.device_name,
            "error":self.error,
            "sample_rate":self.sr,
            "channels":self.channels,
            "block_frames":self.block_frames,
            "queued_blocks":q,
            "sequence":seq,
        }


class LiveMusicTrack(AudioStreamTrack):
    kind="audio"

    def __init__(self,capture:StereoLoopbackCapture):
        super().__init__()
        self.capture=capture
        self.last_seq=None
        self.samples_sent=0
        self.started=False

    async def recv(self):
        # Wait for the next 20ms block, but never deliberately build a buffer.
        block=None
        for _ in range(12):
            block=self.capture.read_after(self.last_seq)
            if block is not None:
                break
            await asyncio.sleep(0.002)

        if block is None:
            # Short silence keeps RTP clock continuous if WASAPI momentarily
            # misses a block.
            pcm=np.zeros((self.capture.block_frames,2),dtype=np.float32)
        else:
            self.last_seq,pcm=block

        # aiortc/av accepts signed 16-bit packed stereo reliably.
        s16=(np.clip(pcm,-1,1)*32767.0).astype(np.int16)
        packed=np.ascontiguousarray(s16.reshape(-1))

        frame=AudioFrame(format="s16",layout="stereo",samples=len(s16))
        frame.planes[0].update(packed.tobytes())
        frame.sample_rate=self.capture.sr
        frame.time_base=fractions.Fraction(1,self.capture.sr)
        frame.pts=self.samples_sent
        self.samples_sent+=len(s16)

        return frame


class PrivateAudioWebRTC:
    def __init__(self):
        self.capture=StereoLoopbackCapture()
        self.capture.start()
        self.peers=set()

    async def offer(self,sdp,sdp_type="offer"):
        pc=RTCPeerConnection()
        self.peers.add(pc)

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed","closed","disconnected"):
                await self.close_peer(pc)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp,type=sdp_type))
        pc.addTrack(LiveMusicTrack(self.capture))

        answer=await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Give ICE gathering a short opportunity to complete so LAN host
        # candidates are in the returned SDP.
        deadline=time.monotonic()+1.0
        while pc.iceGatheringState!="complete" and time.monotonic()<deadline:
            await asyncio.sleep(.01)

        return {
            "sdp":pc.localDescription.sdp,
            "type":pc.localDescription.type,
        }

    async def close_peer(self,pc):
        if pc in self.peers:
            self.peers.discard(pc)
        try:
            await pc.close()
        except Exception:
            pass

    async def close(self):
        peers=list(self.peers)
        self.peers.clear()
        for pc in peers:
            try:
                await pc.close()
            except Exception:
                pass
        self.capture.close()

    def status(self):
        s=self.capture.status()
        s["peers"]=len(self.peers)
        s["transport"]="WebRTC / Opus"
        return s
