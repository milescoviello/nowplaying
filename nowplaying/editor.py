"""Lyrics of your own: written or fixed in your editor, and synced to the
track by tapping each line in as it's sung.

Saved to the file the daemon names for the track (State.lyrics_file), which
it reads in place of LRCLIB from then on, and picks up within a tick of the
save. Nothing here talks to the daemon; it notices the file.
"""
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import lyrics as lyrics_mod
from .state import State


class LyricsError(Exception):
    """Why the lyrics couldn't be written, in a few words to show as is."""


def target(s: State) -> Path:
    """The file this track's own lyrics are kept in."""
    if not s.title:
        raise LyricsError("nothing is playing")
    if not s.lyrics_file:
        # Published once the lookup is done. A lookup that's done without
        # it is a daemon from before lyrics of your own.
        if s.lyrics or s.lyrics_plain or s.lyrics_source:
            raise LyricsError("this daemon can't keep lyrics of your own; restart it")
        raise LyricsError("still looking up the lyrics")
    return Path(s.lyrics_file)


def tags(s: State) -> dict[str, str]:
    """The ID tags a file starts with, to say which track it's for."""
    out = {"ar": s.artist, "ti": s.title, "al": s.album}
    if s.duration > 0:
        minutes, seconds = divmod(round(s.duration), 60)
        out["length"] = f"{minutes}:{seconds:02d}"
    return out


def original(s: State) -> list[tuple[float, str]]:
    """The synced lines as written: in their own script if transliterated."""
    return s.lyrics_original if 0 < len(s.lyrics_original) == len(s.lyrics) else s.lyrics


def sheet(s: State) -> str:
    """What the editor starts from for a track with no file of its own: the
    lyrics showing now, timings and all, so a wrong word is fixed without
    syncing again. With none showing, just the tags, to write under."""
    if s.lyrics:
        return lyrics_mod.format_lrc(original(s), tags(s))
    plain = s.lyrics_plain_original or s.lyrics_plain
    return lyrics_mod.format_lrc([], tags(s)) + "\n" + (plain + "\n" if plain else "")


def _editor() -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        if os.environ.get(var, "").strip():
            return shlex.split(os.environ[var])
    for name in ("nano", "vi"):
        if shutil.which(name):
            return [name]
    raise LyricsError("no editor to open: set $EDITOR")


def _run(command: list[str]) -> int:
    try:
        proc = subprocess.Popen(command)
    except OSError as exc:
        raise LyricsError(f"couldn't start {command[0]}: {exc.strerror}") from None
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue   # the editor's own Ctrl+C, which reached us too


def edit(s: State) -> str:
    """Open the track's lyrics in $VISUAL or $EDITOR and keep what comes
    back. Returns what happened, in a few words."""
    path = target(s)
    try:
        before = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        before = sheet(s)
    except OSError as exc:
        raise LyricsError(f"couldn't read {path.name}: {exc.strerror}") from None
    command = _editor()
    # Edited out of the way, and only saved whole, so the daemon never picks
    # up half an edit -- or one abandoned.
    fd, name = tempfile.mkstemp(prefix="nowplaying-", suffix=".lrc")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(before)
        if _run([*command, name]) != 0:
            raise LyricsError("the editor quit with an error; nothing saved")
        after = Path(name).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LyricsError(f"couldn't edit the lyrics: {exc.strerror}") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)
    if after == before:
        return "unchanged; nothing saved"
    if not lyrics_mod.parse_own(after).available:
        return "no lyrics in it; nothing saved"
    return _save(path, after)


def import_file(s: State, source: Path, force: bool = False) -> str:
    """Take a .lrc or a plain text file as the track's lyrics."""
    path = target(s)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise LyricsError(f"couldn't read {source}: "
                          f"{getattr(exc, 'strerror', None) or 'not text'}") from None
    if not lyrics_mod.parse_own(text).available:
        raise LyricsError(f"no lyrics in {source}")
    if path.exists() and not force:
        raise LyricsError(f"there are lyrics saved already, in {path}; "
                          "--force replaces them")
    return _save(path, text)


def _save(path: Path, text: str) -> str:
    try:
        lyrics_mod.save_own(path, text)
    except OSError as exc:
        raise LyricsError(f"couldn't save {path.name}: {exc.strerror}") from None
    return f"saved {path.name}"


