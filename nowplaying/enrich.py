"""Fill in metadata that a player's MPRIS interface doesn't publish.

Browsers expose only a page title, so album and cover art go missing. Two
sources, both cheap and neither involving audio capture:

  * Plex  -- authoritative. Its /status/sessions reports the real artist,
    album, track and artwork, so nothing has to be guessed from a title string.
  * iTunes Search -- public, no auth. Works for any player, used when Plex
    isn't the thing playing.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class Info:
    artist: str = ""
    album: str = ""
    title: str = ""
    art_url: str = ""
    duration: float = 0.0
    source: str = ""
    position: float = 0.0    # seconds, from Plex's viewOffset
    state: str = ""          # "playing" | "paused" (Plex only)

    @property
    def usable(self) -> bool:
        return bool(self.title or self.album or self.art_url)

    @property
    def playing(self) -> bool:
        return self.state == "playing"


def _plex_conf() -> tuple[str, str] | None:
    p = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    f = p / config.APP / "plex.env"
    if not f.exists():
        return None
    url = token = ""
    try:
        for line in f.read_text().splitlines():
            if line.startswith("PLEX_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("PLEX_TOKEN="):
                token = line.split("=", 1)[1].strip()
    except OSError:
        return None
    return (url, token) if url and token else None


def _get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT,
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
        return json.load(r)


def from_plex() -> Info | None:
    """Whatever this Plex server is currently playing, if anything."""
    conf = _plex_conf()
    if not conf:
        return None
    url, token = conf
    try:
        data = _get_json(f"{url}/status/sessions",
                         {"X-Plex-Token": token, "Accept": "application/json"})
    except (urllib.error.URLError, OSError, ValueError):
        return None
    items = (data.get("MediaContainer") or {}).get("Metadata") or []
    for m in items:
        if m.get("type") != "track":
            continue
        thumb = m.get("thumb") or m.get("parentThumb") or ""
        # Ask Plex to scale it: the original can be a 4000px, 20 MB scan,
        # for an image the panel draws at icon size.
        art = (f"{url}/photo/:/transcode?" + urllib.parse.urlencode({
            "width": 600, "height": 600, "minSize": 1, "upscale": 0,
            "url": thumb, "X-Plex-Token": token}) if thumb else "")
        player = m.get("Player") or {}
        return Info(
            # grandparentTitle is the *album* artist -- "Various Artists" on a
            # compilation, which LRCLIB never matches. The track's own artist,
            # when it differs, is in originalTitle.
            artist=m.get("originalTitle") or m.get("grandparentTitle") or "",
            album=m.get("parentTitle") or "",
            title=m.get("title") or "",
            art_url=art,
            duration=float(m.get("duration") or 0) / 1000.0,
            source="plex",
            position=float(m.get("viewOffset") or 0) / 1000.0,
            state=(player.get("state") or "").lower(),
        )
    return None


def from_itunes(artist: str, title: str) -> Info | None:
    """Public metadata lookup -- fills album and artwork for any player.

    iTunes search is fuzzy, and its top hit is often a different song: one
    with the same title by another artist, a cover, a remix. So a hit
    only counts when it corroborates both the artist and the title, and even
    then it contributes album and artwork only -- the player's own artist and
    title stay, since they are what the lyrics lookup needs. With no artist
    there is nothing to corroborate against (it's usually a video's page
    title anyway), so don't guess.
    """
    if not (artist and title):
        return None
    q = urllib.parse.urlencode({"term": f"{artist} {title}", "media": "music",
                                "entity": "song", "limit": 10})
    try:
        data = _get_json(f"https://itunes.apple.com/search?{q}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    best = next((r for r in data.get("results") or []
                 if _matches(r.get("trackName"), title)
                 and _matches(r.get("artistName"), artist)), None)
    if best is None:
        return None
    art = (best.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
    return Info(
        artist=artist,
        album=best.get("collectionName") or "",
        title=title,
        art_url=art,
        duration=float(best.get("trackTimeMillis") or 0) / 1000.0,
        source="itunes",
    )


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _matches(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def lookup(artist: str, title: str, prefer_plex: bool = True) -> Info | None:
    """Best available metadata for the track, Plex first when it's the source.

    Plex reports what *Plex* is playing, which is not necessarily what this
    machine is playing -- so its answer is only accepted when the track title
    corroborates the hint we already have.
    """
    if prefer_plex:
        info = from_plex()
        if info and info.usable and _matches(info.title, title):
            return info
    return from_itunes(artist, title)
