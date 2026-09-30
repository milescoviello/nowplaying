"""Terminal karaoke view."""
from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Iterator

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from . import client
from .state import State

FPS = 15
CONTEXT = 3  # lyric lines shown either side of the current one
RETRY_SECONDS = 2.0
NOT_RUNNING = "not running — start it with: nowplaying daemon --source mpris"


# Escape sequences (arrows, function keys) are swallowed whole, so their
# trailing letters can't be misread as key presses.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.)")


def _keys(data: str) -> list[str]:
    keys, i = [], 0
    while i < len(data):
        m = _ESCAPE.match(data, i)
        if m:
            i = m.end()
            continue
        keys.append("esc" if data[i] == "\x1b" else data[i])
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


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class TUI:
    def __init__(self) -> None:
        self.state = State()
        # None until the first connect attempt: the socket is the only way to
        # tell, since a dead daemon's last state is all there is otherwise.
        self.alive: bool | None = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.quit = False

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
                    if self.stop.is_set():
                        return
            except OSError:
                pass
            with self.lock:
                self.alive = False
            if self.stop.wait(RETRY_SECONDS):
                return

    # --- rendering -----------------------------------------------------------
    def _header(self, s: State, pos: float) -> Text:
        if s.title:
            head = Text()
            head.append("♪ ", style="bold magenta")
            head.append(s.artist or "Unknown artist", style="bold cyan")
            head.append("  —  ")
            head.append(s.title, style="bold white")
            if s.duration:
                head.append(f"   {_fmt(pos)} / {_fmt(s.duration)}", style="dim")
            else:
                head.append(f"   {_fmt(pos)}", style="dim")
            return head
        label = {
            "idle": "waiting for audio…",
            "searching": "listening…",
            "paused": "paused",
            "error": "error",
        }.get(s.status, s.status)
        return Text(f"♪ {label}", style="bold yellow")

    def _lyrics(self, s: State, pos: float, height: int) -> Group:
        if s.lyrics:
            idx = -1
            lo, hi = 0, len(s.lyrics) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if s.lyrics[mid][0] <= pos:
                    idx, lo = mid, mid + 1
                else:
                    hi = mid - 1
            span = max(1, min(CONTEXT, (height - 4) // 2))
            start = max(0, idx - span)
            end = min(len(s.lyrics), idx + span + 2)
            rows = []
            for i in range(start, end):
                text = s.lyrics[i][1] or "♪"
                if i == idx:
                    rows.append(Align.center(
                        Text(text, style="bold white on grey19")))
                else:
                    distance = abs(i - idx)
                    style = "grey62" if distance == 1 else "grey42" if distance == 2 else "grey30"
                    rows.append(Align.center(Text(text, style=style)))
            if idx < 0:
                rows.insert(0, Align.center(Text("…", style="dim")))
            return Group(*rows)

        if s.lyrics_plain:
            body = Text(s.lyrics_plain, style="grey62")
            return Group(Align.center(Text("(unsynced lyrics)", style="dim yellow")),
                         Text(""), body)

        msg = {
            "playing": s.message or "no lyrics for this track",
            "searching": "listening for a match…",
            "idle": "no audio detected",
            "paused": "audio stopped",
            "error": s.message,
        }.get(s.status, s.message or "…")
        return Group(Align.center(Text(msg, style="dim")))

    def _footer(self, s: State) -> Text:
        bits = []
        if s.source_label:
            bits.append(s.source_label)
        if s.lyrics_source:
            bits.append(f"{s.lyrics_source}{' · synced' if s.lyrics_synced else ''}")
        if s.confidence:
            bits.append(s.confidence)
        if s.message and s.status == "playing":
            bits.append(s.message)
        bits.append("q to quit")
        return Text(" · ".join(bits), style="dim")

    def render(self, height: int) -> Panel:
        with self.lock:
            s = self.state
            pos = s.position()
            alive = self.alive
        if not alive:
            label = "connecting…" if alive is None else NOT_RUNNING
            return Panel(Align.center(Text(f"♪ {label}", style="bold yellow")),
                         title="nowplaying", subtitle=Text("q to quit", style="dim"),
                         border_style="grey35", padding=(1, 2))
        return Panel(
            Group(
                Align.center(self._header(s, pos)),
                Text(""),
                self._lyrics(s, pos, height),
            ),
            title="nowplaying",
            subtitle=self._footer(s),
            border_style="magenta" if s.status == "playing" else "grey35",
            padding=(1, 2),
        )

    # --- keys --------------------------------------------------------------
    def _key(self, key: str) -> None:
        if key in ("q", "Q"):
            self.quit = True

    def _wait(self, fd: int | None) -> None:
        """Sit out one frame, handling any keys pressed meanwhile."""
        if fd is None:
            time.sleep(1 / FPS)
            return
        ready, _, _ = select.select([fd], [], [], 1 / FPS)
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
                    live.update(self.render(console.size.height), refresh=True)
                    self._wait(fd)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
        return 0


def main() -> int:
    return TUI().run()
