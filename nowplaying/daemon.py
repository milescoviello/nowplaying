"""The detection daemon: owns capture + recognition, broadcasts state to UIs."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request

from . import (audio, config, enrich, fleet, lyrics as lyrics_mod, mpris, translit,
               visualizer)
from .recognizer import Match, Recognizer
from .state import State

log = logging.getLogger("nowplaying.daemon")


class Daemon:
    def __init__(self, source_pref: str = "auto", verbose: bool = False) -> None:
        self.source_pref = source_pref
        self.verbose = verbose
        self.state = State(source_pref=source_pref)
        self.recognizer = Recognizer()
        self.clients: set[asyncio.StreamWriter] = set()
        self.clip_path = config.cache_dir() / "clip.wav"

        self._last_verify = 0.0
        self._mpris_key = ""
        self._paused_since = 0.0
        self._silent_since = 0.0
        self._last_src_pos = -1.0
        self._stream: audio.StreamCapture | None = None
        # (position, wall) of a measurement that disagreed with the clock and is
        # waiting on a second opinion before we act on it.
        self._pending_resync: tuple[float, float] | None = None
        self._resume_check = False
        # Artwork + lyrics for the current MPRIS track, fetched off the loop.
        self._load_task: asyncio.Task | None = None
        # The current track's lyrics lookup, and its own file's stamp when the
        # lyrics were loaded: a different stamp is a file added, edited or
        # removed, and the lyrics are loaded again.
        self._own_watch: tuple[Match, tuple] | None = None
        self._lyrics_task: asyncio.Task | None = None
        # Set the moment a player announces a change, so a skip doesn't wait
        # out the rest of a poll interval.
        self._wake = asyncio.Event()
        # A checkpointing source's current reading is of unknown age; the
        # next one that moves is fresh, and is believed outright.
        self._await_report = False
        # Until when a local player's readings re-anchor at the slightest
        # disagreement, after a resync; see config.SETTLE_SECONDS.
        self._settle_until = 0.0
        self._follow: asyncio.subprocess.Process | None = None
        # What the saved-source file looked like when last read; a change is
        # a new pick from the popup.
        self._source_stamp = _saved_stamp()
        # The desktop widget's visualizer: listens only while it is watched.
        self.feed = visualizer.Feed(on_change=self._on_feed)
        self._feed_note: asyncio.Task | None = None

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

    def _on_feed(self) -> None:
        self.state.vis_listening = self.feed.listening
        self._feed_note = asyncio.get_running_loop().create_task(self.broadcast())

    # --- state helpers -------------------------------------------------------
    def _set_status(self, status: str, message: str = "") -> None:
        self.state.status = status
        self.state.message = message

    def _clear_track(self, status: str = "searching", message: str = "") -> None:
        s = self.state
        s.key = s.title = s.artist = s.album = s.cover = s.cover_file = ""
        s.lyrics = []
        s.lyrics_original = []
        s.lyrics_plain = ""
        s.lyrics_plain_original = ""
        s.lyrics_synced = False
        s.lyrics_source = ""
        s.lyrics_file = ""
        s.duration = 0.0
        s.player = ""
        s.playing = False
        s.anchor_pos = 0.0
        s.confidence = ""
        self._pending_resync = None
        self._own_watch = None
        if self._lyrics_task is not None:
            self._lyrics_task.cancel()
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
            with contextlib.suppress(OSError):
                dest.touch()   # mtime = last shown, for pruning
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
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("cover download failed: %s", exc)
            return ""
        self._prune_covers()
        return str(dest)

    @staticmethod
    def _prune_covers() -> None:
        """Drop the least recently shown covers once the cache is over size."""
        try:
            files = [(f.stat(), f) for f in config.covers_dir().iterdir()
                     if f.is_file()]
        except OSError:
            return
        total = sum(st.st_size for st, _ in files)
        budget = config.COVER_CACHE_MB * 1024 * 1024
        for st, f in sorted(files, key=lambda x: x[0].st_mtime):
            if total <= budget:
                break
            with contextlib.suppress(OSError):
                f.unlink()
                total -= st.st_size

    async def _load_cover(self, url: str) -> None:
        key = self.state.key
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, self._download_cover, url)
        if self.state.key == key:   # skipped while downloading: not ours now
            self.state.cover_file = path

    async def _load_lyrics(self, match: Match) -> None:
        key = self.state.key
        duration = self.state.duration or None

        def fetch():
            # Lyrics of your own win, and need no lookup at all.
            # Stamped before it's read, so an edit made meanwhile is caught.
            stamp = lyrics_mod.own_stamp(match.artist, match.title)
            found = lyrics_mod.load_own(stamp[0]) or \
                lyrics_mod.fetch(match.artist, match.title, match.album, duration)
            return found, translit.lyrics(found.lines, found.plain), stamp

        loop = asyncio.get_running_loop()
        result, latin, stamp = await loop.run_in_executor(None, fetch)
        s = self.state
        if s.key != key:
            return
        self._own_watch = (match, stamp)
        s.lyrics = result.lines
        s.lyrics_original = []
        s.lyrics_synced = result.synced
        s.lyrics_plain = result.plain
        s.lyrics_plain_original = ""
        if latin is not None:
            # Not in Latin letters: publish them spelled out where every UI
            # already looks, and keep the original script alongside.
            s.lyrics_original = result.lines
            s.lyrics_plain_original = result.plain
            s.lyrics, s.lyrics_plain = latin
        s.lyrics_source = result.source
        s.lyrics_file = str(stamp[0])
        # LRCLIB knows the track length; Shazam does not. Use it for the
        # progress readout and for noticing when the track has run out.
        if result.duration:
            s.duration = result.duration
        if result.source.endswith("-instrumental"):
            s.message = "instrumental"
        elif not result.available:
            s.message = "no lyrics found on LRCLIB"
        elif not result.synced:
            s.message = "unsynced lyrics only"
        else:
            s.message = ""

    def _check_own_lyrics(self) -> None:
        """Load the lyrics again once the track's own file has changed -- a
        sync saved from the TUI, an edit, a file dropped in by hand -- so it
        shows within a tick, without a restart or a skip."""
        watch = self._own_watch
        if watch is None or watch[0].key != self.state.key:
            return
        if self._lyrics_task is not None and not self._lyrics_task.done():
            return   # already on it
        match, stamp = watch
        if lyrics_mod.own_stamp(match.artist, match.title) == stamp:
            return
        log.info("own lyrics changed for %s - %s", match.artist or "?", match.title)
        self._lyrics_task = asyncio.create_task(self._reload_lyrics(match))

    async def _reload_lyrics(self, match: Match) -> None:
        try:
            await self._load_lyrics(match)
            await self.broadcast()
        except Exception:
            log.exception("reloading lyrics failed")

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

    # --- source switching ----------------------------------------------------
    def _check_saved_source(self) -> None:
        """Apply a source picked in the popup since the last tick."""
        stamp = _saved_stamp()
        if stamp == self._source_stamp:
            return
        self._source_stamp = stamp
        source = read_saved_source()
        if source is not None:
            self._set_source(source)

    def _set_source(self, source: str) -> None:
        if source == self.source_pref:
            return
        log.info("source: %s -> %s", self.source_pref, source)
        self.source_pref = self.state.source_pref = source
        if source == "mpris":
            # The mode that promises no capture must let go of the device now,
            # not whenever the next player turns up.
            self._stop_stream()
        elif source in ("loopback", "mic") and self.state.key.startswith("mpris:"):
            # A player's track, its anchor and a pause-long idle takeover mean
            # nothing to a source that identifies what it hears instead.
            if self._load_task is not None:
                self._load_task.cancel()
            self._mpris_key = ""
            self._await_report = False
            self._clear_idle()
            self._clear_track(status="searching", message="listening")

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

    async def _apply_mpris(self, now: mpris.Now, checkpointed: bool = False) -> None:
        """Drive state from a player's own metadata -- no audio capture.

        Position/duration/playing come straight from the player and are exact,
        except from a `checkpointed` source (Plex reporting for another
        device), whose position is only as fresh as its last report.
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
            # Artwork and lyrics take a second or more to fetch, and a slow
            # LRCLIB far longer. Fetch them off the poll loop, so a skip made
            # meanwhile is still seen on the next poll -- and drop the fetch
            # for a track that has already been skipped past.
            if self._load_task is not None:
                self._load_task.cancel()
            self._load_task = asyncio.create_task(self._load_track(now))
        if now.duration:
            s.duration = now.duration
        # Every poll, not just on a new track: the same song can move from one
        # player to another, and controls must follow it.
        s.player = now.player

        wall = time.time()
        was_playing = s.playing
        predicted = (s.anchor_pos + (wall - s.anchor_wall)) if was_playing else s.anchor_pos
        # A source that checkpoints (Plex) repeats the same number for many
        # polls; only a changed value is new information.
        fresh = abs(now.position - self._last_src_pos) > 0.001
        self._last_src_pos = now.position

        settling = False
        if not s.anchor_wall or was_playing != now.playing:
            resync = True                       # new track, or play/pause flipped
            # A checkpoint is up to one report interval old (~15 s for Plex
            # clients), and nothing says how old -- so wait for the next.
            self._await_report = checkpointed
        elif fresh and self._await_report:
            resync = True                       # a report just landed: exact now
            self._await_report = False
        elif fresh and abs(now.position - predicted) > config.POSITION_RESYNC_TOLERANCE:
            resync = True                       # a real seek, or genuine drift
        elif fresh and not checkpointed and wall < self._settle_until and \
                abs(now.position - predicted) > config.SETTLE_TOLERANCE:
            resync = settling = True            # the last resync's reading was stale
        else:
            resync = False                      # let the local clock run on

        if resync:
            s.anchor_pos = now.position
            # A settling reading doesn't open the window again, so it closes.
            if not settling:
                self._settle_until = wall + config.SETTLE_SECONDS
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

    async def _load_track(self, now: mpris.Now) -> None:
        """Album, artwork and lyrics for the track the player just announced."""
        s = self.state
        key = s.key
        try:
            # MPRIS from a browser carries no album or artwork; fill them in.
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None, lambda: enrich.lookup(now.artist, now.title))
            if s.key != key:
                return
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
            if s.key != key:
                return
            await self.broadcast()
            # Library titles carry suffixes like "(remastered 2024)" that make
            # LRCLIB fall back to an unsynced match; display them, but look
            # up the bare title.
            await self._load_lyrics(Match(key=key, title=mpris.clean_title(s.title),
                                          artist=s.artist, album=s.album))
            if s.key == key:
                await self.broadcast()
        except Exception:
            log.exception("loading track details failed")

    async def _watch_players(self) -> None:
        """Wake the poll loop as soon as any player changes track or state.

        Polling alone sees a skip only on its next pass; playerctl --follow
        hears the MPRIS signal as it happens.
        """
        while True:
            try:
                proc = self._follow = await asyncio.create_subprocess_exec(
                    *mpris.FOLLOW, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError as exc:
                log.debug("not following players: %s", exc)
                return
            try:
                while await proc.stdout.readline():
                    self._wake.set()
            finally:
                self._follow = None
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(5)   # it exited; plain polling until it's back

    async def _nap(self, seconds: float) -> None:
        """Wait for the next poll -- or less, if a player changes first."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            return
        # Players publish a change in a burst (title, then artist, then art);
        # let it land rather than poll a half-updated track.
        await asyncio.sleep(0.15)

    async def _tick(self) -> None:
        self._check_saved_source()
        self._check_own_lyrics()
        # Prefer a player's own metadata: costs no capture, so nothing trips
        # the desktop's recording indicator, and the position is exact.
        if self.source_pref in ("mpris", "auto"):
            loop = asyncio.get_running_loop()
            self._wake.clear()   # this poll covers anything announced so far
            now = await loop.run_in_executor(None, mpris.poll)
            if now is not None and (not now.usable or now.status.lower() == "stopped"):
                # A stale browser tab publishing a bare page title is worse than
                # no player at all -- it produces confident nonsense.
                log.debug("ignoring unusable mpris entry: %r", now.title)
                now = None
            checkpointed = False
            if now is None or not now.playing:
                # Nothing playing here. Plex clients on other devices (Plexamp
                # on a phone, the TV apps) publish nothing locally, but the
                # server knows what they're playing -- it costs no capture, and
                # a track playing elsewhere beats one sitting paused here.
                info = await loop.run_in_executor(None, enrich.from_plex)
                if info is not None and info.usable and info.state and \
                        (now is None or info.playing):
                    checkpointed = True
                    now = mpris.Now(
                        status="Playing" if info.playing else "Paused",
                        artist=info.artist, title=info.title, album=info.album,
                        duration=info.duration, position=info.position,
                        art_url=info.art_url)
            if now is not None and now.title and now.status.lower() != "stopped":
                self._stop_stream()   # something is describing itself; stop listening
                await self._apply_mpris(now, checkpointed)
                await self.broadcast()
                # Waiting on a fresh report: catch it the moment it lands,
                # since the anchor is only as exact as when we see it.
                # Likewise while a local player's position settles.
                quick = self._await_report or time.time() < self._settle_until
                await self._nap(0.25 if quick and self.state.playing else 1.0)
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
                await self._nap(2.0)
                return
            self._mpris_key = ""
            # Fingerprinting from here on; the last player no longer owns the track.
            self.state.player = ""

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
            # The same rule as a player's: nothing identified, or silent for as
            # long as a pause may last, and the idle display takes over.
            if not self._silent_since:
                self._silent_since = time.time()
            if not self.state.key or \
                    time.time() - self._silent_since >= config.PAUSE_IDLE_SECONDS:
                await self._refresh_idle()
            await self.broadcast()
            await asyncio.sleep(config.IDLE_POLL)
            return

        # Audio is present, and wins the widget back from the idle display.
        self._silent_since = 0.0
        if self.state.idle_active:
            self._clear_idle()
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

    def _on_sigterm(self) -> None:
        """`nowplaying stop` sends SIGTERM, whose default kills us outright --
        leaving playerctl --follow running with nobody reading it. Take it
        down first, then die the same abrupt way as ever (a slow, graceful
        exit could race a daemon started right after and unlink its socket).
        """
        if self._follow is not None:
            with contextlib.suppress(ProcessLookupError):
                self._follow.kill()
        # Likewise the visualizer's parec, which would go on listening.
        self.feed.close()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    async def serve(self) -> None:
        sock = config.socket_path()
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self._handle_client, path=str(sock))
        config.pid_path().write_text(str(os.getpid()))
        log.info("listening on %s", sock)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)
        await asyncio.get_running_loop().run_in_executor(None, self._prune_covers)
        await self.feed.start()
        self._set_status("idle", "starting up")
        loop_task = asyncio.create_task(self.run_loop())
        heartbeat = asyncio.create_task(self._heartbeat())
        # Whatever the source: the popup can switch to one that reads players
        # at any moment, and following them costs no capture.
        watcher = asyncio.create_task(self._watch_players())
        try:
            async with server:
                await asyncio.gather(loop_task, heartbeat)
        finally:
            loop_task.cancel()
            heartbeat.cancel()
            watcher.cancel()
            if self._load_task is not None:
                self._load_task.cancel()
            if self._lyrics_task is not None:
                self._lyrics_task.cancel()
            self._stop_stream()
            self.feed.close()
            with contextlib.suppress(OSError):
                sock.unlink()
            with contextlib.suppress(OSError):
                config.pid_path().unlink()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.broadcast()


def _saved_stamp() -> tuple[int, int, int] | None:
    try:
        st = config.source_file().stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_ino, st.st_size


def read_saved_source() -> str | None:
    """The source last picked in the popup, or None if nothing valid is saved."""
    try:
        value = config.source_file().read_text().strip()
    except OSError:
        return None
    if value not in config.SOURCES:
        log.warning("ignoring unknown source %r in %s", value, config.source_file())
        return None
    return value


def main(source: str = "auto", verbose: bool = False) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.log_path())],
    )
    # A pick from the popup beats --source: autostart passes a fixed --source
    # at every login, which would otherwise undo the pick each time.
    saved = read_saved_source()
    if saved is not None and saved != source:
        log.info("using the saved source %s over --source %s", saved, source)
    d = Daemon(source_pref=saved or source, verbose=verbose)
    try:
        asyncio.run(d.serve())
    except KeyboardInterrupt:
        pass
    return 0
