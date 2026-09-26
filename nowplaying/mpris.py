"""MPRIS source via playerctl.

Preferred over fingerprinting whenever a player exposes metadata: it costs no
audio capture at all (so nothing trips the desktop's recording indicator), no
network round-trip to Shazam, and it reports the playback position exactly
rather than estimating it.

The catch is metadata quality. Browsers publish the page title with no artist
field, so "▶ Some Artist - Some Song (Official Video)" has to be cleaned up
before LRCLIB will match it.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

SEP = "\x1f"
FORMAT = SEP.join([
    "{{status}}", "{{artist}}", "{{title}}", "{{album}}",
    "{{mpris:length}}", "{{position}}", "{{mpris:artUrl}}",
])

# Leading playback glyphs some players prepend to the title.
_GLYPHS = re.compile(r"^[\s▶⏸⏹⏯●♪♫‖]+")
# YouTube's unread-notification count: "(12) Some Video Title".
_COUNTER = re.compile(r"^\(\d+\)\s*")
# What Firefox publishes when a page plays media without describing it.
_PLACEHOLDER = re.compile(r"^\w+ is playing media$", re.I)
# Plex's web player publishes TV episodes as "Show - S1 · E10".
_EPISODE = re.compile(r"\bS\d+\s*·\s*E\d+\b")
# Site suffixes: " - YouTube", " | Spotify", ...
_SUFFIX = re.compile(
    r"\s*[-|–]\s*(YouTube( Music)?|Spotify|SoundCloud|Bandcamp|Vimeo|Twitch)\s*$",
    re.I)
# Parenthetical noise that stops LRCLIB matching.
# Landing-page titles that are never a track name.
_BARE_TITLES = {
    "plex", "youtube", "youtube music", "spotify", "soundcloud", "bandcamp",
    "jellyfin", "emby", "navidrome", "twitch", "vimeo", "netflix", "new tab",
}
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"official(?:\s+(?:music\s+)?(?:video|audio|visualizer|lyric\w*))?|"
    r"lyrics?(?:\s+video)?|audio|video|visuali[sz]er|hd|hq|4k|8k|"
    r"full\s+album|music\s+video|explicit|clean|remaster(?:ed)?(?:\s*\d{4})?|"
    r"with\s+lyrics|closed\s+captions?|cc"
    r")\s*[\)\]]", re.I)


@dataclass
class Now:
    status: str
    artist: str
    title: str
    album: str
    duration: float   # seconds, 0 when unknown
    position: float   # seconds
    art_url: str

    @property
    def playing(self) -> bool:
        return self.status.lower() == "playing"

    @property
    def key(self) -> str:
        return f"{self.artist.lower()}|{self.title.lower()}"

    @property
    def usable(self) -> bool:
        """Is this an actual track, or just a browser tab's page title?

        A stale tab parked on a site's landing page publishes the site name as
        the title with no artist and no length -- believing it produces
        confidently wrong lyrics for a track that isn't playing.
        """
        if not self.title or _PLACEHOLDER.match(self.title):
            return False
        if _EPISODE.search(self.title):
            return False   # a TV episode, not a track
        if self.artist:
            return True
        # A site's name is never a track -- even with a length, which a
        # paused Plex or Navidrome tab keeps publishing.
        if self.title.lower() in _BARE_TITLES:
            return False
        return self.duration > 0 or len(self.title) > 3


def clean_title(raw: str) -> str:
    t = _GLYPHS.sub("", raw or "").strip()
    t = _COUNTER.sub("", t)
    t = _SUFFIX.sub("", t)
    t = _NOISE.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -–—")


def split_artist_title(artist: str, title: str) -> tuple[str, str]:
    """Browsers give an empty artist and 'Artist - Title' in one string."""
    artist = (artist or "").strip()
    title = clean_title(title)
    if artist:
        return artist, title
    for sep in (" - ", " – ", " — ", " ~ ", " | "):
        if sep in title:
            left, right = title.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return "", title


def _run(args: list[str]) -> str | None:
    try:
        p = subprocess.run(["playerctl", *args], capture_output=True,
                           text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    # Only trim newlines. Python counts \x1c-\x1f as whitespace, so a bare
    # .strip() would eat our field separator whenever the last field is empty.
    return p.stdout.rstrip("\r\n")


def players() -> list[str]:
    out = _run(["-l"])
    return [p.strip() for p in (out or "").splitlines() if p.strip()]


def poll(player: str | None = None) -> Now | None:
    """Current state, or None when no player is publishing anything usable."""
    args = ["metadata", "--format", FORMAT]
    if player:
        args = ["--player", player, *args]
    out = _run(args)
    if not out:
        return None
    # Players may omit trailing fields entirely; pad rather than reject.
    parts = (out.split(SEP) + [""] * 7)[:7]
    status, artist, title, album, length, position, art = (p.strip() for p in parts)

    def num(v: str) -> float:
        try:
            # playerctl reports these in microseconds
            return max(0.0, float(v) / 1_000_000)
        except (TypeError, ValueError):
            return 0.0

    artist, title = split_artist_title(artist, title)
    if not title:
        return None
    return Now(status=status or "Stopped", artist=artist, title=title,
               album=(album or "").strip(), duration=num(length),
               position=num(position), art_url=art or "")
