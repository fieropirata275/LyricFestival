"""Album artwork via MusicBrainz + Cover Art Archive, cached on disk."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import aiohttp

MB = "https://musicbrainz.org/ws/2/"
CAA = "https://coverartarchive.org"
UA = "LyricFestival/1.0 (desktop lyric visualizer; https://musicbrainz.org/doc/MusicBrainz_API)"
MISS_TTL = 3 * 24 * 3600

_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*(remaster|deluxe|edition|version|feat\.?|ft\.?|with |slowed|reverb|sped up|"
    r"speed up|nightcore|bonus|explicit|clean|radio edit|extended|live|mono|stereo|anniversary)[^)\]]*[\)\]]",
    re.I,
)
_DASH = re.compile(r"\s+-\s+(.*(remaster|slowed|reverb|sped up|radio edit|version|edit|mix).*)$", re.I)


def _clean(s: str) -> str:
    s = _NOISE.sub("", s or "")
    s = _DASH.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _q(s: str) -> str:
    # Lucene phrase-safe value
    return re.sub(r'[\\"]', " ", s or "").strip()


def _primary_artist(a: str) -> str:
    return re.split(r"\s*(?:,|&| x | feat\.? | ft\.? | with )\s*", a or "", maxsplit=1, flags=re.I)[0].strip()


class CoverArt:
    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.dir / "index.json"
        try:
            self.index = json.loads(self.index_file.read_text(encoding="utf-8"))
        except Exception:
            self.index = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.mb_lock = asyncio.Lock()
        self.last_mb = 0.0

    @staticmethod
    def key(artist: str, title: str, album: str) -> str:
        raw = f"{_primary_artist(artist).lower()}|{_clean(album or title).lower()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def _save_index(self):
        try:
            self.index_file.write_text(json.dumps(self.index), encoding="utf-8")
        except Exception:
            pass

    async def _mb(self, session: aiohttp.ClientSession, entity: str, query: str):
        # MusicBrainz etiquette: max ~1 request per second.
        async with self.mb_lock:
            wait = 1.05 - (time.monotonic() - self.last_mb)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_mb = time.monotonic()
            async with session.get(MB + entity, params={"query": query, "fmt": "json", "limit": "8"}) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
        return data.get(entity + "s", []) if isinstance(data, dict) else []

    async def _candidates(self, session, artist: str, title: str, album: str):
        art = _q(_primary_artist(artist))
        alb = _q(_clean(album))
        tit = _q(_clean(title))
        out: list[tuple[str, str]] = []

        def add(kind, mbid):
            if mbid and (kind, mbid) not in out:
                out.append((kind, mbid))

        if alb and art:
            for rel in await self._mb(session, "release", f'release:"{alb}" AND artist:"{art}"'):
                if int(rel.get("score", 0)) < 85:
                    continue
                add("release-group", (rel.get("release-group") or {}).get("id"))
                add("release", rel.get("id"))
        if len(out) < 2 and tit and art:
            for rec in await self._mb(session, "recording", f'recording:"{tit}" AND artist:"{art}"'):
                if int(rec.get("score", 0)) < 85:
                    continue
                for rel in rec.get("releases", [])[:4]:
                    add("release-group", (rel.get("release-group") or {}).get("id"))
                    add("release", rel.get("id"))
        return out[:10]

    async def get(self, artist: str, title: str, album: str) -> Path | None:
        if not (artist and (title or album)):
            return None
        k = self.key(artist, title, album)
        f = self.dir / f"{k}.jpg"
        if f.is_file() and f.stat().st_size > 1000:
            return f
        miss = self.index.get(k)
        if miss and time.time() - float(miss) < MISS_TTL:
            return None
        lock = self.locks.setdefault(k, asyncio.Lock())
        async with lock:
            if f.is_file() and f.stat().st_size > 1000:
                return f
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=timeout) as s:
                    for kind, mbid in await self._candidates(s, artist, title, album):
                        async with s.get(f"{CAA}/{kind}/{mbid}/front-500") as r:
                            if r.status == 200 and r.headers.get("Content-Type", "").startswith("image/"):
                                data = await r.read()
                                if len(data) > 1000:
                                    tmp = f.with_suffix(".part")
                                    tmp.write_bytes(data)
                                    tmp.replace(f)
                                    self.index.pop(k, None)
                                    self._save_index()
                                    print(f"[cover] {artist} - {album or title} -> {kind} {mbid}", flush=True)
                                    return f
            except Exception as exc:
                print(f"[cover] lookup failed: {type(exc).__name__}: {exc}", flush=True)
                return None  # network trouble: do not cache as a miss
            self.index[k] = time.time()
            self._save_index()
            print(f"[cover] no artwork for {artist} - {album or title}", flush=True)
            return None
