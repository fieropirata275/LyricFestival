"""Real-time lyric clock: forced alignment + Kalman tracking.

Mixed into LyricsTimingEngine. Two modes:

* line/aligned lyrics (LRCLIB, cached): every pass force-aligns the lyric text
  expected inside the last ~9 s of loopback audio, commits per-word timings
  for well-conditioned words and tracks the global line clock with a Kalman
  filter (slewed, never stepped once locked).
* authoritative word provider (Musixmatch RichSync): provider word spacing is
  never touched; aligned anchors only estimate the provider/global offset,
  which is applied with dead-band and slew limits.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque

import numpy as np

from forced_align import OffsetKalman, guess_language, line_tokens, syllables, WORD_RE

LOOPBACK_MS = 15.0          # WASAPI shared-mode loopback + 40 ms block midpoint bias
MIN_SCORE = 0.30            # mean forced-token posterior to trust a pass
MIN_WORD_P = 0.35
EDGE_HEAD_S = 0.30
EDGE_TAIL_S = 0.25
ALIGN_BIAS_MS = 35.0        # measured early bias of DTW+onset word starts


class ForcedSync:
    # ------------------------------------------------------------ lifecycle
    def _fs_reset(self):
        self.lang = guess_language(self.lines)
        self.ltok = [line_tokens(l.get("text", "")) for l in self.lines]
        cached = self.quality == "cached_word"
        self.kf = OffsetKalman(offset=self.offset_ms, var=(250.0 if cached else 900.0) ** 2)
        self.pkf = OffsetKalman(offset=self.provider_offset_ms, var=120.0 ** 2)
        self.target_offset = float(self.offset_ms)
        self.locked = cached
        self.provider_anchors = 0
        self.ms_per_syl = 240.0
        self.dur_obs = deque(maxlen=80)     # (syllables, onset-to-onset ms)
        self.wa, self.wb = 140.0, 170.0     # word ms = a + b * syllables
        self.bad_passes = 0
        self.next_pass = 0.0
        self.last_save = 0.0
        self.last_score = 0.0
        self._tt_key = None
        self._tt = []
        self._pstart = {}
        if self._provider_mode():
            for v in self.wordmap.values():
                li = int(v.get("line", -1))
                st = float(v["start_ms"])
                if li not in self._pstart or st < self._pstart[li]:
                    self._pstart[li] = st

    def _fs_on_seek(self):
        # Clock knowledge survives a seek; only widen uncertainty a little.
        if hasattr(self, "kf"):
            self.kf.P[0, 0] = max(self.kf.P[0, 0], 180.0 ** 2)
            self.bad_passes = 0
            self.next_pass = 0.0

    # --------------------------------------------------- rhythm model
    def _word_ms(self, token):
        return self.wa + self.wb * syllables(token)

    def _fit_rhythm(self):
        """Ridge regression of onset-to-onset time on syllable count, with a
        prior so a handful of observations cannot run away."""
        if len(self.dur_obs) < 4:
            return
        X = np.array([[1.0, s] for s, _ in self.dur_obs])
        y = np.array([d for _, d in self.dur_obs])
        lam = np.diag([4.0, 4.0])
        prior = np.array([140.0, 170.0])
        A = X.T @ X + lam
        bvec = X.T @ y + lam @ prior
        a, b = np.linalg.solve(A, bvec)
        self.wa = float(min(450.0, max(40.0, a)))
        self.wb = float(min(420.0, max(60.0, b)))
        self.ms_per_syl = self.wb

    # ------------------------------------------------------ timing model
    def _provider_mode(self):
        return self.quality == "word_provider" and bool(self.wordmap)

    def _line_raw_start(self, li):
        return float(self.lines[li].get("t", 0))

    def _line_start(self, li):
        if self._provider_mode():
            base = self._pstart.get(li, self._line_raw_start(li))
            return base + self.provider_offset_ms
        return self._line_raw_start(li) + self.offset_ms

    def _line_end(self, li):
        if li + 1 < len(self.lines):
            nxt = self._line_start(li + 1)
        else:
            nxt = self._line_start(li) + 4200
        toks, _ = self.ltok[li]
        natural = sum(self._word_ms(t) for t in toks) * 1.15 + 250
        return min(nxt, self._line_start(li) + max(600.0, natural))

    def _token_times(self, li):
        """Best current estimate (track ms) of each display token in a line."""
        toks, mapping = self.ltok[li]
        if not toks:
            return []
        off = self.provider_offset_ms if self._provider_mode() else 0.0
        known = []
        for ti, wids in enumerate(mapping):
            ent = [self.wordmap.get(f"{li}:{w}") for w in wids]
            ent = [e for e in ent if e]
            if ent:
                known.append((ti, min(float(e["start_ms"]) for e in ent) + off,
                              max(float(e["end_ms"]) for e in ent) + off))
        L, E = self._line_start(li), self._line_end(li)
        dur = [self._word_ms(t) for t in toks]
        out = [None] * len(toks)
        for ti, s, e in known:
            out[ti] = (s, e)
        pts = [(-1, L)] + [(ti, s) for ti, s, e in known] + [(len(toks), E)]
        for (ia, ta), (ib, tb) in zip(pts, pts[1:]):
            if ia >= 0 and out[ia]:
                ta = out[ia][0] + dur[ia]          # next onset after a known word
            span_tok = list(range(ia + 1, ib))
            if not span_tok:
                continue
            want = sum(dur[i] for i in span_tok)
            room = tb - ta
            if ib < len(toks):
                k = room / want if want > 0 else 1.0   # between anchors: fit exactly
            else:
                k = min(1.0, room / want) if want > 0 and room > 0 else 1.0  # tail: natural pace
            k = max(0.35, min(2.5, k))
            acc = ta
            for i in span_tok:
                d = dur[i] * k
                out[i] = (acc, acc + d * 0.92)
                acc += d
        return out

    def token_times_all(self):
        """Display-token timings for the whole song (ms, track clock).
        Single source of truth for every screen; recomputed only when the
        model changes."""
        if not hasattr(self, "ltok"):
            return []
        key = (len(self.wordmap), round(self.offset_ms, 1), round(self.provider_offset_ms, 1),
               round(self.wa, 1), round(self.wb, 1), self.pass_no, self.quality)
        if key == self._tt_key:
            return self._tt
        out = []
        for li in range(len(self.lines)):
            try:
                out.append([[int(round(s)), int(round(e))] for s, e in self._token_times(li)])
            except Exception:
                out.append([])
        self._tt_key, self._tt = key, out
        return out

    # ------------------------------------------------- candidate windows
    def _active_lines(self, t0, t1):
        res = []
        for li in range(len(self.lines)):
            if not self.ltok[li][0]:
                continue
            s, e = self._line_start(li), self._line_end(li)
            if s < t1 and e > t0:
                res.append(li)
        return res

    def _tokens_in_window(self, lines, t0, t1, margin):
        refs = []
        for li in lines:
            times = self._token_times(li)
            for ti, (s, e) in enumerate(times):
                if e < t0 - margin or s > t1 + margin:
                    continue
                refs.append((li, ti))
        return refs

    def _build_candidates(self, t0, t1, wide=False):
        margin = 350.0 + min(1500.0, 2.0 * self.kf.sigma if not self._provider_mode() else 2.0 * self.pkf.sigma)
        base_lines = self._active_lines(t0 - margin, t1 + margin)
        cands = []
        if base_lines:
            cands.append(self._tokens_in_window(base_lines, t0, t1, margin))
        if wide or self.bad_passes >= 2 or (not self._provider_mode() and not self.locked):
            n = max(1, len(base_lines))
            centre = base_lines[0] if base_lines else self._nearest_line((t0 + t1) / 2)
            span = 10 if wide else 2
            for k in range(max(0, centre - span), min(len(self.lines), centre + span + 1)):
                group = [li for li in range(k, min(len(self.lines), k + n)) if self.ltok[li][0]]
                if group and group != base_lines:
                    cands.append([(li, ti) for li in group for ti in range(len(self.ltok[li][0]))])
        # de-duplicate
        seen, uniq = set(), []
        for c in cands:
            key = tuple(c)
            if c and key not in seen:
                seen.add(key)
                uniq.append(c)
        return uniq[:24]

    def _nearest_line(self, t):
        best, bd = 0, 1e18
        for li in range(len(self.lines)):
            d = abs(self._line_start(li) - t)
            if d < bd:
                best, bd = li, d
        return best

    # --------------------------------------------------------- one pass
    async def _forced_pass(self, track_ms, ref_perf, seconds, wide=False):
        audio, end_perf = self.audio.latest_with_time(seconds)
        if len(audio) < self.audio.sr * 3.0 or not end_perf:
            return dict(ok=False, error="not_enough_audio")
        t1 = float(track_ms) - (float(ref_perf) - float(end_perf)) * 1000.0 - LOOPBACK_MS
        dur_ms = len(audio) / self.audio.sr * 1000.0
        t0 = t1 - dur_ms
        cands = self._build_candidates(t0, t1, wide=wide)
        if not cands:
            return dict(ok=False, error="no_lyrics_here")
        token_lists = [[self.ltok[li][0][ti] for li, ti in c] for c in cands]
        loop = asyncio.get_running_loop()
        self._ensure_model()
        results = await loop.run_in_executor(None, self.model.align, audio, token_lists, self.lang)
        best_i = max(range(len(results)), key=lambda i: results[i]["score"])
        best = results[best_i]
        self.last_score = float(best["score"])
        anchors = []
        dur_s = dur_ms / 1000.0
        for (li, ti), w in zip(cands[best_i], best["words"]):
            if not w:
                continue
            s, e, p = w
            if s < EDGE_HEAD_S or e > dur_s - EDGE_TAIL_S or (e - s) < 0.05 or p < MIN_WORD_P:
                continue
            anchors.append(dict(line=li, tok=ti, start=t0 + s * 1000.0 + ALIGN_BIAS_MS,
                                end=t0 + e * 1000.0 + ALIGN_BIAS_MS * 0.5, p=float(p)))
        ok = best["score"] >= MIN_SCORE and len(anchors) >= 2
        self.bad_passes = 0 if ok else self.bad_passes + 1
        return dict(ok=ok, anchors=anchors, score=best["score"], hypothesis=best_i,
                    candidates=len(cands), error=None if ok else ("no_vocals" if best["score"] < 0.12 else "weak_match"))

    # ----------------------------------------------------- commit anchors
    def _raw_token_start(self, li, ti):
        """LRC-raw (offset-free) predicted start of a token, by rhythm model."""
        toks, _ = self.ltok[li]
        return self._line_raw_start(li) + sum(self._word_ms(t) for t in toks[:ti])

    def _commit_line_anchors(self, anchors, hard=False):
        now = time.monotonic()
        res = []
        for a in anchors:
            li, ti = a["line"], a["tok"]
            z = a["start"] - self._raw_token_start(li, ti)
            # the line clock is observed directly only by line-initial words
            sigma = (55.0 if ti == 0 else 450.0 + 60.0 * ti) / max(0.3, a["p"])
            res.append((z, sigma))
            self._store_token(a)
        # learn singing rate from consecutive anchors
        by_line = {}
        for a in anchors:
            by_line.setdefault(a["line"], []).append(a)
        for li, rows in by_line.items():
            rows.sort(key=lambda r: r["tok"])
            toks = self.ltok[li][0]
            for r1, r2 in zip(rows, rows[1:]):
                if r2["tok"] == r1["tok"] + 1:
                    d = r2["start"] - r1["start"]
                    if 60 < d < 1600:
                        self.dur_obs.append((syllables(toks[r1["tok"]]), d))
        self._fit_rhythm()
        if hard and res:
            z = statistics.median([z for z, _ in res])
            self.kf.x[0] = z
            self.kf.x[1] = 0.0
            self.kf.P = np.diag([60.0 ** 2, 1.0])
            self.offset_ms = self.target_offset = z
            self.locked = True
        else:
            self.kf.predict(now)
            for z, sg in res:
                self.kf.update(z, sg)
            self.target_offset = self.kf.offset
            gap = self.target_offset - self.offset_ms
            if not self.locked and self.kf.sigma < 160.0:
                self.offset_ms = self.target_offset      # first lock: snap
                self.locked = True
            else:
                self.offset_ms += max(-45.0, min(45.0, gap))   # slew
        self._repair_monotonic_order()
        if now - self.last_save > 5.0:
            self.last_save = now
            self._save()

    def _store_token(self, a):
        li, ti = a["line"], a["tok"]
        toks, mapping = self.ltok[li]
        wids = mapping[ti] if ti < len(mapping) else []
        if not wids:
            return
        words = WORD_RE.findall(toks[ti])
        weights = [syllables(w) for w in words] or [1.0]
        tot = sum(weights)
        acc = a["start"]
        span = max(40.0, a["end"] - a["start"])
        for wid, w, wt in zip(wids, words, weights):
            s = acc
            e = acc + span * wt / tot
            acc = e
            key = f"{li}:{wid}"
            old = self.wordmap.get(key)
            conf = round(float(a["p"]), 3)
            if old and old.get("provider") == "musixmatch":
                continue
            if old:
                os_, oc = float(old["start_ms"]), float(old.get("confidence", 0.5))
                if abs(os_ - s) < 140:
                    wsum = oc + conf
                    s = (os_ * oc + s * conf) / wsum
                    e = (float(old["end_ms"]) * oc + e * conf) / wsum
                    conf = max(oc, conf)
                elif conf < oc * 0.8:
                    continue
            self.wordmap[key] = {
                "line": li, "word": wid, "text": w,
                "start_ms": int(round(s)), "end_ms": int(round(max(s + 40, e))),
                "confidence": round(conf, 3), "provider": "forced",
            }

    def _commit_provider_anchors(self, anchors, hard=False):
        res = []
        for a in anchors:
            li, ti = a["line"], a["tok"]
            _, mapping = self.ltok[li]
            wids = mapping[ti] if ti < len(mapping) else []
            ent = [self.wordmap.get(f"{li}:{w}") for w in wids]
            ent = [e for e in ent if e]
            if not ent:
                continue
            raw = min(float(e["start_ms"]) for e in ent)
            res.append((a["start"] - raw, 45.0 / max(0.3, a["p"])))
        if not res:
            return 0
        if hard:
            z = statistics.median([z for z, _ in res])
            self.pkf.x[0] = z
            self.pkf.P = np.diag([40.0 ** 2, 1.0])
            self.provider_offset_ms = float(max(-4000.0, min(4000.0, z)))
            self.provider_anchors = len(res)
            return len(res)
        self.pkf.predict(time.monotonic())
        for z, sg in res:
            if self.pkf.update(z, sg):
                self.provider_anchors += 1
        gap = self.pkf.offset - self.provider_offset_ms
        # dead-band + slew: the provider stays law, we only fix the clock
        if self.provider_anchors >= 6 and self.pkf.sigma < 45.0 and abs(gap) > 30.0:
            self.provider_offset_ms += max(-25.0, min(25.0, gap))
        return len(res)

    # ------------------------------------------------------ public passes
    async def forced_update(self, track_ms, ref_perf):
        provider = self._provider_mode()
        now = time.perf_counter()
        if now < self.next_pass or self.worker_lock.locked():
            return
        gpu = self.device == "cuda"
        interval = (0.45 if gpu else 0.9) * (2.5 if provider else 1.0)
        self.next_pass = now + interval
        async with self.worker_lock:
            self.pass_no += 1
            r = await self._forced_pass(track_ms, ref_perf, 10.0 if gpu else 9.0)
            if not r.get("ok"):
                self.last_added = 0
                self.status = (f"{self.device.upper()} {self.model_name.upper()} · FORCED ALIGN · "
                               f"{'LISTENING' if r.get('error') == 'no_vocals' else 'SEARCHING'} · score {r.get('score', 0):.2f}")
                return
            if provider:
                n = self._commit_provider_anchors(r["anchors"])
                self.last_added = 0
                self.status = (f"MUSIXMATCH LAW · AUTO-CAL {self.provider_offset_ms:+.0f}ms "
                               f"σ{self.pkf.sigma:.0f} · {n} anchors")
            else:
                before = len(self.wordmap)
                self._commit_line_anchors(r["anchors"])
                self.last_added = max(0, len(self.wordmap) - before)
                self.quality = "forced_word"
                self.status = (f"{self.device.upper()} {self.model_name.upper()} · FORCED ALIGN · "
                               f"σ{self.kf.sigma:.0f}ms · {len(self.wordmap)}/{len(self.flat)} words")

    async def forced_resync(self, track_ms, ref_perf, provider=False):
        async with self.worker_lock:
            self.status = "MANUAL RESYNC · LISTENING…"
            r = await self._forced_pass(track_ms, ref_perf, 12.0, wide=True)
            if not r.get("ok"):
                self.status = f"MANUAL RESYNC · {str(r.get('error')).upper()}"
                return dict(ok=False, error=r.get("error"), matches=len(r.get("anchors", [])),
                            heard=len(r.get("anchors", [])), timing=self.export())
            if provider:
                old = self.provider_offset_ms
                n = self._commit_provider_anchors(r["anchors"], hard=True)
                self.status = f"MUSIXMATCH LOCKED · {n} ANCHORS · OFFSET {self.provider_offset_ms:+.0f}ms"
                return dict(ok=True, authoritative=True, matches=n,
                            old_provider_offset_ms=round(old, 1),
                            provider_offset_ms=round(self.provider_offset_ms, 1),
                            offset_change_ms=round(self.provider_offset_ms - old, 1),
                            message="MUSIXMATCH LOCKED", timing=self.export())
            old = self.offset_ms
            self._commit_line_anchors(r["anchors"], hard=True)
            self.quality = "forced_word"
            self._save()
            self.status = (f"MANUAL RESYNC LOCKED · {len(r['anchors'])} WORDS · "
                           f"Δ{self.offset_ms - old:+.0f}ms")
            return dict(ok=True, authoritative=False, matches=len(r["anchors"]),
                        committed=len(r["anchors"]), old_offset_ms=round(old, 1),
                        new_offset_ms=round(self.offset_ms, 1),
                        offset_change_ms=round(self.offset_ms - old, 1),
                        message="LOCKED", timing=self.export())
