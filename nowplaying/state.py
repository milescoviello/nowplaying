"""Shared player state broadcast from the daemon to every UI client.

The daemon sends an *anchor* (wall-clock time + track position at that instant)
rather than a ticking position, so each client interpolates locally at its own
frame rate. That keeps the socket quiet and the scroll smooth.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class State:
    status: str = "starting"      # starting|idle|listening|searching|playing|error
    title: str = ""
    artist: str = ""
    album: str = ""
    cover: str = ""        # remote URL from Shazam
    cover_file: str = ""   # local copy, so UIs never touch the network
    key: str = ""

    playing: bool = False
    anchor_wall: float = 0.0      # time.time() when the anchor was taken
    anchor_pos: float = 0.0       # track position (s) at anchor_wall
    duration: float = 0.0         # 0 when unknown
    # The playerctl instance the track comes from, for UIs that send controls.
    # Empty when nothing local is playing it (a Plex client on another device,
    # or audio fingerprinting).
    player: str = ""

    lyrics: list[tuple[float, str]] = field(default_factory=list)
    # Same timings in the original script, when `lyrics` is transliterated.
    lyrics_original: list[tuple[float, str]] = field(default_factory=list)
    lyrics_synced: bool = False
    lyrics_plain: str = ""
    lyrics_source: str = ""

    # What the daemon is set to listen with: mpris | auto | loopback | mic, or
    # a device name from --source. UIs show this, never what they last asked
    # for, so a switch that didn't take is visible as such.
    source_pref: str = ""
    source_label: str = ""
    # Listening to the speaker output for the desktop widget's visualizer,
    # whatever the source: UIs that spell out what is being captured say so.
    vis_listening: bool = False
    message: str = ""
    confidence: str = ""          # "" | "anchored" | "estimated"

    # Shown by the panel widget when nothing is playing, so the space earns its
    # keep. Generic on purpose -- a future idle source can reuse these.
    idle_kind: str = ""           # "" | "fleet"
    idle_line1: str = ""
    idle_line2: str = ""
    idle_ok: bool = True          # False = something needs attention
    # The daemon decides when the idle display takes over, so the UIs don't
    # each have to re-implement the rule.
    idle_active: bool = False

    def position(self, now: float | None = None) -> float:
        if not self.playing:
            return self.anchor_pos
        now = time.time() if now is None else now
        return max(0.0, self.anchor_pos + (now - self.anchor_wall))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        for k in ("lyrics", "lyrics_original"):
            if clean.get(k):
                clean[k] = [(float(t), s) for t, s in clean[k]]
        return cls(**clean)
