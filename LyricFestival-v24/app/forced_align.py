"""Lyrics-to-audio forced alignment primitives.

Technique stack (all standard in professional lyric/karaoke sync):

1. Forced alignment with a known transcript: the lyric text is fed to Whisper
   as the decoder sequence (teacher forcing) and word boundaries are recovered
   by monotonic DTW over the cross-attention alignment heads (the method used
   by OpenAI's word timestamps / stable-ts). Recognition errors cannot derail
   it because nothing is "recognised" - the text is given.
2. Hypothesis scoring: several candidate lyric windows (previous / expected /
   next line) are aligned against the same encoder pass; forced-token
   posteriors pick the one actually being sung (handles drift and repeated
   choruses).
3. Vocal-onset refinement: word starts are snapped to the nearest spectral-flux
   onset in the vocal band, removing the attention-DTW bias (~20-80 ms).
4. Interior-only anchors: words compressed against the window edges are
   discarded; only well-conditioned words become timing anchors.
5. Clock tracking: anchors drive a Kalman filter (offset + drift) with
   innovation gating, and the published offset is slewed (never stepped) so
   the highlight never jumps.
"""
from __future__ import annotations

import math
import re
import unicodedata

import numpy as np

SR = 16000
ONSET_CENTER_S = 0.032  # n_fft/2 at 16 kHz
WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)

# ------------------------------------------------------------------ text utils
_STOP = {
    "en": "the you and i to a me my it is in that of your on we for all be love know just like what so this oh yeah",
    "es": "que de la el y en no me te lo mi tu se por un una con para es como pero más mas yo si ya quiero",
    "pt": "que não nao de o a e eu você voce me te um uma com pra para meu minha é tá ta na no se",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch in "'’").replace("’", "'")


def guess_language(lines) -> str:
    counts = {k: 0 for k in _STOPSETS}
    for row in lines or []:
        for w in WORD_RE.findall(str(row.get("text", "")).lower()):
            for k, st in _STOPSETS.items():
                if w in st:
                    counts[k] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] >= 3 else "en"


_VOWELS = re.compile(r"[aeiouyáéíóúàèìòùâêîôûãõäëïöü]+", re.I)


def syllables(token: str) -> float:
    w = norm(token)
    if not w:
        return 0.35  # punctuation / symbols: tiny but non-zero
    n = len(_VOWELS.findall(w))
    if w.endswith("e") and n > 1 and not w.endswith(("le", "ee")):
        n -= 1
    return float(max(1, n))


def line_tokens(text: str):
    """Display tokens (whitespace split - what the UI renders) and, for each,
    the list of WORD_RE word indices it contains (the wordmap key space)."""
    toks = str(text or "").split()
    mapping = []
    wi = 0
    for t in toks:
        k = len(WORD_RE.findall(t))
        mapping.append(list(range(wi, wi + k)))
        wi += k
    return toks, mapping


# ----------------------------------------------------- alignment -> tokens
def assign_words_to_tokens(tokens, aligned):
    """Map Whisper's aligned 'words' (which may split punctuation) onto our
    display tokens by walking characters."""
    target = [re.sub(r"\s+", "", t) for t in tokens]
    out = [dict(start=None, end=None, probs=[]) for _ in tokens]
    ti, consumed = 0, 0
    for w in aligned:
        txt = re.sub(r"\s+", "", str(w.get("word", "")))
        if not txt:
            continue
        while ti < len(target) and consumed >= len(target[ti]):
            ti += 1
            consumed = 0
        if ti >= len(target):
            break
        slot = out[ti]
        if slot["start"] is None:
            slot["start"] = float(w["start"])
        slot["end"] = float(w["end"])
        slot["probs"].append(float(w.get("probability", 0.0)))
        consumed += len(txt)
    res = []
    for t, s in zip(tokens, out):
        if s["start"] is None:
            res.append(None)
        else:
            res.append((s["start"], max(s["end"], s["start"] + 0.02), float(np.mean(s["probs"])) if s["probs"] else 0.0))
    return res


