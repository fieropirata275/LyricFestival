from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class BeatState:
    active: bool = False
    bpm: float = 0.0
    confidence: float = 0.0
    period_ms: float = 0.0
    phase_ms: float = 0.0
    beat_index: int = 0
    bar_beat: int = 0
    bar_index: int = 0
    meter: str = "4/4 estimated"
    intensity: float = 0.0
    last_beat_track_ms: float = 0.0
    next_beat_track_ms: float = 0.0
    status: str = "waiting for audio"

    def export(self):
        return asdict(self)


class BeatOracle:
    """
    Lightweight server-side beat/tempo/phase estimator.

    It consumes the SAME already-captured WASAPI loopback ring buffer used by
    the lyrics aligner. It never modifies that buffer or any lyric timings.

    Two layers:
      - analyzer (~3 Hz): estimates tempo + phase from recent onset energy
      - scheduler (~60 Hz): emits predicted beats on the media timeline

    This gives LAN clients a shared beat clock instead of making every device
    analyze audio independently.
    """

    def __init__(self, audio_capture):
        self.audio = audio_capture
        self.state = BeatState()

        self._track_epoch = None
        self._last_analysis_perf = 0.0
        self._last_state_push_perf = 0.0

        self._period_ms = 500.0
        self._phase_anchor_ms = 0.0
        self._confidence = 0.0

        self._next_beat_ms = None
        self._beat_index = 0
        self._last_emitted_ms = -1e9
        self._last_intensity = 0.5

    def reset(self, track_ms=0.0, track_epoch=None):
        self._track_epoch = track_epoch
        self._last_analysis_perf = 0.0
        self._period_ms = 500.0
        self._phase_anchor_ms = float(track_ms)
        self._confidence = 0.0
        self._next_beat_ms = None
        self._beat_index = 0
        self._last_emitted_ms = -1e9
        self._last_intensity = 0.5
        self.state = BeatState(status="acquiring beat")

    @staticmethod
    def _robust_norm(x):
        x = np.asarray(x, dtype=np.float32)
        if len(x) == 0:
            return x
        med = float(np.median(x))
        mad = float(np.median(np.abs(x - med))) + 1e-6
        return np.clip((x - med) / (mad * 2.2), 0.0, 8.0)

    def _onset_envelope(self, audio):
        sr = int(self.audio.sr)
        # 25 ms frames, 12.5 ms hop.
        frame = max(128, int(sr * 0.025))
        hop = max(64, int(sr * 0.0125))
        if len(audio) < frame * 8:
            return np.zeros(0, dtype=np.float32), hop

        n = 1 + (len(audio) - frame) // hop
        # Keep bounded work.
        if n > 720:
            start = len(audio) - (720 - 1) * hop - frame
            audio = audio[max(0, start):]
            n = 1 + (len(audio) - frame) // hop

        # Strided RMS is cheap and works surprisingly well for kick/snare-heavy
        # pop, rap, drill, reggaeton and EDM.
        shape = (n, frame)
        strides = (audio.strides[0] * hop, audio.strides[0])
        frames = np.lib.stride_tricks.as_strided(audio, shape=shape, strides=strides)

        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-9)
        loge = np.log1p(rms * 45.0)

        # Positive energy flux plus a small absolute-energy term.
        flux = np.maximum(0.0, np.diff(loge, prepend=loge[0]))
        env = self._robust_norm(flux) + 0.18 * self._robust_norm(loge)

        # 3-sample smoothing, preserving transients.
        if len(env) >= 3:
            env = np.convolve(env, np.array([0.2, 0.6, 0.2], dtype=np.float32), mode="same")
        return env.astype(np.float32), hop

    def _estimate(self, audio, track_ms):
        env, hop = self._onset_envelope(audio)
        if len(env) < 80:
            return None

        sr = float(self.audio.sr)
        hop_ms = hop * 1000.0 / sr

        # Tempo autocorrelation range: 62..190 BPM.
        min_bpm, max_bpm = 62.0, 190.0
        min_lag = max(2, int((60000.0 / max_bpm) / hop_ms))
        max_lag = min(len(env) // 2, int((60000.0 / min_bpm) / hop_ms))
        if max_lag <= min_lag:
            return None

        x = env - float(np.mean(env))
        corrs = []
        for lag in range(min_lag, max_lag + 1):
            a = x[:-lag]
            b = x[lag:]
            den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-7
            corrs.append(float(np.dot(a, b) / den))

        corrs = np.asarray(corrs, dtype=np.float32)
        best_rel = int(np.argmax(corrs))
        best_lag = min_lag + best_rel
        best_corr = float(corrs[best_rel])

        # Prefer musically plausible double/half-time interpretations by
        # bringing tempo into roughly 78..170 when possible.
        bpm = 60000.0 / (best_lag * hop_ms)
        period_ms = best_lag * hop_ms
        while bpm < 78.0:
            bpm *= 2.0
            period_ms *= 0.5
        while bpm > 170.0:
            bpm *= 0.5
            period_ms *= 2.0

        # Local peaks for phase.
        threshold = max(0.8, float(np.percentile(env, 72)))
        peak_idx = np.where(
            (env[1:-1] >= env[:-2]) &
            (env[1:-1] > env[2:]) &
            (env[1:-1] >= threshold)
        )[0] + 1
        if len(peak_idx) == 0:
            return None

        # Audio end is approximately "now" on media timeline. Convert each
        # onset index into track time.
        audio_ms = len(audio) * 1000.0 / sr
        audio_start_track = float(track_ms) - audio_ms
        peak_times = audio_start_track + peak_idx * hop_ms
        peak_weights = env[peak_idx]

        # Choose phase which maximizes peak support on a periodic grid.
        recent_mask = peak_times >= (float(track_ms) - 5000.0)
        pt = peak_times[recent_mask]
        pw = peak_weights[recent_mask]
        if len(pt) == 0:
            pt, pw = peak_times, peak_weights

        # Candidate anchors from stronger recent peaks.
        order = np.argsort(pw)[-min(18, len(pw)):]
        candidates = pt[order]
        best_anchor = float(candidates[-1])
        best_score = -1.0

        for anchor in candidates:
            # Distance of each detected onset to nearest grid beat.
            d = np.abs(((pt - anchor + period_ms / 2.0) % period_ms) - period_ms / 2.0)
            support = np.exp(-0.5 * (d / max(45.0, period_ms * 0.13)) ** 2)
            score = float(np.sum(support * pw) / (np.sum(pw) + 1e-6))
            if score > best_score:
                best_score = score
                best_anchor = float(anchor)

        # Bring anchor to nearest beat at/before current position.
        k = math.floor((float(track_ms) - best_anchor) / period_ms)
        anchor_now = best_anchor + k * period_ms

        intensity = float(np.clip(np.percentile(peak_weights, 82) / 4.0, 0.12, 1.0))
        confidence = float(np.clip((best_corr + max(0.0, best_score)) * 0.55, 0.0, 1.0))

        return {
            "bpm": bpm,
            "period_ms": period_ms,
            "anchor_ms": anchor_now,
            "confidence": confidence,
            "intensity": intensity,
        }

    def _analyze_if_due(self, track_ms):
        now = time.perf_counter()
        if now - self._last_analysis_perf < 0.30:
            return
        self._last_analysis_perf = now

        try:
            audio = self.audio.latest(8.0)
        except Exception:
            return

        if len(audio) < self.audio.sr * 2.0:
            self.state.status = "collecting audio"
            return

        est = self._estimate(audio, track_ms)
        if not est:
            self.state.status = "searching beat"
            return

        # Smooth tempo, but phase can reacquire faster.
        if self._confidence < 0.15:
            self._period_ms = est["period_ms"]
            self._phase_anchor_ms = est["anchor_ms"]
        else:
            # Avoid giant tempo jumps caused by half-time ambiguity.
            ratio = est["period_ms"] / max(1.0, self._period_ms)
            if 0.72 <= ratio <= 1.38:
                self._period_ms = self._period_ms * 0.78 + est["period_ms"] * 0.22

            # Move the current phase gently toward detected phase.
            predicted_anchor = self._phase_anchor_ms
            cycles = round((est["anchor_ms"] - predicted_anchor) / max(1.0, self._period_ms))
            aligned_est = est["anchor_ms"] - cycles * self._period_ms
            phase_err = aligned_est - predicted_anchor
            phase_err = float(np.clip(phase_err, -180.0, 180.0))
            self._phase_anchor_ms += phase_err * 0.30

        self._confidence = self._confidence * 0.68 + est["confidence"] * 0.32
        self._last_intensity = self._last_intensity * 0.65 + est["intensity"] * 0.35

        # Rebuild scheduler if absent or badly out of phase.
        if self._next_beat_ms is None:
            cycles = math.floor((float(track_ms) - self._phase_anchor_ms) / self._period_ms) + 1
            self._next_beat_ms = self._phase_anchor_ms + cycles * self._period_ms
        elif abs(self._next_beat_ms - float(track_ms)) > self._period_ms * 2.2:
            cycles = math.floor((float(track_ms) - self._phase_anchor_ms) / self._period_ms) + 1
            self._next_beat_ms = self._phase_anchor_ms + cycles * self._period_ms

        self.state.active = self._confidence >= 0.10
        self.state.bpm = round(60000.0 / max(1.0, self._period_ms), 2)
        self.state.period_ms = round(self._period_ms, 2)
        self.state.phase_ms = round(self._phase_anchor_ms, 1)
        self.state.confidence = round(self._confidence, 3)
        self.state.intensity = round(self._last_intensity, 3)
        self.state.next_beat_track_ms = round(float(self._next_beat_ms or 0), 1)
        self.state.status = "locked" if self._confidence >= 0.22 else "acquiring"

    def update(self, track_ms, playing, track_epoch=None):
        """
        Returns zero or more beat events. Usually 0 or 1.
        """
        track_ms = float(track_ms or 0.0)

        if track_epoch != self._track_epoch:
            self.reset(track_ms, track_epoch)

        if not playing:
            self.state.active = False
            self.state.status = "paused"
            self._next_beat_ms = None
            return []

        self._analyze_if_due(track_ms)

        if self._next_beat_ms is None or self._confidence < 0.08:
            return []

        events = []
        # Catch at most 2 beats after temporary event-loop stalls.
        for _ in range(2):
            if track_ms + 9.0 < self._next_beat_ms:
                break

            beat_ms = float(self._next_beat_ms)
            if beat_ms <= self._last_emitted_ms + 80.0:
                self._next_beat_ms += self._period_ms
                continue

            self._beat_index += 1
            bar_beat = ((self._beat_index - 1) % 4) + 1
            bar_index = (self._beat_index - 1) // 4
            downbeat = bar_beat == 1

            # Downbeats get a little visual emphasis, not a fake audio claim.
            intensity = float(np.clip(
                self._last_intensity * (1.18 if downbeat else 1.0),
                0.12, 1.0
            ))

            event = {
                "beat_index": self._beat_index,
                "bar_beat": bar_beat,
                "bar_index": bar_index,
                "downbeat": downbeat,
                "meter": "4/4 estimated",
                "track_ms": round(beat_ms, 1),
                "bpm": round(60000.0 / max(1.0, self._period_ms), 2),
                "confidence": round(self._confidence, 3),
                "intensity": round(intensity, 3),
            }
            events.append(event)

            self._last_emitted_ms = beat_ms
            self._next_beat_ms += self._period_ms

            self.state.beat_index = self._beat_index
            self.state.bar_beat = bar_beat
            self.state.bar_index = bar_index
            self.state.last_beat_track_ms = round(beat_ms, 1)
            self.state.next_beat_track_ms = round(self._next_beat_ms, 1)
            self.state.active = True

        return events
