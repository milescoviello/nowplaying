"""Configuration and shared constants for nowplaying."""
from __future__ import annotations

import os
from pathlib import Path

APP = "nowplaying"

# --- capture -----------------------------------------------------------------
# shazamio-core fingerprints a SEGMENT_SECONDS window taken from the middle of
# whatever clip we hand it, and Shazam reports `offset` = the track position at
# the START of that window. So for a clip of length L:
#     offset          = clip_start + (L/2 - SEGMENT_SECONDS/2)
#     position at end = offset + L/2 + SEGMENT_SECONDS/2
# Verified against known-position playback: error was a constant -0.05 s.
# CLIP_SECONDS must stay >= SEGMENT_SECONDS or the core uses the whole clip
# instead of a centred window and this relation breaks.
SEGMENT_SECONDS = 10.0
CLIP_SECONDS = 12.0
OFFSET_TAIL = SEGMENT_SECONDS / 2
SAMPLE_RATE = 16000
CHANNELS = 1

# parec hands us audio this far behind real time, so the samples at the end of
# a clip were actually played CAPTURE_LATENCY seconds ago. Cancels the residual
# error measured above.
CAPTURE_LATENCY_MS = 50
CAPTURE_LATENCY = CAPTURE_LATENCY_MS / 1000.0

assert CLIP_SECONDS >= SEGMENT_SECONDS, "clip must be at least one fingerprint segment"

# Below this RMS (0..1 full scale) we treat the capture as silence and skip the
# network call entirely.
SILENCE_RMS = 0.004
# Short probe used to poll for "has audio come back yet" without burning a
# recognition request.
PROBE_SECONDS = 1.0

# --- cadence -----------------------------------------------------------------
# Paused this long and the panel widget stops holding the lyrics up and shows
# the idle display instead. Long enough to survive a quick pause, short enough
# that a walk-away hands the space back.
PAUSE_IDLE_SECONDS = 15.0

# Some sources report position as a periodic checkpoint rather than a live
# clock -- Plex's viewOffset only moves every ~15s. Re-anchoring on every poll
# would keep yanking the local clock back to a stale reading, so only re-anchor
# when a genuinely new reading disagrees with the running clock by this much.
POSITION_RESYNC_TOLERANCE = 2.5

IDLE_POLL = 3.0          # seconds between silence probes when nothing is playing
SEARCH_INTERVAL = 2.0    # gap between recognition attempts while searching
# Re-recognise this often while locked, which is also how fast a mid-track skip
# is noticed. This is close to the practical floor: one check costs a 1 s probe
# plus a CLIP_SECONDS capture plus the lookup (~14 s), so anything much below
# this just means recognising back to back.
VERIFY_INTERVAL = 15.0
RESYNC_TOLERANCE = 3.0   # |predicted - measured| under this = clock is fine

# --- sources -----------------------------------------------------------------
# What the popup offers, and all a saved choice may hold. `--source` also takes
# an explicit pactl source name, but only on the command line.
SOURCES = ("mpris", "auto", "loopback", "mic")

# --- lyrics ------------------------------------------------------------------
LRCLIB_BASE = "https://lrclib.net/api"
# LRCLIB gains lyrics over time, so a miss is only remembered for a day --
# long enough that replaying an album doesn't re-ask for every track.
LYRICS_MISS_TTL = 24 * 3600
# Album art is kept on disk so a repeat play paints instantly; past this the
# least recently shown covers are dropped.
COVER_CACHE_MB = 50
USER_AGENT = "nowplaying/0.1 (personal use; https://lrclib.net)"
HTTP_TIMEOUT = 10.0

# --- paths -------------------------------------------------------------------
def _runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg)
    return Path(f"/tmp/{APP}-{os.getuid()}")


def socket_path() -> Path:
    return _runtime_dir() / f"{APP}.sock"


def pid_path() -> Path:
    return _runtime_dir() / f"{APP}.pid"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def lyrics_cache_dir() -> Path:
    d = cache_dir() / "lyrics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def covers_dir() -> Path:
    d = cache_dir() / "covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP


def source_file() -> Path:
    """The source picked in the panel popup: one word from SOURCES.

    A file rather than a socket command, because the applet is QML with no
    socket API; it writes this through the same executable data source it
    reads the state with. Being a file, the choice also outlives a restart.
    """
    return config_dir() / "source"


def log_path() -> Path:
    return cache_dir() / "daemon.log"


def state_file() -> Path:
    """State mirrored to disk for clients that can't speak to a unix socket.

    The Plasma applet is QML, which has no socket API -- it polls this file.
    """
    return cache_dir() / "state.json"
