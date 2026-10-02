"""Terminal version of the panel widget and its popup.

Renders the same state the applet does, read from the daemon's socket rather
than its state file, and takes the popup's controls from the keyboard. The
daemon still decides everything: keys only go to playerctl, or save a source
for the daemon to pick up.
"""
from __future__ import annotations

import bisect
import colorsys
import contextlib
import functools
import math
import os
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable, Iterator

from rich.align import Align
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.segment import Segment
from rich.style import Style, StyleType
from rich.table import Table
from rich.text import Text

from . import client, config, editor, spectrum
from .state import State

FPS = 15
VIS_FPS = spectrum.FPS   # the bars need more frames than the words do
# The applet's default leadInMs: a line is taken as current this early, so it
# is up by the time it is sung, and in step with the panel.
LEAD_IN = 0.3
RETRY_SECONDS = 2.0
# The daemon sends a heartbeat every 5 s even when nothing changes, so a
# connection this quiet means a wedged daemon rather than an idle one. The same
# limit the applet puts on its state file.
STALE_SECONDS = 20.0
# Keep the line being sung 40% of the way down rather than dead centre: lyrics
# are read downward, so show more of what's coming. As in the popup.
FOLLOW_AT = 0.4
# Scrolling away to read ahead shouldn't be yanked back on the next line: the
# view goes back to the music this long after the last scroll.
RESUME_SECONDS = 4.0
START_HINT = "start it with: nowplaying daemon --source mpris"
FLASH_SECONDS = 3.0      # how long a key's "can't do that" note stays up
# A pick not reported back by then didn't take. Longer than the daemon's
# slowest tick, a fingerprint lookup.
SWITCH_SECONDS = 15.0

# The popup's source buttons, in its order, picked outright with 1-4. No key
# steps through them: going from Player to Mic that way would pass through
# two sources that listen. The hints are the buttons' tooltips, shown on ?.
SOURCES = (
    ("mpris", "Player", "Player metadata only. Nothing is recorded."),
    ("auto", "Auto", "Player metadata when a player has it, else listens: "
                     "speaker output or the mic."),
    ("loopback", "Speaker", "Identify whatever this machine is playing by "
                            "listening to its output."),
    ("mic", "Mic", "Identify whatever is playing in the room through the microphone."),
)
KEYS = (
    ("space", "play / pause"),
    ("n  p", "next / previous track"),
    ("1-4", "pick the source"),
    ("o", "original script / Latin letters"),
    ("h", "pin the homelab readout"),
    ("v", "visualizer (listens to the speaker output)"),
    ("e", "write the lyrics, or fix them, in $EDITOR"),
    ("s", "sync the lyrics to the track, a tap a line"),
    ("↑↓ j k", "scroll the lyrics"),
    ("PgUp PgDn", "a page at a time"),
    ("Home End", "to the top or bottom"),
    ("?", "this list"),
    ("q", "quit"),
)
# While syncing: enter as each line starts, the marker moving on to the next.
SYNC_KEYS = (
    ("enter", "the marked line starts now"),
    ("backspace", "take the last tap back"),
    ("↑↓ j k", "move the marker"),
    ("← →", "the track back or on 5 s"),
    ("0", "the track back to the start"),
    ("- +", "every line a tenth of a second earlier or later"),
    ("w", "save"),
    ("esc", "leave without saving"),
)
# Past the daemon's resync tolerance, so a seek is believed on its next poll.
SEEK_STEP = 5.0
SHIFT_STEP = 0.1
# A seek has landed once the daemon's position is this close to where it
# should be: a player's first reading after one can be stale, and the daemon
# only settles on the next. Not by then, and it never will be; taps go ahead.
SEEK_TOLERANCE = 0.3
SEEK_SECONDS = 3.0
# The marker, a line's timing and a gap, before each line while syncing.
GUTTER = 11

# Escape sequences (arrows, function keys) are read whole, so their trailing
# letters can't be misread as key presses; the ones not named here are dropped.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.)")
_NAMED = {
    "\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down",
    "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
    "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home", "\x1b[7~": "home",
    "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "\x1b[8~": "end",
    "\x1b[D": "left", "\x1bOD": "left", "\x1b[C": "right", "\x1bOC": "right",
}
_PLAIN = {"\x1b": "esc", "\r": "enter", "\n": "enter", "\x7f": "backspace",
          "\x08": "backspace"}


def _keys(data: str) -> list[str]:
    keys, i = [], 0
    while i < len(data):
        m = _ESCAPE.match(data, i)
        if m:
            if m.group() in _NAMED:
                keys.append(_NAMED[m.group()])
            i = m.end()
            continue
        keys.append(_PLAIN.get(data[i], data[i]))
        i += 1
    return keys


@contextlib.contextmanager
def _keyboard() -> Iterator[int | None]:
    """Stdin in cbreak mode -- keys arrive unbuffered and unechoed, Ctrl+C
    still interrupts -- with the terminal put back however we leave.

    Yields None when stdin isn't a terminal: the view still runs, just deaf.
    """
    if not sys.stdin.isatty():
        yield None
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


@contextlib.contextmanager
def _cooked(fd: int | None) -> Iterator[None]:
    """The terminal out of cbreak for a program run in the middle of the
    view -- the editor -- and back into it after."""
    if fd is None:
        yield
        return
    mode = termios.tcgetattr(fd)
    cooked = list(mode)
    cooked[3] |= termios.ECHO | termios.ICANON   # what cbreak took away
    termios.tcsetattr(fd, termios.TCSADRAIN, cooked)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, mode)


