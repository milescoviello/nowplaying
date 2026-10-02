"""The visualizer's ears and maths: the speaker output, and bar heights from it.

Shared by the TUI, which listens for itself, and the daemon, which listens on
behalf of the desktop widget (QML can't capture audio).
"""
from __future__ import annotations

import subprocess
import threading

# The caps' hold and fall are counted in frames, so both users draw at this
# rate.
FPS = 60

RATE = 44100
# Samples per look: 46 ms, short enough that a beat moves the bars rather
# than being averaged away.
WINDOW = 2048
# Each look is blended with the last this much, both ways, as a web audio
# analyser does: enough to stop flicker, little enough to keep the bounce.
SMOOTHING = 0.5
# A full-scale sine reads 0 dB; the bars span this far below the loudest
# band of late -- wide enough that a quiet band still stands mid-height
# rather than dropping to the floor -- and a quieter peak than FLOOR is
# treated as silence rather than turned up into a wall of noise.
RANGE = 48.0
FLOOR = -35.0
# A bar that pushes its cap throws it upward with the bar's own speed, up to
# KICK a frame -- enough for an arc a good way above the bar, not enough to
# fling a quiet one to the top -- and it falls back faster by GRAVITY each
# frame, so the caps bounce rather than wait.
KICK = 0.04
GRAVITY = 0.004


class Listener:
    """The speaker output for the visualizer: parec on the default sink's
    monitor, keeping the last WINDOW samples.

    It is a capture like the fingerprinting one, so the desktop's recording
    indicator is on while it runs, though nothing leaves this machine. Its
    users start it only on request, and only keep it open while a track
    plays.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.buf = bytearray()
        self.lock = threading.Lock()

    def start(self) -> str:
        """Start listening; why it couldn't, or "" when it did."""
        from . import audio
        source = audio.default_monitor()
        if not source:
            return "no speaker output to listen to"
        try:
            self.proc = subprocess.Popen(
                ["parec", f"--device={source}", "--format=s16le", f"--rate={RATE}",
                 "--channels=1", "--latency-msec=20"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        except OSError:
            return "the visualizer needs parec"
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        return ""

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        while chunk := proc.stdout.read1(4096):
            with self.lock:
                self.buf += chunk
                del self.buf[:-WINDOW * 2]

    def started(self) -> bool:
        return self.proc is not None

    def died(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        with self.lock:
            self.buf.clear()

    def latest(self) -> bytes | None:
        with self.lock:
            return bytes(self.buf) if len(self.buf) >= WINDOW * 2 else None


class Spectrum:
    """Bar heights from the latest samples: log-spaced bands from the bass to
    the treble, levelled against the loudest of late so any volume fills the
    space, with a cap above each bar that the bar throws up and gravity brings
    back down."""

    def __init__(self) -> None:
        import numpy
        self.np = numpy
        self.window = numpy.blackman(WINDOW)
        self.freqs = numpy.fft.rfftfreq(WINDOW, 1 / RATE)
        self.smooth = numpy.zeros(len(self.freqs))
        self.peak = FLOOR
        self.levels = numpy.zeros(0)
        self.caps = numpy.zeros(0)
        self.lift = numpy.zeros(0)       # each cap's upward speed

    def __call__(self, raw: bytes, bars: int):
        np = self.np
        x = np.frombuffer(raw, dtype="<i2")[-WINDOW:] / 32768
        # Scaled so a full-scale sine comes out at 1, i.e. 0 dB.
        spectrum = np.abs(np.fft.rfft(x * self.window)) / (WINDOW * 0.42 / 2)
        self.smooth = SMOOTHING * self.smooth + (1 - SMOOTHING) * spectrum
        edges = np.geomspace(40, 16000, bars + 1)
        centres = np.sqrt(edges[:-1] * edges[1:])
        # The bass bands are narrower than the FFT's bins, so read them off
        # the curve between bins; wider bands take their loudest bin.
        band = np.interp(centres, self.freqs, self.smooth)
        bins = np.searchsorted(self.freqs, edges)
        for i, (a, b) in enumerate(zip(bins[:-1], bins[1:])):
            if b > a:
                band[i] = max(band[i], self.smooth[a:b].max())
        db = 20 * np.log10(band + 1e-9)
        # Music thins out towards the treble; tilt it back up 3 dB an octave.
        db += 3 * np.log2(centres / 1000)
        self.peak = max(FLOOR, db.max(), self.peak - 0.08)
        level = np.clip((db - (self.peak - RANGE)) / RANGE, 0, 1)
        if len(self.levels) != bars:
            self.caps, self.lift, self.levels = level.copy(), np.zeros(bars), level
        before, self.levels = self.levels, level
        pushed = level >= self.caps
        speed = np.minimum(np.maximum(level - before, 0), KICK)
        self.lift = np.where(pushed, np.maximum(self.lift, speed), self.lift - GRAVITY)
        caps = np.where(pushed, level, self.caps + self.lift)
        # Back down on its bar: it rides there until the bar throws it again.
        self.lift = np.where(~pushed & (caps <= level), 0, self.lift)
        self.caps = np.clip(np.maximum(caps, level), 0, 1)
        self.lift = np.where(self.caps >= 1, np.minimum(self.lift, 0), self.lift)
        return self.levels, self.caps
