"""Terminal version of the panel widget's popup.

Renders the same state the applet does, read from the daemon's socket rather
than its state file: the track, the whole lyrics sheet following the line being
sung, and the track's progress.
"""
from __future__ import annotations

import bisect
import contextlib
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

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from . import client, config
from .state import State

FPS = 15
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
START_HINT = "start it with: nowplaying daemon --source mpris"
FLASH_SECONDS = 3.0      # how long a key's "can't do that" note stays up
# A pick not reported back by then didn't take. Longer than the daemon's
# slowest tick, a fingerprint lookup.
SWITCH_SECONDS = 15.0

# The popup's source buttons, in its order, picked outright with 1-4. No key
# steps through them: going from Player to Mic that way would pass through
# two sources that listen.
SOURCES = (("mpris", "Player"), ("auto", "Auto"), ("loopback", "Speaker"), ("mic", "Mic"))

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
    t = max(0, int(seconds))
    h, m, s = t // 3600, t // 60 % 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cap(key: str, label: str, style: str = "") -> Text:
    """A key and what it does, as the button it stands in for."""
    return Text.assemble((key, "bold magenta"), " ", (label, style))


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
                 body: Callable[[Console, int, int], list[Text]],
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
        rows: list[RenderableType] = [_line(s.title, "bold")]
        if s.artist:
            rows.append(_line(s.artist))
        if s.album:
            rows.append(_line(s.album, "dim"))
        rows.append(Rule(style="dim"))
        return rows

    def _progress(self, s: State, pos: float) -> RenderableType:
        # An unknown length has nothing to be a fraction of.
        if s.duration <= 0:
            return _line(_fmt(pos), "dim")
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_column(no_wrap=True)
        grid.add_row(Text(_fmt(min(pos, s.duration)), style="dim"),
                     ProgressBar(total=s.duration, completed=min(pos, s.duration),
                                 complete_style="magenta", finished_style="magenta"),
                     Text(_fmt(s.duration), style="dim"))
        return grid

    def _synced(self, console: Console, s: State, pos: float,
                width: int, height: int) -> list[Text]:
        current = bisect.bisect_right(s.lyrics, pos + LEAD_IN, key=lambda l: l[0]) - 1
        items = [Text(text or "♪", style="bold" if i == current else "dim")
                 for i, (_, text) in enumerate(s.lyrics)]
        rows, starts = _wrap(console, items, width)
        rows = [Text(""), *rows, Text("")]
        top = 0
        if current >= 0:
            start = starts[current] + 1
            end = starts[current + 1] + 1 if current + 1 < len(starts) else len(rows) - 1
            top = round((start + end) / 2 - height * FOLLOW_AT)
        top = max(0, min(top, len(rows) - height))
        return rows[top:top + height]

    def _plain(self, console: Console, s: State, width: int, height: int) -> list[Text]:
        # No timings: the sheet as published, from the top.
        items = [Text(line) for line in s.lyrics_plain.splitlines()]
        rows, _ = _wrap(console, items, width)
        return [Text(""), *rows][:height]

    def _sources(self, pref: str) -> list[RenderableType]:
        """Where the daemon gets the track from. Marked by what the daemon
        reports, never by the key pressed, so a switch that didn't take is
        plain to see."""
        if self.requested and (pref == self.requested or
                               time.monotonic() - self.requested_at > SWITCH_SECONDS):
            self.requested = ""
        row = Text("  ", justify="center").join(
            _cap(str(n), f" {label} ", "reverse" if value == pref else "")
            for n, (value, label) in enumerate(SOURCES, 1))
        cost = "switching…" if self.requested else _cost(pref)
        return [Text.assemble(("Source  ", "dim"), row, justify="center"),
                Text(cost, style="dim", justify="center")]

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
                     width: int, height: int, style: str = "bold") -> list[Text]:
        items = [Text(text, style=style)]
        if explanation:
            items.append(Text(explanation, style="dim"))
        rows, _ = _wrap(console, items, width)
        return [Text("")] * max(0, (height - len(rows)) // 2) + rows

    def render(self, height: int) -> Panel:
        with self.lock:
            s, link = self.state, self._link()
        pos = s.position()
        # A dead daemon's last track is not "now playing"; show nothing of it.
        live = link == "up" and bool(s.title)

        def body(console: Console, width: int, height: int) -> list[Text]:
            # The daemon decides when the idle display takes over (no player,
            # or paused long enough); the pin forces it.
            if link == "up" and (s.idle_active or self.pinned):
                if s.idle_kind:
                    # Only shout when something is actually wrong.
                    return self._placeholder(console, s.idle_line1, s.idle_line2,
                                             width, height,
                                             "bold" if s.idle_ok else "bold red")
                if self.pinned:
                    return self._placeholder(console, "No homelab readout",
                                             "The daemon has none to show.",
                                             width, height)
            if live and s.lyrics:
                return self._synced(console, s, pos, width, height)
            if live and s.lyrics_plain:
                return self._plain(console, s, width, height)
            return self._placeholder(console, *self._message(s, link), width, height)

        header = self._header(s) if live else []
        footer: list[RenderableType] = []
        if live:
            footer += [Rule(style="dim"), self._progress(s, pos)]
        # Only when there's a local player to obey them: a Plex client on
        # another device is out of playerctl's reach.
        if live and s.player:
            footer.append(Text("   ", justify="center").join([
                _cap("p", "previous"),
                _cap("space", "pause" if s.playing else "play"),
                _cap("n", "next"),
            ]))
        # Not just while a track plays: nothing playing is exactly when
        # someone reaches for a different source. An older daemon doesn't
        # publish its source.
        if link == "up" and s.source_pref:
            footer += [Rule(style="dim"), *self._sources(s.source_pref)]
        return Panel(
            _Fill(header, body, footer),
            # Given outright: under Live's alt screen the height never reaches
            # the panel, and the body is cut to fit it.
            height=height,
            title="nowplaying",
            subtitle=self._subtitle(s, link),
            border_style="magenta" if live and s.playing else "grey35",
            padding=(0, 1),
        )

    def _subtitle(self, s: State, link: str) -> Text:
        if time.monotonic() < self.flash_until:
            return Text(self.flash, style="yellow")
        caps = []
        if self.pinned:
            caps.append(_cap("h", "unpin"))
        elif link == "up" and s.idle_kind:
            caps.append(_cap("h", "homelab"))
        caps.append(_cap("q", "quit"))
        return Text("   ", style="dim").join(caps)

    # --- keys --------------------------------------------------------------
    def _say(self, text: str) -> None:
        self.flash, self.flash_until = text, time.monotonic() + FLASH_SECONDS

    def _control(self, verb: str) -> None:
        """Hand a transport command to playerctl. The daemon notices the
        change on its next poll, so no state is touched here."""
        with self.lock:
            s, link = self.state, self._link()
        if link != "up":
            self._say("the daemon is not running")
            return
        if not s.player:
            self._say("nothing local to control")
            return
        try:
            proc = subprocess.Popen(["playerctl", "--player", s.player, verb],
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            self._say("playerctl is not installed")
            return
        threading.Thread(target=proc.wait, daemon=True).start()   # reap it

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

    def _key(self, key: str) -> None:
        if key in ("q", "Q"):
            self.quit = True
        elif key == " ":
            self._control("play-pause")
        elif key == "n":
            self._control("next")
        elif key == "p":
            self._control("previous")
        elif key == "h":
            self.pinned = not self.pinned
        elif key in ("1", "2", "3", "4"):
            self._pick_source(SOURCES[int(key) - 1][0])

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
