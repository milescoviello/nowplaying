"""LRCLIB lyrics client, LRC parser, on-disk cache, and lyrics of your own."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config

_TS = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# An ID tag on a line of its own. Only the known names: a sheet's own
# "[Chorus: Someone]" is a line of lyrics, not a tag.
_TAG = re.compile(r"\[(ar|al|ti|au|by|length|offset|re|ve|tool|la|id):([^\]]*)\]",
                  re.I)


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


def _tags(text: str) -> dict[str, str]:
    """An LRC's ID tags -- [ar:...], [offset:...] -- by lower-case name."""
    return {m.group(1).lower(): m.group(2).strip()
            for m in map(_TAG.fullmatch, map(str.strip, text.splitlines())) if m}


def _seconds(text: str) -> float:
    """"3:45" or "03:45.20" in seconds; 0 if it's neither."""
    m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)", text.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse an LRC body into sorted (timestamp, text) pairs.

    Handles multiple timestamps sharing one line, e.g. `[00:12.34][01:02.00]words`,
    and an `[offset:ms]` tag, which moves every line that much earlier.
    """
    try:
        offset = float(_tags(text).get("offset") or 0) / 1000
    except ValueError:
        offset = 0.0
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
            out.append((max(0.0, minutes * 60 + seconds + frac_val - offset), body))
    out.sort(key=lambda x: x[0])
    return out


def format_lrc(lines: list[tuple[float, str]], tags: dict[str, str] | None = None) -> str:
    """parse_lrc the other way round: the ID tags, then a [mm:ss.xx] per line."""
    # A bracket inside a tag would end it early and leave the rest as a line.
    out = [f"[{name}:{value.replace('[', '(').replace(']', ')')}]"
           for name, value in (tags or {}).items() if value]
    for t, text in lines:
        cs = round(max(0.0, t) * 100)
        out.append(f"[{cs // 6000:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}]{text}")
    return "\n".join(out) + "\n"


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


# --- lyrics of your own ------------------------------------------------------
# One file per track in config.own_lyrics_dir(), named "Artist - Title.lrc" (or
# .txt), which wins over LRCLIB. With timings it is synced, without them a
# plain sheet, and with no lyrics at all it says the track is instrumental.
# Found by name, loosely -- case, punctuation and spacing don't count -- so
# files from other players match too.
OWN_SUFFIXES = (".lrc", ".txt")

# (the folder's stamp when read, loose name -> file)
_own_index: tuple[tuple[int, int] | None, dict[str, Path]] = (None, {})


def _loose(name: str) -> str:
    """What a file name is matched on: its words, in lower case."""
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold()))


def _own_name(artist: str, title: str) -> str:
    return f"{artist} - {title}" if artist else title


def _own_files() -> dict[str, Path]:
    """Every file of your own by its loose name, re-read when the folder changes."""
    global _own_index
    folder = config.own_lyrics_dir()
    try:
        st = folder.stat()
        stamp = (st.st_mtime_ns, st.st_ino)
        if stamp != _own_index[0]:
            files = [(f.suffix.lower() == ".lrc", f.stat().st_mtime_ns, f)
                     for f in folder.iterdir() if not f.name.startswith(".")
                     and f.suffix.lower() in OWN_SUFFIXES]
            # Last in wins: a .lrc over a .txt, and the newest of the rest.
            files.sort(key=lambda entry: entry[:2])
            _own_index = (stamp, {_loose(f.stem): f for _, _, f in files})
    except OSError:
        _own_index = (None, {})
    return _own_index[1]


def own_file(artist: str, title: str) -> Path:
    """Where this track's own lyrics are, or would be kept."""
    name = _own_name(artist, title)
    loose = _loose(name)
    found = _own_files().get(loose) if loose else None
    if found is not None:
        return found
    name = name.replace("/", "-").replace("\0", "").lstrip(".")
    # Inside the usual 255-byte limit on a file name, with room for the rest.
    name = name.encode()[:240].decode(errors="ignore").strip() or "untitled"
    return config.own_lyrics_dir() / f"{name}.lrc"


def own_stamp(artist: str, title: str) -> tuple[Path, tuple[int, int, int] | None]:
    """Changes whenever this track's own lyrics are added, edited or removed."""
    path = own_file(artist, title)
    try:
        st = path.stat()
    except OSError:
        return path, None
    return path, (st.st_mtime_ns, st.st_ino, st.st_size)


def parse_own(text: str) -> Lyrics:
    """One of your own files: synced if it has timings, else a plain sheet."""
    duration = _seconds(_tags(text).get("length", ""))
    lines = parse_lrc(text)
    if lines:
        return Lyrics(lines=lines, synced=True, source="own",
                      plain="\n".join(line for _, line in lines), duration=duration)
    plain = "\n".join(line.rstrip() for line in text.splitlines()
                      if not _TAG.fullmatch(line.strip())).strip("\n")
    if plain.strip():
        return Lyrics(source="own", plain=plain, duration=duration)
    return Lyrics(source="own-instrumental", duration=duration)


def load_own(path: Path) -> Lyrics | None:
    """The lyrics in one of your own files, or None if there's no such file."""
    try:
        # -sig: editors elsewhere start a file with a byte-order mark.
        return parse_own(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return None


def save_own(path: Path, text: str) -> None:
    """Write one of your own files whole, so the daemon never reads half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
