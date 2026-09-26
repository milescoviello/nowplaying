"""LRCLIB lyrics client, LRC parser and on-disk cache."""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config

_TS = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")


@dataclass
class Lyrics:
    lines: list[tuple[float, str]] = field(default_factory=list)  # (start_sec, text)
    synced: bool = False
    source: str = ""
    plain: str = ""
    duration: float = 0.0  # LRCLIB knows the track length; Shazam does not

    @property
    def available(self) -> bool:
        return bool(self.lines or self.plain)

    def index_at(self, position: float) -> int:
        """Index of the line that should be highlighted at `position` seconds."""
        if not self.lines:
            return -1
        lo, hi, best = 0, len(self.lines) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid][0] <= position:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse an LRC body into sorted (timestamp, text) pairs.

    Handles multiple timestamps sharing one line, e.g. `[00:12.34][01:02.00]words`.
    """
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():].strip()
        for m in stamps:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            frac_val = int(frac) / (10 ** len(frac))
            out.append((minutes * 60 + seconds + frac_val, body))
    out.sort(key=lambda x: x[0])
    return out


def _cache_key(artist: str, title: str, duration: float | None) -> str:
    base = f"{artist.lower().strip()}|{title.lower().strip()}|{int(duration or 0)}"
    return hashlib.sha256(base.encode()).hexdigest()[:32]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _from_payload(payload: dict) -> Lyrics:
    synced = payload.get("syncedLyrics") or ""
    plain = payload.get("plainLyrics") or ""
    dur = float(payload.get("duration") or 0.0)
    if synced:
        return Lyrics(lines=parse_lrc(synced), synced=True, source="lrclib",
                      plain=plain, duration=dur)
    if plain:
        return Lyrics(lines=[], synced=False, source="lrclib", plain=plain, duration=dur)
    return Lyrics(source="lrclib-instrumental" if payload.get("instrumental") else "",
                  duration=dur)


def fetch(artist: str, title: str, album: str = "", duration: float | None = None,
          use_cache: bool = True) -> Lyrics:
    """Look up lyrics on LRCLIB, preferring a synced (.lrc) match."""
    key = _cache_key(artist, title, duration)
    cache_file = config.lyrics_cache_dir() / f"{key}.json"

    if use_cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if not cached.get("miss"):
                return Lyrics(
                    lines=[(float(t), s) for t, s in cached.get("lines", [])],
                    synced=cached.get("synced", False),
                    source=cached.get("source", "cache"),
                    plain=cached.get("plain", ""),
                    duration=float(cached.get("duration") or 0.0),
                )
            if time.time() - float(cached.get("checked_at") or 0) < config.LYRICS_MISS_TTL:
                return Lyrics(source=cached.get("source", ""),
                              duration=float(cached.get("duration") or 0.0))
        except (OSError, ValueError, TypeError):
            pass

    result = Lyrics()
    # Only a definite "not there" may be remembered as a miss -- never a
    # timeout or an outage, or one bad minute would hide lyrics for a day.
    answered = True
    # Exact match first -- LRCLIB matches on duration, which disambiguates
    # remasters and live versions.
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    try:
        result = _from_payload(_get_json(
            f"{config.LRCLIB_BASE}/get?" + urllib.parse.urlencode(params)))
    except urllib.error.HTTPError as exc:
        answered = exc.code == 404
    except (urllib.error.URLError, OSError, ValueError):
        answered = False

    if not result.available:
        # Fall back to a fuzzy search and pick the closest duration. When the
        # artist is unknown (browsers often publish only a page title) search
        # free-text instead, since an empty artist_name matches nothing.
        try:
            query = ({"artist_name": artist, "track_name": title} if artist
                     else {"q": title})
            hits = _get_json(f"{config.LRCLIB_BASE}/search?" + urllib.parse.urlencode(query))
            if isinstance(hits, list) and hits:
                def score(h):
                    has_sync = 0 if h.get("syncedLyrics") else 1
                    delta = abs((h.get("duration") or 0) - (duration or 0)) if duration else 0
                    return (has_sync, delta)
                result = _from_payload(sorted(hits, key=score)[0])
        except (urllib.error.URLError, OSError, ValueError):
            answered = False

    if result.available:
        entry = {
            "lines": result.lines,
            "synced": result.synced,
            "source": result.source,
            "plain": result.plain,
            "duration": result.duration,
        }
    elif answered:
        entry = {"miss": True, "checked_at": time.time(),
                 "source": result.source, "duration": result.duration}
    else:
        return result
    try:
        cache_file.write_text(json.dumps(entry))
    except OSError:
        pass
    return result