def _fmt(seconds: float) -> str:
    t = max(0, int(seconds))
    h, m, s = t // 3600, t // 60 % 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _stamp(seconds: float) -> str:
    """A line's timing, to the hundredth: 1:02.34."""
    cs = round(seconds * 100)
    return f"{cs // 6000}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _cap(key: str, label: str, accent: str, style: StyleType = "") -> Text:
    """A key and what it does, as the button it stands in for."""
    return Text.assemble((key, Style(color=accent, bold=True)), " ", (label, style))


def _mix(colour: str, other: str, share: float) -> str:
    """`colour` moved `share` of the way towards `other`, both #rrggbb."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02x}" for x, y in zip(a, b))


def _rgb(colour: str, scale: float = 1.0) -> str:
    """`colour` (#rrggbb) with each channel scaled, to darken it."""
    r, g, b = (round(int(colour[i:i + 2], 16) * scale) for i in (1, 3, 5))
    return f"#{r:02x}{g:02x}{b:02x}"


@functools.lru_cache(maxsize=16)
def _accent(path: str) -> str:
    """The cover's colour: its most prominent vivid hue, lifted to read on a
    dark background. Worked out once per cover, from a thumbnail.

    Decoded with QImage, which PyQt6 already brings for the overlay and
    which needs no application object.
    """
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage
    except ImportError:
        return PLAIN
    image = QImage(path) if path else None
    if image is None or image.isNull():
        return PLAIN
    side = 32
    small = image.scaled(side, side, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    # Twelve hue buckets, each holding its weight and weighted r, g, b.
    buckets = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]
    for y in range(side):
        for x in range(side):
            v = small.pixel(x, y)
            r, g, b = ((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255
            hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
            if sat < 0.25 or val < 0.2:
                continue
            # Vivid counts for far more than murky, so a small bright emblem
            # wins over a wide dull sky.
            weight = sat * sat * val
            bucket = buckets[int(hue * 12) % 12]
            bucket[0] += weight
            bucket[1] += r * weight
            bucket[2] += g * weight
            bucket[3] += b * weight
    # One hue can straddle two buckets, so each is judged with its neighbours.
    best = max(range(12), key=lambda i: buckets[i][0]
               + (buckets[i - 1][0] + buckets[(i + 1) % 12][0]) / 2)
    group = [buckets[(best + d) % 12] for d in (-1, 0, 1)]
    weight = sum(bucket[0] for bucket in group)
    if weight < 0.01 * side * side:
        return COLOURLESS
    r, g, b = (sum(bucket[i] for bucket in group) / weight for i in (1, 2, 3))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(hue, min(0.72, max(0.6, light)),
                                  min(0.85, max(0.55, sat)))
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _has_original(s: State) -> bool:
    """The lyrics come in their own script too, line for line."""
    return 0 < len(s.lyrics_original) == len(s.lyrics)


# --- visualizer --------------------------------------------------------------
VIS_BLOCKS = " ▁▂▃▄▅▆▇█"
VIS_BAR = 2              # columns per bar
# Wide enough, and the visualizer stands beside the lyrics at their full
# height instead of in a band under them.
VIS_BESIDE = 90


def _cost(pref: str) -> str:
    """The current source's cost, spelled out: anything that listens sends
    fingerprints to Shazam and lights the recording indicator."""
    return {
        "mpris": "No audio capture.",
        "auto": "Listens when no player describes the track: "
                "sent to Shazam, recording indicator on.",
        "loopback": "Listening to the speaker output: "
                    "sent to Shazam, recording indicator on.",
        "mic": "Listening through the mic: sent to Shazam, recording indicator on.",
    }.get(pref, f"Listening to {pref}: sent to Shazam, recording indicator on.")


def _line(text: str, style: str = "") -> Text:
    """One row, cut short rather than wrapped, so the header's height is fixed."""
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


# The line being sung, and the plain sheet, which has no line to single out.
SUNG = Style(color="#ffffff", bold=True)
SHEET = Style(color="#b4b4b4")
TITLE = Style(color="#ffffff", bold=True)
BYLINE = Style(color="#b4b4b4")
QUIET = Style(color="#787878")
# The chrome takes its colour from the cover; without one, this violet.
PLAIN = "#c678dd"
# A cover with next to no colour in it gets a light grey rather than a
# guess at a hue.
COLOURLESS = "#c8c8c8"
ALARM = "#e06c75"
IDLE_BORDER = "#4a4a4a"
TROUGH = "#3a3a3a"       # the unplayed part of the progress bar


@functools.lru_cache(maxsize=64)
def _grey(level: int) -> Style:
    return Style(color=f"#{level:02x}{level:02x}{level:02x}")


def _fade(middle: float, height: int) -> Style:
    """The grey for a line whose middle sits `middle` rows down a sheet
    `height` rows tall: brightest at the line being sung, fading out towards
    the top and bottom edges, as the desktop widget's lines do."""
    focus = height * FOLLOW_AT
    if middle < focus:
        near = middle / max(1.0, focus)
    else:
        near = (height - 1 - middle) / max(1.0, height - 1 - focus)
    near = min(1.0, max(0.0, near))
    # sqrt keeps the lines around the sung one readable, and the fall-off
    # at the edges.
    return _grey(round(255 * (0.16 + 0.6 * math.sqrt(near))))


def _wrap(console: Console, items: list[Text], width: int) -> tuple[list[Text], list[int]]:
    """Each item wrapped and centred to the width, plus the row each starts on."""
    rows: list[Text] = []
    starts = []
    for item in items:
        starts.append(len(rows))
        rows.extend(item.wrap(console, width, justify="center") or [Text("")])
    return rows, starts


class _Fill:
    """Header, body and footer stacked to the exact height on offer, so the
    body can be cut to whatever room the other two leave it."""

    def __init__(self, header: list[RenderableType],
                 body: Callable[[Console, int, int], list[RenderableType]],
                 footer: list[RenderableType]) -> None:
        self.header, self.body, self.footer = header, body, footer

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width, height = options.max_width, options.height or console.height
        opts = options.reset_height()

        def lines(parts: list[RenderableType]) -> list[list[Segment]]:
            return console.render_lines(Group(*parts), opts) if parts else []

        head, foot = lines(self.header), lines(self.footer)
        room = max(0, height - len(head) - len(foot))
        body = Segment.set_shape(lines(self.body(console, width, room)), width, room)
        for line in (head + body + foot)[:height]:
            yield from line
            yield Segment.line()


class TUI:
    def __init__(self) -> None:
        self.state = State()
        # None until the first connect attempt: the socket is the only way to
        # tell, since a dead daemon's last state is all there is otherwise.
        self.alive: bool | None = None
        self.seen = 0.0          # monotonic time of the last message
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.quit = False
        self.flash = ""
        self.flash_until = 0.0
        # The last pick, until the daemon either reports it or gives up.
        self.requested = ""
        self.requested_at = 0.0
        # The panel's middle-click pin: the homelab readout even while music
        # plays. Kept for this session only.
        self.pinned = False
        # The lyrics in their own script rather than Latin letters, when the
        # daemon has both. Kept across tracks, like the popup's toggle.
        self.original = False
        # Where the user has scrolled the sheet to, as its top row; None while
        # the synced sheet follows the music. A new sheet starts over.
        self.scroll: int | None = None
        self.scrolled_at = 0.0
        self.sheet: tuple = ()
        # The last frame's view of the sheet, for a scroll to start from.
        self.top = 0
        self.room = 0
        # The synced sheet wrapped to the width, kept until either changes:
        # (what it was wrapped from, rows, the row each line starts on).
        self.wrapped: tuple = ((), [], [])
        self.help = False
        # The lyrics are to be opened in the editor before the next frame.
        self.editing = False
        # Lyrics being synced, from s until saved or left.
        self.draft: editor.Draft | None = None
        # The draft's lines wrapped to the width: (the draft itself, how they
        # were wrapped, rows, the row each line starts on). The draft, not
        # its id, which the next one can be given once this one is gone.
        self.sync_wrapped: tuple = (None, None, [], [])
        # A seek asked of the player while syncing, (where to, when), until
        # the daemon's position gets there: a tap before then would be wrong.
        self.seeking: tuple[float, float] | None = None
        # A key that wants pressing again to do what it would lose, and until when.
        self.confirm: tuple[str, float] = ("", 0.0)
        self.accent = PLAIN
        # Off until asked for: it listens, and lights the recording indicator.
        self.visualizer = False
        self.listener = spectrum.Listener()
        self.spectrum: spectrum.Spectrum | None = None
        self.frame_at = 0.0      # when the next frame is due

    def _reader(self) -> None:
        """Follow the daemon, reconnecting whenever it goes away.

        Never starts one: the CLI's default source is `auto`, which opens the
        audio device the moment nothing is playing -- the recording indicator
        the MPRIS setup exists to avoid. Same reasoning as cmd_status.
        """
        while not self.stop.is_set():
            try:
                sock = client.connect(autostart=False)
                for state in client.stream(sock):
                    with self.lock:
                        self.state, self.alive = state, True
                        self.seen = time.monotonic()
                    if self.stop.is_set():
                        return
            except OSError:
                pass
            with self.lock:
                self.alive = False
            if self.stop.wait(RETRY_SECONDS):
                return

    def _link(self) -> str:
        """connecting | down | quiet | up: how the daemon is, by its socket."""
        if self.alive is None:
            return "connecting"
        if not self.alive:
            return "down"
        return "quiet" if time.monotonic() - self.seen > STALE_SECONDS else "up"

    # --- rendering -----------------------------------------------------------
    def _header(self, s: State) -> list[RenderableType]:
        rows: list[RenderableType] = [_line(s.title, TITLE)]
        # The byline the popup and the desktop widget both use.
        byline = "  ·  ".join(part for part in (s.artist, s.album) if part)
        if byline:
            rows.append(_line(byline.upper(), BYLINE))
        about = "  ·  ".join(part for part in (s.player, s.source_label) if part)
        if about:
            rows.append(_line(about, QUIET))
        rows.append(Text(""))
        return rows

    def _progress(self, s: State, pos: float) -> RenderableType:
        # An unknown length has nothing to be a fraction of.
        if s.duration <= 0:
            return _line(_fmt(pos), QUIET)
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_column(no_wrap=True)
        grid.add_row(Text(_fmt(min(pos, s.duration)), style=QUIET),
                     ProgressBar(total=s.duration, completed=min(pos, s.duration),
                                 style=TROUGH, complete_style=self.accent,
                                 finished_style=self.accent),
                     Text(_fmt(s.duration), style=QUIET))
        return grid

    def _synced(self, console: Console, s: State, pos: float,
                width: int, height: int) -> list[Text]:
        current = bisect.bisect_right(s.lyrics, pos + LEAD_IN, key=lambda l: l[0]) - 1
        # Same timings either way, so the current line holds.
        original = self.original and _has_original(s)
        lines = s.lyrics_original if original else s.lyrics
        # The lines themselves, not just how many: lyrics synced or edited
        # again keep their length.
        made = (lines, original, width)
        if self.wrapped[0] != made:
            rows, starts = _wrap(console, [Text(text or "♪") for _, text in lines], width)
            # A blank row either end; the sentinel is where the last line ends.
            self.wrapped = (made, [Text(""), *rows, Text("")],
                            [start + 1 for start in starts] + [len(rows) + 1])
        _, rows, starts = self.wrapped
        if self.scroll is not None and time.monotonic() - self.scrolled_at > RESUME_SECONDS:
            self.scroll = None
        top = 0
        if self.scroll is not None:
            top = self.scroll
        elif current >= 0:
            top = round((starts[current] + starts[current + 1]) / 2 - height * FOLLOW_AT)
        top = self._settle(len(rows), top, height)
        out = [Text("")] * min(height, len(rows))
        for i in range(len(lines)):
            first, last = max(starts[i], top), min(starts[i + 1], top + height)
            if first >= last:
                continue
            # A wrapped line fades as one, by where its middle is.
            style = SUNG if i == current else \
                _fade((starts[i] + starts[i + 1] - 1) / 2 - top, height)
            for r in range(first, last):
                row = rows[r].copy()
                row.style = style
                out[r - top] = row
        return out

    def _plain(self, console: Console, s: State, width: int, height: int) -> list[Text]:
        # No timings: the sheet as published, from the top. Nothing to follow,
        # so it stays wherever it is scrolled to.
        items = [Text(line, style=SHEET) for line in s.lyrics_plain.splitlines()]
        rows, _ = _wrap(console, items, width)
        rows = [Text(""), *rows, Text("")]
        top = self._settle(len(rows), self.scroll or 0, height)
        return rows[top:top + height]

    def _syncing(self, console: Console, s: State, pos: float,
                 width: int, height: int) -> list[Text]:
        """The lyrics being synced, each line after its timing, followed by
        the marker on the line to tap next rather than by the music."""
        d = self.draft
        self.room = height
        if s.key != d.key:
            return self._placeholder(console, f"Syncing {d.tags['ti']}",
                                     "It isn't playing now. Play it again to carry "
                                     "on, or esc to leave.", width, height)
        lines = d.texts if self.original else d.shown
        made = (self.original, width)
        if self.sync_wrapped[0] is not d or self.sync_wrapped[1] != made:
            rows: list[Text] = []
            starts = []
            for text in lines:
                starts.append(len(rows))
                rows.extend(Text(text or "♪").wrap(console, max(1, width - GUTTER))
                            or [Text("")])
            self.sync_wrapped = (d, made, rows, [*starts, len(rows)])
        _, _, rows, starts = self.sync_wrapped
        mark = min(d.cursor, len(lines) - 1)
        top = round((starts[mark] + starts[mark + 1]) / 2 - height * FOLLOW_AT)
        top = max(0, min(top, len(rows) - height))
        # What the timings so far would show: a sync can be watched as it goes.
        sung = d.current(pos + LEAD_IN)
        marked = Style(color=self.accent, bold=True)
        out = []
        for i, t in enumerate(d.times):
            first, last = max(starts[i], top), min(starts[i + 1], top + height)
            if first >= last:
                continue
            style = (marked if i == d.cursor else SUNG if i == sung
                     else SHEET if t is not None else QUIET)
            for r in range(first, last):
                gutter = " " * GUTTER
                if r == starts[i]:
                    timing = _stamp(t) if t is not None else "·"
                    gutter = ("▸ " if i == d.cursor else "  ") + timing.rjust(GUTTER - 4) + "  "
                out.append(Text.assemble((gutter, marked if i == d.cursor else QUIET),
                                         (rows[r].plain, style), no_wrap=True))
        return out

    def _sync_keys(self) -> list[Text]:
        d, accent = self.draft, self.accent
        left = d.untimed()
        return [
            Text("   ").join([_cap("enter", "tap", accent), _cap("backspace", "undo", accent),
                             _cap("↑↓", "line", accent), _cap("←→", "5 s", accent),
                             _cap("0", "from the top", accent)]),
            Text("   ").join([_cap("-", "earlier", accent), _cap("+", "later", accent),
                             _cap("w", "save", accent), _cap("esc", "leave", accent)]),
            Text(f"{left} line{'s'[:left != 1]} to tap" if left
                 else "Every line tapped: w saves them.", style=QUIET),
        ]

    def _settle(self, total: int, top: int, height: int) -> int:
        """Clamp the sheet's top row to what it has, and note it for scrolling."""
        top = max(0, min(top, total - height))
        if self.scroll is not None:
            self.scroll = top   # so scrolling back from past the end is immediate
        self.top, self.room = top, height
        return top

    def _sources(self, pref: str, desktop_listening: bool = False) -> tuple[Text, Text]:
        """Where the daemon gets the track from. Marked by what the daemon
        reports, never by the key pressed, so a switch that didn't take is
        plain to see. desktop_listening: the daemon is listening for the
        desktop widget's visualizer, which costs the same as this one's."""
        if self.requested and (pref == self.requested or
                               time.monotonic() - self.requested_at > SWITCH_SECONDS):
            self.requested = ""
        chosen = Style(color="#1a1a1a", bgcolor=self.accent, bold=True)
        row = Text("  ", justify="center").join(
            _cap(str(n), f" {label} ", self.accent, chosen if value == pref else "")
            for n, (value, label, _) in enumerate(SOURCES, 1))
        note = _cost(pref)
        if self.listener.started() or desktop_listening:
            note = ("The visualizer listens to the speaker output: recording "
                    "indicator on, nothing leaves this machine." if pref == "mpris"
                    else note + " The visualizer listens too, locally.")
        cost = Text("switching…", style=self.accent) if self.requested \
            else Text(note, style=QUIET)
        cost.justify = "center"
        return Text.assemble(("Source  ", QUIET), row), cost

    def _help(self) -> list[RenderableType]:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True, style=Style(color=self.accent, bold=True))
        grid.add_column()
        for key, what in KEYS:
            grid.add_row(key, what)
        grid.add_row("", "")
        grid.add_row("", Text("While syncing", style=TITLE))
        for key, what in SYNC_KEYS:
            grid.add_row(key, what)
        grid.add_row("", "")
        for n, (_, label, hint) in enumerate(SOURCES, 1):
            grid.add_row(f"{n} {label}", Text(hint, style=QUIET))
        return [Text(""), Align.center(grid)]

    def _bars(self, rows: int, width: int) -> list[Text]:
        """The visualizer: bars in eighths of a row, with the peak caps a line
        above them."""
        raw = self.listener.latest()
        count = width // VIS_BAR
        if raw and self.spectrum:
            levels, caps = self.spectrum(raw, count)
        else:
            levels = caps = [0.0] * count
        tops = [round(level * rows * 8) for level in levels]
        # A cap sits in the eighth above its peak; drawn as a sliver at the
        # top or foot of its cell, whichever is nearer, since a cell holds
        # one glyph.
        marks = [min(rows * 8 - 1, round(cap * rows * 8)) for cap in caps]
        # A bar ends in a seven-eighths block, so the gap to the next is a
        # sliver rather than a whole column.
        body = "█" * (VIS_BAR - 1) + "▉"
        # Centred by hand: justify="center" trims each row's trailing spaces
        # first, which would shift the rows against each other and break
        # the bars apart.
        indent = " " * ((width - count * VIS_BAR) // 2)
        cap_style = Style(color=_mix(self.accent, "#ffffff", 0.35))
        out = []
        for r in reversed(range(rows)):
            # A slight gradient, darker at the foot, as Plexamp's bars have.
            bar_style = Style(color=_rgb(self.accent, 0.62 + 0.38 * r / max(1, rows - 1)))
            row = Text(indent, no_wrap=True)
            for top, mark in zip(tops, marks):
                fill = top - r * 8
                if fill >= 8:
                    row.append(body, bar_style)
                elif mark // 8 == r and mark >= top:
                    row.append(("▔" if mark % 8 >= 4 else "▁") * VIS_BAR, cap_style)
                elif fill > 0:
                    row.append(VIS_BLOCKS[fill] * VIS_BAR, bar_style)
                else:
                    row.append(" " * VIS_BAR)
            out.append(row)
        return out

    def _listen(self, wanted: bool) -> None:
        """Keep the visualizer's capture open exactly while it is drawn."""
        if self.listener.died():
            self.listener.stop()
            self.visualizer = False
            self._say("the visualizer lost the speaker output")
        elif wanted and not self.listener.started():
            why = self.listener.start()
            if why:
                self.visualizer = False
                self._say(why)
        elif not wanted and self.listener.started():
            self.listener.stop()

    def _message(self, s: State, link: str) -> tuple[str, str]:
        if link == "connecting":
            return "Connecting…", ""
        if link == "down":
            return "nowplaying is not running", START_HINT
        if link == "quiet":
            return "nowplaying is not responding", \
                f"No word from the daemon in {STALE_SECONDS:.0f} s."
        if not s.title:
            if s.status == "error":
                return "Nothing playing", s.message
            return "Nothing playing", "listening…" if s.status == "searching" else ""
        # The daemon only says why once the lookup has answered.
        return ("No lyrics", s.message) if s.message else ("Looking up lyrics…", "")

    def _placeholder(self, console: Console, text: str, explanation: str,
                     width: int, height: int, style: StyleType = TITLE) -> list[Text]:
        items = [Text(text, style=style)]
        if explanation:
            items.append(Text(explanation, style=QUIET))
        rows, _ = _wrap(console, items, width)
        return [Text("")] * max(0, (height - len(rows)) // 2) + rows

    def _view(self, s: State, link: str) -> str:
        """What the body shows: help | sync | idle | no-idle | synced | plain | message."""
        if self.help:
            return "help"
        # Syncing holds the view, whatever the idle display would do.
        if self.draft is not None and link == "up":
            return "sync"
        # The daemon decides when the idle display takes over (no player, or
        # paused long enough); the pin forces it.
        if link == "up" and (s.idle_active or self.pinned):
            if s.idle_kind:
                return "idle"
            if self.pinned:
                return "no-idle"
        # A dead daemon's last track is not "now playing"; show nothing of it.
        if link == "up" and s.title:
            if s.lyrics:
                return "synced"
            if s.lyrics_plain:
                return "plain"
        return "message"

    def render(self, width: int, height: int) -> Panel:
        with self.lock:
            s, link = self.state, self._link()
        pos = s.position()
        live = link == "up" and bool(s.title)
        view = self._view(s, link)
        self.accent = _accent(s.cover_file) if live else PLAIN
        # Only while a track plays: a still screen isn't worth the indicator.
        self._listen(self.visualizer and live and s.playing)
        bars = self.listener.started() and view != "help"
        # Inside the border and padding.
        beside = bars and width - 6 >= VIS_BESIDE
        sheet = (s.key, len(s.lyrics), view)
        if sheet != self.sheet:
            self.sheet, self.scroll = sheet, None

        def body(console: Console, width: int, height: int) -> list[RenderableType]:
            if not beside:
                return sheet(console, width, height)
            # The lyrics on the left, the visualizer on the right, the height
            # of the whole body so the bars have room to climb.
            right = width * 9 // 20
            left = width - right - 3
            grid = Table.grid()
            grid.add_column(width=left, no_wrap=True)
            grid.add_column(width=3)
            grid.add_column(width=right, no_wrap=True)
            grid.add_row(Group(*sheet(console, left, height)), "",
                         Group(*self._bars(height, right)))
            return [grid]

        def sheet(console: Console, width: int, height: int) -> list[RenderableType]:
            if view == "help":
                return self._help()
            if view == "sync":
                return self._syncing(console, s, pos, width, height)
            if view == "idle":
                # Only shout when something is actually wrong.
                return self._placeholder(console, s.idle_line1, s.idle_line2,
                                         width, height,
                                         Style(color=TITLE.color if s.idle_ok else ALARM,
                                               bold=True))
            if view == "no-idle":
                return self._placeholder(console, "No homelab readout",
                                         "The daemon has none to show.",
                                         width, height)
            if view == "synced":
                return self._synced(console, s, pos, width, height)
            if view == "plain":
                return self._plain(console, s, width, height)
            return self._placeholder(console, *self._message(s, link), width, height)

        header = self._header(s) if live else []
        footer: list[RenderableType] = [Text("")]
        if bars and not beside:
            footer += self._bars(min(8, max(3, (height - 2) // 6)), width - 6)
        if live:
            footer.append(self._progress(s, pos))
        buttons = []
        # Only when there's a local player to obey them: a Plex client on
        # another device is out of playerctl's reach.
        if live and s.player:
            buttons.append(Text("   ").join([
                _cap("p", "previous", self.accent),
                _cap("space", "pause" if s.playing else "play", self.accent),
                _cap("n", "next", self.accent),
            ]))
        # Not just while a track plays: nothing playing is exactly when
        # someone reaches for a different source. An older daemon doesn't
        # publish its source.
        cost = None
        # Syncing needs the room for its keys more than the source switch.
        if link == "up" and s.source_pref and view != "sync":
            sources, cost = self._sources(s.source_pref, s.vis_listening)
            buttons.append(sources)
        gap = 8
        # One row when they fit side by side, inside the border and padding.
        if sum(b.cell_len for b in buttons) + gap * (len(buttons) - 1) <= width - 6:
            buttons = [Text(" " * gap).join(buttons)] if buttons else []
        if view == "sync":
            buttons += self._sync_keys()
        for row in buttons:
            row.justify = "center"
            footer.append(row)
        if cost is not None:
            footer.append(cost)
        return Panel(
            _Fill(header, body, footer),
            # Given outright: under Live's alt screen the height never reaches
            # the panel, and the body is cut to fit it.
            height=height,
            title="nowplaying",
            subtitle=self._subtitle(s, link, view),
            border_style=self._border(s, live, view),
            padding=(0, 2),
        )

    def _border(self, s: State, live: bool, view: str) -> str:
        # Only shout when something is actually wrong.
        if view == "idle" and not s.idle_ok:
            return ALARM
        if not live:
            return IDLE_BORDER
        return self.accent if s.playing else _rgb(self.accent, 0.55)

    def _subtitle(self, s: State, link: str, view: str) -> Text:
        if time.monotonic() < self.flash_until:
            return Text(self.flash, style="#e5c07b")
        hints = []
        if self._scripts(s, view):
            hints.append(("o", "Latin letters" if self.original else "original script"))
        if view == "sync":
            pass   # its keys are in the footer
        elif self.pinned:
            hints.append(("h", "unpin"))
        elif link == "up" and s.idle_kind:
            hints.append(("h", "homelab"))
        if view in ("synced", "plain", "message") and link == "up" and s.title:
            hints.append(("v", "hide visualizer" if self.visualizer else "visualizer"))
            if s.lyrics_file:
                hints.append(("e", "edit lyrics"))
                if view != "message":
                    hints.append(("s", "sync"))
        hints += [("?", "close" if self.help else "keys"), ("q", "quit")]
        # Styled piece by piece: the panel lays a subtitle's own style over
        # all of it, which would wash out the keys.
        return Text("   ").join(_cap(key, label, self.accent, QUIET) for key, label in hints)

    # --- keys --------------------------------------------------------------
    def _say(self, text: str) -> None:
        self.flash, self.flash_until = text, time.monotonic() + FLASH_SECONDS

    def _control(self, *command: str) -> bool:
        """Hand a transport command to playerctl. The daemon notices the
        change on its next poll, so no state is touched here."""
        with self.lock:
            s, link = self.state, self._link()
        if link != "up":
            self._say("the daemon is not running")
            return False
        if not s.player:
            self._say("nothing local to control")
            return False
        try:
            proc = subprocess.Popen(["playerctl", "--player", s.player, *command],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            self._say("playerctl is not installed")
            return False
        threading.Thread(target=proc.wait, daemon=True).start()   # reap it
        return True

    def _pick_source(self, value: str) -> None:
        """Save the pick where the daemon looks for it, as the popup does:
        written whole and renamed into place, so it never reads half a word."""
        with self.lock:
            s, link = self.state, self._link()
        if link != "up" or not s.source_pref:
            self._say("the daemon is not running" if link != "up"
                      else "this daemon can't switch sources")
            return
        # Already there -- unless another pick is still on its way, which this
        # one has to overwrite or the daemon will go on and take it.
        if value == s.source_pref and not self.requested:
            return
        target = config.source_file()
        tmp = target.with_suffix(".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(value)
            tmp.replace(target)
        except OSError as exc:
            self._say(f"couldn't save the source: {exc.strerror}")
            return
        self.requested, self.requested_at = value, time.monotonic()

    def _toggle_visualizer(self) -> None:
        if not self.visualizer and self.spectrum is None:
            try:
                self.spectrum = spectrum.Spectrum()
            except ImportError:
                self._say("the visualizer needs numpy")
                return
        self.visualizer = not self.visualizer

    def _ask_edit(self) -> None:
        """Open the lyrics in the editor, once this frame is done -- unless
        there's nothing to open, which is said without leaving the view."""
        with self.lock:
            s, link = self.state, self._link()
        if link != "up":
            self._say("the daemon is not running")
            return
        try:
            editor.target(s)
        except editor.LyricsError as exc:
            self._say(str(exc))
            return
        self.editing = True

    def _edit(self, live: Live, fd: int | None) -> None:
        """Hand the terminal to the editor, and take it back after. The
        daemon notices a save on its next tick and sends the new lyrics."""
        self.editing = False
        with self.lock:
            s = self.state
        live.stop()
        try:
            with _cooked(fd):
                said = editor.edit(s)
        except editor.LyricsError as exc:
            said = str(exc)
        finally:
            live.start()
        self._say(said)

    def _start_sync(self) -> None:
        with self.lock:
            s, link = self.state, self._link()
        if link != "up":
            self._say("the daemon is not running")
            return
        try:
            self.draft = editor.Draft.start(s, s.position())
        except editor.LyricsError as exc:
            self._say(str(exc))
            return
        self.help = False
        self.seeking = None
        # A second press the last draft was waiting for isn't this one's.
        self.confirm = ("", 0.0)
        self._say("tap enter as each line starts")

    def _sync_key(self, key: str) -> bool:
        """Do what `key` means while syncing; False for the keys that mean
        what they always do."""
        d = self.draft
        page = max(1, self.room // 2)
        moves = {"up": -1, "k": -1, "down": 1, "j": 1, "pgup": -page, "pgdn": page,
                 "home": -len(d.times), "g": -len(d.times),
                 "end": len(d.times), "G": len(d.times)}
        if key == "enter":
            self._tap()
        elif key == "backspace":
            d.untap()
        elif key in moves:
            d.move(moves[key])
        elif key in ("left", "right"):
            self._seek(step=SEEK_STEP if key == "right" else -SEEK_STEP)
        elif key == "0":
            self._seek(step=-math.inf)
        elif key in ("-", "_", "+", "="):
            d.shift(-SHIFT_STEP if key in ("-", "_") else SHIFT_STEP)
            self._say(f"every line {abs(d.shifted):.1f} s "
                      f"{'later' if d.shifted > 0 else 'earlier'}" if d.shifted
                      else "every line back where it was")
        elif key == "w":
            try:
                said = d.save()
            except editor.LyricsError as exc:
                self._say(str(exc))
                return True
            self.draft = None
            self._say(said)
        elif key in ("esc", "s"):
            if not d.changed or self._twice(key, f"not saved: w saves it, {key} again drops it"):
                self.draft = None
        elif key in ("q", "Q"):
            if not d.changed or self._twice("q", "not saved: w saves it, q again quits"):
                self.quit = True
        elif key == "e":
            self._say("save the sync or leave it first")
        else:
            return False
        return True

    def _twice(self, key: str, warning: str) -> bool:
        """Whether `key` is pressed again while its warning shows; the
        first press only gives the warning."""
        pending, until = self.confirm
        if pending == key and time.monotonic() < until:
            self.confirm = ("", 0.0)
            return True
        self.confirm = (key, time.monotonic() + FLASH_SECONDS)
        self._say(warning)
        return False

    def _tap(self) -> None:
        with self.lock:
            s, link = self.state, self._link()
        if link != "up" or s.key != self.draft.key:
            self._say("that track isn't playing")
            return
        if not self._settled(s):
            self._say("waiting for the player to get there")
            return
        clash = self.draft.tap(s.position())
        if clash:
            self._say(f"{clash} line{'s'[:clash != 1]} out of order with that: "
                      "tap again, or backspace")

    def _seek(self, step: float) -> None:
        """Move the track `step` seconds, to tap a stretch again. By so much
        rather than to a position: some players (VLC) only take the first."""
        with self.lock:
            s = self.state
        if s.key != self.draft.key:
            self._say("that track isn't playing")
            return
        pos = s.position()
        if not self._settled(s):
            # Seeking already, and the daemon's position is from before:
            # this one goes on from where that one is landing.
            target, since = self.seeking
            pos = target + (time.monotonic() - since if s.playing else 0.0)
        where = max(0.0, pos + step)
        if s.duration > 0:
            where = min(where, s.duration)
        if where == pos:
            return
        by = where - pos
        if self._control("position", f"{abs(by):.3f}{'+' if by > 0 else '-'}"):
            self.seeking = (where, time.monotonic())

    def _settled(self, s: State) -> bool:
        """The daemon's position has caught up with the last seek, which
        takes it a poll or two."""
        if self.seeking is None:
            return True
        where, since = self.seeking
        elapsed = time.monotonic() - since
        if abs(s.position() - (where + (elapsed if s.playing else 0.0))) < SEEK_TOLERANCE \
                or elapsed > SEEK_SECONDS:
            self.seeking = None
            return True
        return False

    def _scripts(self, s: State, view: str) -> bool:
        """The lyrics showing come in their own script too, line for line."""
        if view == "sync":
            return self.draft.texts != self.draft.shown
        return view == "synced" and _has_original(s)

    def _toggle_original(self) -> None:
        with self.lock:
            s, link = self.state, self._link()
        if not self._scripts(s, self._view(s, link)):
            self._say("no original script for this track")
            return
        self.original = not self.original

    def _scroll(self, key: str) -> None:
        with self.lock:
            s, link = self.state, self._link()
        if self._view(s, link) not in ("synced", "plain"):
            return
        page = max(1, self.room - 2)
        step = {"up": -1, "k": -1, "down": 1, "j": 1, "pgup": -page, "pgdn": page}
        if key in ("home", "g"):
            self.scroll = 0
        elif key in ("end", "G"):
            self.scroll = sys.maxsize   # clamped to the last page on the next frame
        else:
            self.scroll = (self.top if self.scroll is None else self.scroll) + step[key]
        self.scrolled_at = time.monotonic()

    def _key(self, key: str) -> None:
        if key == "?" or (key == "esc" and self.help):
            self.help = not self.help
        elif self.draft is not None and self._sync_key(key):
            return
        elif key in ("q", "Q"):
            self.quit = True
        elif key == " ":
            self._control("play-pause")
        elif key == "n":
            self._control("next")
        elif key == "p":
            self._control("previous")
        elif key == "h":
            self.pinned = not self.pinned
        elif key == "v":
            self._toggle_visualizer()
        elif key == "o":
            self._toggle_original()
        elif key == "e":
            self._ask_edit()
        elif key == "s":
            self._start_sync()
        elif key in ("1", "2", "3", "4"):
            self._pick_source(SOURCES[int(key) - 1][0])
        elif key in ("up", "down", "pgup", "pgdn", "home", "end", "j", "k", "g", "G"):
            self._scroll(key)

    def _wait(self, fd: int | None) -> None:
        """Sit out the rest of the frame, handling any keys pressed meanwhile.

        Paced from the frame's start rather than its end, so the time spent
        drawing doesn't slow the rate down.
        """
        frame = 1 / (VIS_FPS if self.listener.started() else FPS)
        now = time.monotonic()
        # Fallen behind (a slow frame, a suspended terminal): start afresh
        # rather than rush to catch up.
        self.frame_at = max(self.frame_at + frame, now)
        pause = self.frame_at - now
        if fd is None:
            time.sleep(pause)
            return
        ready, _, _ = select.select([fd], [], [], pause)
        if not ready:
            return
        data = os.read(fd, 1024)
        if not data:   # the terminal went away
            self.quit = True
            return
        for key in _keys(data.decode(errors="ignore")):
            self._key(key)

    def run(self) -> int:
        console = Console()
        threading.Thread(target=self._reader, daemon=True).start()
        # A plain `kill` would otherwise skip every finally and leave the
        # terminal in cbreak mode; exiting unwinds through them instead.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            # Live is left first, so the screen is back before the keyboard is.
            with _keyboard() as fd, Live(console=console, screen=True,
                                         auto_refresh=False) as live:
                while not self.quit:
                    if self.editing:
                        self._edit(live, fd)
                    live.update(self.render(*console.size), refresh=True)
                    self._wait(fd)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            self.listener.stop()
        return 0


def main() -> int:
    return TUI().run()