# --------------------------------------------------------- onset refinement
def _median_filter_time(x, w):
    """Median over time (axis 0) with an odd window, numpy only."""
    pad = w // 2
    xp = np.pad(x, ((pad, pad), (0, 0)), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(xp, w, axis=0)
    return np.median(view, axis=-1)


def _median_filter_freq(x, w):
    pad = w // 2
    xp = np.pad(x, ((0, 0), (pad, pad)), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(xp, w, axis=1)
    return np.median(view, axis=-1)


def vocal_onset_envelope(audio: np.ndarray, hop: int = 160, n_fft: int = 1024):
    """Harmonic-only onset strength in the vocal band.

    Harmonic/percussive separation (median filtering, Fitzgerald 2010) removes
    kicks, snares and hats; the positive flux of the remaining harmonic energy
    in 250-3500 Hz marks sung-syllable onsets.
    """
    if len(audio) < n_fft:
        return np.zeros(1), hop / SR
    win = np.hanning(n_fft).astype(np.float32)
    n = 1 + (len(audio) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    frames = audio[idx] * win
    mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    band = (freqs >= 250) & (freqs <= 3500)
    S = mag[:, band]
    H = _median_filter_time(S, 17)      # sustained (harmonic) partials
    P = _median_filter_freq(S, 17)      # broadband (percussive) events
    mask = (H * H) / (H * H + P * P + 1e-9)
    harm = np.log1p(S * mask * 30.0).sum(axis=1)
    sm = np.convolve(harm, np.ones(3) / 3.0, mode="same")
    flux = np.maximum(0.0, np.diff(sm, prepend=sm[0]))
    if flux.max() > 0:
        flux = flux / (np.percentile(flux, 97) + 1e-6)
    return flux, hop / SR


def _local_maxima(seg):
    if len(seg) < 3:
        return np.array([], dtype=int)
    return np.where((seg[1:-1] >= seg[:-2]) & (seg[1:-1] > seg[2:]))[0] + 1


def refine_onset(start: float, end: float, env: np.ndarray, step: float, prev_end=None):
    """Recover the true vocal onset of a word.

    Attention-DTW places a word's start at the previous word's boundary, so
    inter-word gaps (and leading silence) are absorbed into the start. The real
    onset lies inside [start, end): take the earliest strong vocal-band onset
    there (strong = at least half of the strongest one in the span).
    """
    if env is None or len(env) < 4:
        return start
    lo = max(1, int((start - 0.06) / step))
    if prev_end is not None:
        lo = max(lo, int((prev_end - 0.03) / step))
    hi = min(len(env) - 2, int(max(start + 0.05, end - 0.05) / step))
    if hi - lo < 2:
        return start
    seg = env[lo:hi + 1]
    peaks = _local_maxima(seg)
    if not len(peaks):
        return start
    vals = seg[peaks]
    strong = peaks[(vals >= 0.5 * vals.max()) & (vals >= 0.3)]
    if not len(strong):
        return start
    k = int(strong[0])
    # walk back from the flux peak to where the rise begins
    while k > 0 and seg[k - 1] > 0.5 * seg[k] and seg[k - 1] < seg[k]:
        k -= 1
    # frame k is timestamped at its analysis-window centre
    return max(0.0, (lo + k) * step + ONSET_CENTER_S)


# ------------------------------------------------------------ clock model
class OffsetKalman:
    """State: [offset_ms, drift_ms_per_s]. Observations: anchor residuals."""

    def __init__(self, offset=0.0, var=600.0 ** 2):
        self.x = np.array([float(offset), 0.0])
        self.P = np.diag([var, 4.0])
        self.t = None
        self.rejects = []

    def predict(self, t_s: float):
        if self.t is None:
            self.t = t_s
            return
        dt = max(0.0, min(30.0, t_s - self.t))
        self.t = t_s
        F = np.array([[1.0, dt], [0.0, 1.0]])
        q = 8.0 ** 2  # ms^2 per s of random walk
        Q = np.array([[q * dt + 1e-3, 0.0], [0.0, 0.02 * dt]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, z_ms: float, sigma_ms: float) -> bool:
        H = np.array([1.0, 0.0])
        S = float(H @ self.P @ H + sigma_ms ** 2)
        innov = z_ms - float(self.x[0])
        if innov * innov > 9.0 * S and self.P[0, 0] < 250.0 ** 2:
            # outlier vs a confident state; a run of consistent outliers means
            # the clock really jumped (seek / different edit) -> re-acquire
            self.rejects.append(z_ms)
            if len(self.rejects) >= 4 and float(np.std(self.rejects[-4:])) < 120.0:
                self.x[0] = float(np.median(self.rejects[-4:]))
                self.P = np.diag([150.0 ** 2, 4.0])
                self.rejects.clear()
                return True
            return False
        self.rejects.clear()
        K = (self.P @ H) / S
        self.x = self.x + K * innov
        self.P = (np.eye(2) - np.outer(K, H)) @ self.P
        return True

    @property
    def offset(self):
        return float(self.x[0])

    @property
    def sigma(self):
        return float(math.sqrt(max(0.0, self.P[0, 0])))


# ------------------------------------------------------- worker-side align
_TOKENIZERS = {}


def align_candidates(model, audio: np.ndarray, candidates, language: str = "en", refine=True):
    """Run one encoder pass and force-align every candidate token list.

    candidates: list of list[str] (display tokens).
    Returns list of dict(score, words=[(start,end,prob)|None per token]).
    Times are seconds from the start of `audio`.
    """
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer

    audio = np.asarray(audio, dtype=np.float32)
    feats = model.feature_extractor(audio)
    n_frames = int(min(feats.shape[-1], 3000))
    enc = model.encode(pad_or_trim(feats))
    multilingual = bool(getattr(model.model, "is_multilingual", True))
    key = (language if multilingual else "en", multilingual)
    tok = _TOKENIZERS.get(key)
    if tok is None:
        tok = Tokenizer(model.hf_tokenizer, multilingual, task="transcribe",
                        language=(language if multilingual else None))
        _TOKENIZERS[key] = tok

    env, step = (vocal_onset_envelope(audio) if refine else (None, 0.01))
    dur = len(audio) / SR
    results = []
    for tokens in candidates:
        if not tokens:
            results.append(dict(score=0.0, words=[]))
            continue
        ids = tok.encode(" " + " ".join(tokens))
        if not ids or len(ids) > 400:
            results.append(dict(score=0.0, words=[None] * len(tokens)))
            continue
        aligned = model.find_alignment(tok, [ids], enc, n_frames)[0]
        words = assign_words_to_tokens(tokens, aligned)
        if refine:
            prev_end = None
            fixed = []
            for w in words:
                if w is None:
                    fixed.append(None)
                    continue
                s, e, p = w
                s2 = refine_onset(s, e, env, step, prev_end=prev_end)
                if s2 < e - 0.03:
                    s = s2
                fixed.append((s, e, p))
                prev_end = e
            words = fixed
        interior = [w[2] for w in words if w and 0.25 < w[0] < dur - 0.2]
        score = float(np.mean(interior)) if interior else 0.0
        results.append(dict(score=score, words=words))
    return results
