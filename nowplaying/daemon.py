"""The detection daemon: owns capture + recognition, broadcasts state to UIs."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request

from . import audio, config, enrich, fleet, lyrics as lyrics_mod, mpris
from .recognizer import Match, Recognizer
from .state import State

log = logging.getLogger("nowplaying.daemon")


class Daemon:
    def __init__(self, source_pref: str = "auto", verbose: bool = False) -> None:
        self.source_pref = source_pref
        self.verbose = verbose
        self.state = State()
        self.recognizer = Recognizer()
        self.clients: set[asyncio.StreamWriter] = set()
        self.clip_path = config.cache_dir() / "clip.wav"

        self._last_verify = 0.0
        self._mpris_key = ""
        self._paused_since = 0.0
        self._last_src_pos = -1.0
        self._stream: audio.StreamCapture | None = None
        # (position, wall) of a measurement that disagreed with the clock and is
        # waiting on a second opinion before we act on it.
        self._pending_resync: tuple[float, float] | None = None
        self._resume_check = False

    # --- client plumbing -----------------------------------------------------
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        try:
            writer.write(self._encode())
            await writer.drain()
            # Clients are read-only; wait for EOF so we notice disconnects.
            while await reader.readline():
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    def _encode(self) -> bytes:
        return (json.dumps(self.state.to_dict()) + "\n").encode()

    def _write_state_file(self) -> None:
        """Mirror state to disk for the QML applet (written atomically)."""
        data = self.state.to_dict()
        data["written_at"] = time.time()
        data["position"] = round(self.state.position(), 3)
        target = config.state_file()
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            tmp.replace(target)
        except OSError:
            pass

    async def broadcast(self) -> None:
        self._write_state_file()
        if not self.clients:
            return
        payload = self._encode()
        for writer in list(self.clients):
            try:
                writer.write(payload)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError):
                self.clients.discard(writer)

    # --- state helpers -------------------------------------------------------
    def _set_status(self, status: str, message: str = "") -> None:
        self.state.status = status
        self.state.message = message

    def _clear_track(self, status: str = "searching", message: str = "") -> None:
        s = self.state
        s.key = s.title = s.artist = s.album = s.cover = s.cover_file = ""
        s.lyrics = []
        s.lyrics_plain = ""
        s.lyrics_synced = False
        s.lyrics_source = ""
        s.duration = 0.0
        s.playing = False
        s.anchor_pos = 0.0
        s.confidence = ""
        self._pending_resync = None
        self._set_status(status, message)

    def _anchor(self, position: float, wall: float) -> None:
        self.state.anchor_pos = max(0.0, position)
        self.state.anchor_wall = wall
        self.state.playing = True
        self.state.confidence = "anchored"

    def _download_cover(self, url: str) -> str:
        """Cache the album art locally. Returns a path, or "" on failure."""
        if not url:
            return ""
        name = hashlib.sha256(url.encode()).hexdigest()[:32] + ".jpg"
        dest = config.covers_dir() / name
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                data = resp.read()
            if not data:
                return ""
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return str(dest)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("cover download failed: %s", exc)
            return ""

    async def _load_cover(self, url: str) -> None:
        loop = asyncio.get_running_loop()
        self.state.cover_file = await loop.run_in_executor(
            None, self._download_cover, url)

    async def _load_lyrics(self, match: Match) -> None:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: lyrics_mod.fetch(match.artist, match.title, match.album,
                                     self.state.duration or None),
        )
        s = self.state
        s.lyrics = result.lines
        s.lyrics_synced = result.synced
        s.lyrics_plain = result.plain
        s.lyrics_source = result.source
        # LRCLIB knows the track length; Shazam does not. Use it for the
        # progress readout and for noticing when the track has run out.
        if result.duration:
            s.duration = result.duration
        if not result.available:
            s.message = "no lyrics found on LRCLIB"
        elif not result.synced:
            s.message = "unsynced lyrics only"
        else:
            s.message = ""

    # --- recognition ---------------------------------------------------------
    def _ensure_stream(self) -> audio.StreamCapture | None:
        """One capture stream, held open, so the recording indicator stays
        steady instead of blinking once per probe."""
        source, reason = audio.resolve_source(self.source_pref)
        if not source:
            self._set_status("error", "no audio source available")
            return None
        self.state.source_label = reason
        if self._stream is not None and (self._stream.source != source
                                         or not self._stream.alive()):
            self._stream.stop()
            self._stream = None
        if self._stream is None:
            self._stream = audio.StreamCapture(source)
            if not self._stream.start():
                self._stream = None
                self._set_status("error", "could not open the audio device")
                return None
            log.info("capture stream open on %s (%s)", source, reason)
        return self._stream

    def _stop_stream(self) -> None:
        if self._stream is not None:
            log.info("closing capture stream")
            self._stream.stop()
            self._stream = None

    async def _recognise_now(self) -> None:
        stream = self._ensure_stream()
        if stream is None:
            return
        clip = stream.snapshot(config.CLIP_SECONDS)
        if clip is None:
            return  # buffer still filling
        if clip.rms < config.SILENCE_RMS:
            self._go_silent()
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, audio.write_wav, clip.raw, self.clip_path)

        self._last_verify = time.monotonic()
        try:
            match = await self.recognizer.recognize_file(self.clip_path)
        except Exception as exc:  # network hiccup, API shape change, ...
            log.warning("recognition failed: %s", exc)
            self._set_status(self.state.status or "searching", f"lookup failed: {exc}")
            return

        if match is None:
            if self.state.key:
                # A miss mid-track is normal (quiet passage, talking over it).
                # Keep the clock running rather than dropping the lyrics.
                log.debug("no match while locked; keeping current track")
            else:
                self._set_status("searching", "no match yet")
            return

        measured = match.position_at_clip_end
        if match.key != self.state.key:
            await self._on_new_track(match, measured, clip.end_wall)
        else:
            self._on_same_track(measured, clip.end_wall)

    async def _on_new_track(self, match: Match, measured: float | None,
                            end_wall: float) -> None:
        log.info("new track: %s", match.display)
        self._clear_track(status="playing")
        s = self.state
        s.key, s.title, s.artist = match.key, match.title, match.artist
        s.album, s.cover = match.album, match.cover
        if measured is not None:
            self._anchor(measured, end_wall)
        else:
            s.playing = True
            s.anchor_pos = 0.0
            s.anchor_wall = end_wall
            s.confidence = "estimated"
        self._set_status("playing")
        # Art first: it paints instantly, while the lyrics lookup may block.
        await self._load_cover(match.cover)
        await self.broadcast()
        await self._load_lyrics(match)

    def _on_same_track(self, measured: float | None, end_wall: float) -> None:
        s = self.state
        s.status = "playing"
        if measured is None:
            return
        predicted = s.anchor_pos + (end_wall - s.anchor_wall) if s.playing else s.anchor_pos
        drift = measured - predicted
        if abs(drift) <= config.RESYNC_TOLERANCE:
            # Clock is good. Nudge the anchor so slow drift never accumulates.
            self._anchor(measured, end_wall)
            self._pending_resync = None
            return
        # Big disagreement. Repetitive sections make Shazam report the *other*
        # occurrence, so demand two consecutive measurements that agree with
        # each other before believing a jump (a real seek stays consistent,
        # a mis-localised loop does not).
        if self._pending_resync is not None:
            prev_pos, prev_wall = self._pending_resync
            expected = prev_pos + (end_wall - prev_wall)
            if abs(measured - expected) < config.RESYNC_TOLERANCE:
                log.info("re-anchoring: %.1fs drift confirmed by 2 measurements", drift)
                self._anchor(measured, end_wall)
                self._pending_resync = None
                return
        log.debug("ignoring %.1fs jump pending confirmation", drift)
        self._pending_resync = (measured, end_wall)
        s.confidence = "estimated"

    def _go_silent(self) -> None:
        s = self.state
        if s.playing:
            # Freeze the clock where it is; a resume needs a fresh recognition.
            s.anchor_pos = s.position()
            s.anchor_wall = time.time()
            s.playing = False
            self._resume_check = True
        self._set_status("idle" if not s.key else "paused",
                         "silence" if not s.key else "paused / silent")

    # --- main loop -----------------------------------------------------------
    async def run_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("tick failed")
                self._set_status("error", str(exc))
            await self.broadcast()

    async def _refresh_idle(self, activate: bool = True) -> None:
        """Keep the homelab readout current (fleet.poll is cached, so calling
        this often is nearly free).

        The content is refreshed even while music is playing, because the panel
        widget can be pinned to show it instead of lyrics -- it needs something
        to show the moment the user asks.
        """
        loop = asyncio.get_running_loop()
        try:
            status = await loop.run_in_executor(None, fleet.poll)
        except Exception as exc:
            log.debug("fleet poll failed: %s", exc)
            return
        s = self.state
        s.idle_kind = "fleet"
        s.idle_line1, s.idle_line2 = status.summary()
        s.idle_ok = status.healthy
        if activate:
            s.idle_active = True

    def _clear_idle(self) -> None:
        # Retract only the daemon's own takeover; keep the content so a pinned
        # widget still has something to display.
        self.state.idle_active = False

    async def _apply_mpris(self, now: mpris.Now) -> None:
        """Drive state from a player's own metadata -- no audio capture.

        Position/duration/playing come straight from the player and are exact.
        The title is only a hint: browsers publish a page title, so it gets used
        for the lyrics lookup but is not treated as verified track identity.
        """
        s = self.state
        if now.key != self._mpris_key:
            self._mpris_key = now.key
            self._clear_track(status="playing")
            self._clear_idle()   # a new track wins the widget back immediately
            self._paused_since = 0.0
            self._last_src_pos = -1.0
            s.anchor_wall = 0.0   # force a fresh anchor for the new track
            s.artist, s.title, s.album = now.artist, now.title, now.album
            s.key = "mpris:" + now.key
            s.source_label = "player metadata (mpris)"
            log.info("mpris track: %s - %s", now.artist or "?", now.title)

            # MPRIS from a browser carries no album or artwork; fill them in.
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None, lambda: enrich.lookup(now.artist, now.title))
            art_url = now.art_url
            if info and info.usable:
                s.artist = info.artist or s.artist
                s.album = info.album or s.album
                s.title = info.title or s.title
                art_url = info.art_url or art_url
                if info.duration and not now.duration:
                    s.duration = info.duration
                s.source_label = f"player metadata + {info.source}"
                log.info("enriched via %s: %s - %s [%s]",
                         info.source, s.artist, s.title, s.album)

            if art_url.startswith(("http://", "https://")):
                await self._load_cover(art_url)
            elif art_url.startswith("file://"):
                s.cover_file = art_url[7:]
            if now.duration:
                s.duration = now.duration
            await self.broadcast()
            await self._load_lyrics(Match(key=s.key, title=s.title,
                                          artist=s.artist, album=s.album))
        if now.duration:
            s.duration = now.duration

        wall = time.time()
        was_playing = s.playing
        predicted = (s.anchor_pos + (wall - s.anchor_wall)) if was_playing else s.anchor_pos
        # A source that checkpoints (Plex) repeats the same number for many
        # polls; only a changed value is new information.
        fresh = abs(now.position - self._last_src_pos) > 0.001
        self._last_src_pos = now.position

        if not s.anchor_wall:
            resync = True                       # first reading for this track
        elif was_playing != now.playing:
            resync = True                       # play/pause flipped
        elif fresh and abs(now.position - predicted) > config.POSITION_RESYNC_TOLERANCE:
            resync = True                       # a real seek, or genuine drift
        else:
            resync = False                      # let the local clock run on

        if resync:
            s.anchor_pos = now.position
        else:
            s.anchor_pos = predicted
        s.anchor_wall = wall
        s.playing = now.playing
        s.confidence = "player"
        s.status = "playing" if now.playing else "paused"
        await self._refresh_idle(activate=False)

        # A brief pause keeps the lyrics; a long one hands the widget over.
        if now.playing:
            self._paused_since = 0.0
            if s.idle_active:
                self._clear_idle()
        else:
            if not self._paused_since:
                self._paused_since = time.time()
            if (time.time() - self._paused_since) >= config.PAUSE_IDLE_SECONDS:
                await self._refresh_idle()
            elif s.idle_active:
                self._clear_idle()

    async def _tick(self) -> None:
        # Prefer a player's own metadata: costs no capture, so nothing trips
        # the desktop's recording indicator, and the position is exact.
        if self.source_pref in ("mpris", "auto"):
            loop = asyncio.get_running_loop()
            now = await loop.run_in_executor(None, mpris.poll)
            if now is not None and (not now.usable or now.status.lower() == "stopped"):
                # A stale browser tab publishing a bare page title is worse than
                # no player at all -- it produces confident nonsense.
                log.debug("ignoring unusable mpris entry: %r", now.title)
                now = None
            if now is None:
                # Nothing describing itself over MPRIS. Plex clients (Plexamp,
                # the mobile apps) publish nothing locally, but the server knows
                # exactly what they are playing -- and it costs no capture.
                info = await loop.run_in_executor(None, enrich.from_plex)
                if info is not None and info.usable and info.state:
                    now = mpris.Now(
                        status="Playing" if info.playing else "Paused",
                        artist=info.artist, title=info.title, album=info.album,
                        duration=info.duration, position=info.position,
                        art_url=info.art_url)
            if now is not None and now.title and now.status.lower() != "stopped":
                self._stop_stream()   # something is describing itself; stop listening
                await self._apply_mpris(now)
                await self.broadcast()
                await asyncio.sleep(1.0)
                return
            if self.source_pref == "mpris":
                # No player: stay idle rather than opening the audio device.
                self._stop_stream()
                if self.state.key:
                    self._clear_track(status="idle", message="no player")
                    self._mpris_key = ""
                else:
                    self._set_status("idle", "no player")
                await self._refresh_idle()
                await self.broadcast()
                await asyncio.sleep(2.0)
                return
            self._mpris_key = ""

        # Read the level out of the rolling buffer -- no new stream, so the
        # recording indicator doesn't flicker.
        stream = self._ensure_stream()
        if stream is None:
            await asyncio.sleep(config.IDLE_POLL)
            return
        if stream.seconds_buffered() < config.PROBE_SECONDS:
            await asyncio.sleep(0.5)   # still filling after opening
            return
        if stream.level(config.PROBE_SECONDS) < config.SILENCE_RMS:
            self._go_silent()
            await self.broadcast()
            await asyncio.sleep(config.IDLE_POLL)
            return

        # Audio is present.
        if self.state.key and self.state.duration and \
                self.state.position() > self.state.duration + 5:
            self._clear_track(status="searching", message="track ended")

        due = (time.monotonic() - self._last_verify) >= config.VERIFY_INTERVAL
        if not self.state.key:
            self._set_status("searching", "listening for a match")
            await self.broadcast()
            await self._recognise_now()
            if not self.state.key:
                await asyncio.sleep(config.SEARCH_INTERVAL)
            return

        if self._resume_check or due:
            self._resume_check = False
            await self._recognise_now()
            return

        # Locked and recently verified: let the clock run.
        self.state.playing = True
        self.state.status = "playing"
        await asyncio.sleep(config.IDLE_POLL)

    async def serve(self) -> None:
        sock = config.socket_path()
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self._handle_client, path=str(sock))
        config.pid_path().write_text(str(os.getpid()))
        log.info("listening on %s", sock)
        self._set_status("idle", "starting up")
        loop_task = asyncio.create_task(self.run_loop())
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            async with server:
                await asyncio.gather(loop_task, heartbeat)
        finally:
            loop_task.cancel()
            heartbeat.cancel()
            self._stop_stream()
            with contextlib.suppress(OSError):
                sock.unlink()
            with contextlib.suppress(OSError):
                config.pid_path().unlink()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.broadcast()


def main(source: str = "auto", verbose: bool = False) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.log_path())],
    )
    d = Daemon(source_pref=source, verbose=verbose)
    try:
        asyncio.run(d.serve())
    except KeyboardInterrupt:
        pass
    return 0
