"""The desktop widget's visualizer, served by the daemon.

QML can neither capture audio nor speak a unix socket, but it can stream an
HTTP response from localhost. So while the widget's visualizer is on it asks
for http://127.0.0.1:<port>/<token>/<bars>, and the daemon answers with one
line of bar heights a frame -- listening to the speaker output only while
somebody is asking.

The port is random and the token a secret, both written to a file in the
runtime directory that only this user can read: a web page can reach
localhost too, and must not be able to switch the capture on by guessing.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import secrets
from collections import Counter
from collections.abc import Callable

from . import config, spectrum

log = logging.getLogger("nowplaying.visualizer")

# Each answer ends after this long and the widget asks again: QML hands over
# the whole response so far on every frame, so it has to stay short.
STREAM_SECONDS = 2.0
# Keep listening this long after the last viewer goes, to ride over those
# reconnects without restarting parec each time.
LINGER_SECONDS = 3.0
MIN_BARS, MAX_BARS = 4, 192
# A bar's height as one printable character, 0 to LEVELS - 1 above "0".
LEVELS = 64


def _encode(levels, caps) -> bytes:
    """One frame: the bars' heights, then their caps', a character each."""
    def chars(values) -> str:
        return "".join(chr(48 + round(float(v) * (LEVELS - 1))) for v in values)
    return (chars(levels) + chars(caps) + "\n").encode()


class Feed:
    def __init__(self, on_change: Callable[[], None]) -> None:
        # Called when listening starts or stops, so the daemon can say so.
        self.on_change = on_change
        self.token = secrets.token_urlsafe(18)
        self.listener = spectrum.Listener()
        self.server: asyncio.Server | None = None
        self.endpoint = ""
        # Viewers per bar count; each count keeps one Spectrum across
        # reconnects, or its levelling and caps would restart every answer.
        self.wanted: Counter[int] = Counter()
        self.spectra: dict[int, spectrum.Spectrum] = {}
        self.frames: dict[int, bytes] = {}
        self._frame = asyncio.Event()
        self._ticker: asyncio.Task | None = None
        self._linger: asyncio.TimerHandle | None = None
        self._starting: asyncio.Lock = asyncio.Lock()

    @property
    def listening(self) -> bool:
        return self.listener.started()

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}/{self.token}"
        path = config.vis_endpoint_path()
        tmp = path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.endpoint + "\n")
            tmp.replace(path)
        except OSError as e:
            log.warning("visualizer unavailable: can't write %s: %s", path, e)
            return
        log.info("visualizer on port %d", port)

    def close(self) -> None:
        """Stop listening and forget the endpoint. Safe from a signal handler:
        nothing here waits."""
        if self.server is not None:
            self.server.close()
        if self.listener.proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self.listener.proc.kill()
        # Only our own file: a daemon started since may have written its own.
        path = config.vis_endpoint_path()
        with contextlib.suppress(OSError):
            if self.endpoint and path.read_text().strip() == self.endpoint:
                path.unlink()

    # --- listening -----------------------------------------------------------
    async def _see(self) -> str:
        """A viewer arrived: be listening. Why not, or "" when listening."""
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        async with self._starting:
            if self.listener.died():
                await self._stop()
            if not self.listening:
                loop = asyncio.get_running_loop()
                why = await loop.run_in_executor(None, self.listener.start)
                if why:
                    return why
                log.info("visualizer listening to the speaker output")
                self.on_change()
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick())
        return ""

    def _leave(self, bars: int) -> None:
        self.wanted[bars] -= 1
        if self.wanted[bars] <= 0:
            del self.wanted[bars]
        if not self.wanted and self._linger is None:
            loop = asyncio.get_running_loop()
            self._linger = loop.call_later(
                LINGER_SECONDS, lambda: asyncio.create_task(self._stop()))

    async def _stop(self) -> None:
        self._linger = None
        if self.wanted and not self.listener.died():
            return   # someone came back in the meantime
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        self._frame.set()   # anyone still waiting on a frame: hang up
        was = self.listening
        await asyncio.get_running_loop().run_in_executor(None, self.listener.stop)
        self.spectra.clear()
        self.frames.clear()
        if was:
            log.info("visualizer stopped listening")
            self.on_change()

    async def _tick(self) -> None:
        """Work each frame out once, however many are watching it."""
        loop = asyncio.get_running_loop()
        period = 1 / spectrum.FPS
        due = loop.time()
        while self.listening and not self.listener.died():
            raw = self.listener.latest()
            for bars in list(self.wanted):
                if raw is None:
                    self.frames[bars] = _encode([0] * bars, [0] * bars)
                    continue
                spec = self.spectra.get(bars)
                if spec is None:
                    spec = self.spectra[bars] = spectrum.Spectrum()
                self.frames[bars] = _encode(*spec(raw, bars))
            self._frame.set()
            self._frame = asyncio.Event()
            due += period
            await asyncio.sleep(max(0.0, due - loop.time()))
        # parec went away: wake the viewers so they hang up and ask again.
        self._frame.set()

    # --- HTTP ----------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        bars = 0
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError):
                return
            method, _, rest = head.decode("latin-1").partition(" ")
            path = rest.partition(" ")[0]
            parts = path.split("/")
            if (method != "GET" or len(parts) != 3
                    or not hmac.compare_digest(parts[1].encode("latin-1"),
                                               self.token.encode())):
                await self._refuse(writer, "403 Forbidden", "")
                return
            try:
                wanted = int(parts[2])
            except ValueError:
                await self._refuse(writer, "400 Bad Request", "bars must be a number")
                return
            bars = max(MIN_BARS, min(MAX_BARS, wanted))
            self.wanted[bars] += 1
            why = await self._see()
            if why:
                await self._refuse(writer, "503 Service Unavailable", why)
                return
            writer.write(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Type: text/plain; charset=us-ascii\r\n"
                         b"Cache-Control: no-store\r\n"
                         b"Connection: close\r\n\r\n")
            loop = asyncio.get_running_loop()
            ends = loop.time() + STREAM_SECONDS
            while loop.time() < ends and self.listening:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._frame.wait(), 1)
                if bars in self.frames:
                    writer.write(self.frames[bars])
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if bars:
                self._leave(bars)
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, status: str, why: str) -> None:
        body = (why + "\n").encode() if why else b""
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                     .encode() + body)
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()