def _stanzas(lines: list[str]) -> list[int]:
    """Which lines of a plain sheet to tap: every one with words, and a single
    blank between stanzas, for the gap a break in the singing leaves."""
    keep: list[int] = []
    for i, line in enumerate(lines):
        if line.strip():
            if keep and not lines[i - 1].strip():
                keep.append(i - 1)
            keep.append(i)
    return keep


@dataclass
class Draft:
    """Lyrics being synced to the track: each line's start, or None until
    it's tapped in. The cursor is the line the next tap is for, and is past
    the end once every line has been."""
    key: str                # the track it's for
    path: Path
    tags: dict[str, str]
    texts: list[str]        # as saved: in their own script
    shown: list[str]        # as read: in Latin letters, if transliterated
    times: list[float | None]
    cursor: int = 0
    # Each tap's line and the time it had before, so taps can be taken back.
    taps: list[tuple[int, float | None]] = field(default_factory=list)
    shifted: float = 0.0    # how far every line has been moved, all told
    changed: bool = False

    @classmethod
    def start(cls, s: State, pos: float) -> Draft:
        """A draft of the lyrics showing, its cursor on the next line to be
        sung: timings kept, for lyrics that only need some retiming."""
        path = target(s)
        if s.lyrics:
            texts = [text for _, text in original(s)]
            shown = [text for _, text in s.lyrics]
            times: list[float | None] = [t for t, _ in s.lyrics]
        else:
            raw = (s.lyrics_plain_original or s.lyrics_plain).splitlines()
            latin = s.lyrics_plain.splitlines()
            if len(latin) != len(raw):
                latin = raw
            keep = _stanzas(raw)
            texts = [raw[i].strip() for i in keep]
            shown = [latin[i].strip() for i in keep]
            times = [None] * len(texts)
        if not texts:
            raise LyricsError("no lyrics to sync: write them first")
        cursor = next((i for i, t in enumerate(times) if t is None or t > pos), len(times))
        return cls(key=s.key, path=path, tags=tags(s), texts=texts, shown=shown,
                   times=times, cursor=cursor)

    def tap(self, pos: float) -> None:
        """The cursor's line starts now."""
        if self.cursor >= len(self.times):
            return
        self.taps.append((self.cursor, self.times[self.cursor]))
        self.times[self.cursor] = pos
        self.cursor += 1
        self.changed = True

    def untap(self) -> None:
        """Take the last tap back, and the cursor to its line to tap again."""
        if not self.taps:
            self.move(-1)
            return
        line, before = self.taps.pop()
        self.times[line] = before
        self.cursor = line

    def move(self, step: int) -> None:
        self.cursor = max(0, min(len(self.times), self.cursor + step))

    def shift(self, seconds: float) -> None:
        """Every line timed so far that much later (earlier, if negative)."""
        def moved(t: float | None) -> float | None:
            return None if t is None else max(0.0, t + seconds)
        self.times = [moved(t) for t in self.times]
        self.taps = [(line, moved(before)) for line, before in self.taps]
        self.shifted = round(self.shifted + seconds, 3)
        self.changed = True

    def untimed(self) -> int:
        """Lines with words in them still to tap. A gap can go untapped."""
        return sum(1 for t, text in zip(self.times, self.texts) if t is None and text)

    def current(self, pos: float) -> int:
        """The line its timings say is being sung at `pos`, or -1."""
        best = -1
        for i, t in enumerate(self.times):
            if t is not None and t <= pos:
                best = i
        return best

    def lrc(self) -> str:
        lines: list[tuple[float, str]] = []
        last = 0.0
        for t, text in zip(self.times, self.texts):
            if t is None:
                continue
            # A line retimed past the next keeps its place in the sheet.
            last = max(last, t)
            lines.append((last, text))
        return lyrics_mod.format_lrc(lines, self.tags)

    def save(self) -> str:
        left = self.untimed()
        if left:
            raise LyricsError(f"{left} line{'s'[:left != 1]} still to tap")
        # Beside a plain .txt rather than over it: the .lrc wins, and the
        # plain sheet it was synced from stays as it was.
        said = _save(self.path.with_suffix(".lrc"), self.lrc())
        self.changed = False
        return said
