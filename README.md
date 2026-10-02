# nowplaying

A KDE Plasma 6 panel widget that scrolls the **lyrics of whatever you're
listening to** through your taskbar, line by line, in time with the music — and
shows **homelab health** when nothing is playing.

```
♪  I watch how the moon sits in the sky in the dark night ← the line being sung
   Shining with the light from the sun                    ← the line coming next
```

```
▤  all 88 services up
   homelab healthy
```

Left-click it for the whole lyrics sheet in a popup, with the album art on top,
the line being sung highlighted and followed, and previous / play-pause / next
with a progress bar underneath. At the bottom, a switch picks where the track
comes from: the player's own metadata, or listening to the speaker output or
the mic ([Choosing the source](#choosing-the-source)). Middle-click pins the
homelab readout over the lyrics until you middle-click again.

Put it on the desktop instead and it lays the lyrics straight across the
wallpaper ([On the desktop](#on-the-desktop)).

Where the lyrics it finds are wrong, out of time or missing, write your own
and tap them into time with the track; they're kept, and used from then on
([Your own lyrics](#your-own-lyrics)).

## How it works

A daemon works out what's playing and writes a small JSON state file. The panel
widget reads that file and renders it. Splitting them means the widget stays
trivial and multiple UIs can share one detector.

```
                 ┌── MPRIS (playerctl) ──┐
                 │   what + where + when │
  daemon ────────┤                       ├──> state.json ──> Plasma applet
                 │   Plex / iTunes       │                   (QML)
                 └── LRCLIB (.lrc) ──────┘
```

### Getting the track

**MPRIS is the primary source.** `playerctl` reports the title, the duration and
the exact playback position, for free, with **no audio capture at all**. That
last part matters: capturing audio — even a *monitor* of your own speaker
output — makes Plasma light up the microphone indicator, which is both alarming
and wrong.

**Metadata is not trusted from MPRIS.** Browsers publish a page title and nothing
else: no artist, no album, no artwork, and a title like
`▶ Some Artist - Some Song (Official Video)`. So the title is cleaned (strip the
play glyph, drop `(Official Video)` / `- YouTube` noise, split on the dash) and
treated as a **hint**, not as truth.

**Real metadata comes from Plex**, via `/status/sessions`, which reports the
actual artist, album, track and cover art from the library. Plex's answer is only
accepted when its track title corroborates the MPRIS hint — because Plex reports
what the *server* is playing, which is not necessarily what this machine is
playing. Anything that isn't Plex falls back to the iTunes Search API (public, no
auth) to fill in album and artwork.

**Lyrics come from [LRCLIB](https://lrclib.net)** — free, no key, no account.
Matching uses artist + title + album + duration, since duration is what stops you
getting the radio edit's timings on the album cut. Results are cached to disk, so
a repeat play is instant and works offline. A file of your own for the track
beats all of it ([Your own lyrics](#your-own-lyrics)).

### Choosing the source

| Source | Where the track comes from | Audio capture |
|---|---|---|
| `mpris` | player metadata only | never |
| `auto` | player metadata; failing that, fingerprinting the speaker output while something plays here, else the mic | whenever no player describes the track |
| `loopback` | fingerprinting the speaker output | always |
| `mic` | fingerprinting the room | always |

Fingerprinting sends audio fingerprints to Shazam, and holding the device open
lights Plasma's recording indicator the whole time — which is why the autostart
entry runs `mpris`.

The visualizers are the one exception to "never", whatever the source: turned
on, they listen to the speaker output while a track plays, so the recording
indicator is on then too. Nothing leaves the machine, and both are off until
you turn them on.

The source can be switched while the daemon runs, from the popup or by writing
one of those words to `~/.config/nowplaying/source`:

```bash
echo mic > ~/.config/nowplaying/source
```

The daemon picks it up on its next tick, within a few seconds, and the choice
outlives a restart. **A saved choice wins over `--source`**, because autostart
passes the same `--source` at every login and would otherwise undo it;
`--source` only applies while nothing is saved. Delete the file to go back to
it.

### Getting the timing right

Each line is selected by binary-searching the `.lrc` timestamps against the
current position, and positioned by the music rather than by a fixed scroll
speed:

* A line slides up **before** it is sung (default 300 ms), so it has settled into
  place on the downbeat instead of arriving late.
* A line wider than the strip creeps sideways just far enough to expose its tail
  exactly as the line ends, so nothing is permanently cut off.
* Rapid-fire lines shrink their own lead-in rather than jumping in early.

### When nothing is playing

The widget shows homelab health from [Uptime
Kuma](https://github.com/louislam/uptime-kuma): a rack glyph plus
`all N services up`, or the names of whatever is down in the theme's error
colour.

Kuma has no unauthenticated status API unless you publish a status page, and
`/metrics` needs an API key — so rather than store another credential, this reads
`kuma.db` **read-only** over SSH that already exists:

```
laptop ──ssh──> proxmox node ──pct exec──> sqlite3 (read-only)
```

The daemon decides when the idle display takes over — no player (or, for a
source that listens, nothing identified), or paused or silent for longer than
`PAUSE_IDLE_SECONDS` (15 s) — and publishes a single `idle_active` flag. The widget just obeys it, so the rule lives in exactly one place.

## On the desktop

Drag the same widget onto the desktop and it leaves the strip behind: the
lyrics sheet sits right on the wallpaper, the line being sung held a little
above the middle and the rest fading out towards the top and bottom, with the
track and its cover above and a progress hairline below.

* It is bare white text with a soft shadow, to read over a photo. For a busy
  wallpaper, the widget's edit handle has a *Show background* button, which
  puts a frame behind it and switches to the theme's colours.
* The type scales with the widget: resize it to make the words bigger.
* Hover it for the elapsed time, previous / play-pause / next, and — for
  lyrics spelled out in Latin letters — the original-script switch.
* Lyrics without timings are shown whole, to scroll by hand.
* The homelab readout takes over by the same rule as in the panel, and
  middle-click pins it the same way.
* A visualizer, the TUI's bars along the bottom, is in the hover row and the
  settings. It's off by default: while it's on and a track plays, the daemon
  listens to the speaker output for it.

The widget can't listen for itself (QML has no audio capture), so the daemon
does it, and streams the bars over localhost while the widget is watching. It
answers on a random port, and only to requests carrying the token it writes to
`$XDG_RUNTIME_DIR/nowplaying.vis`, which only you can read. A web page can
reach localhost too, and shouldn't be able to switch the capture on. Within a
few seconds of the widget hanging up, because the visualizer is off, the music
paused or the widget gone, the daemon stops listening.

## Your own lyrics

When LRCLIB has a track's lyrics wrong, out of time or not at all, give it your
own. They're kept one file per track in `~/.local/share/nowplaying/lyrics/`,
and a track with a file there never asks LRCLIB:

```
~/.local/share/nowplaying/lyrics/
  Some Artist - Some Song.lrc      ← synced: a [mm:ss.xx] before each line
  Other Artist - Other Song.txt    ← plain text, no timings
```

* A file is found by its name, loosely: case, punctuation and spacing don't
  count, so `some artist - some song!.lrc` matches too, and files kept by
  another player work as they are. With both, the `.lrc` wins.
* Timings make it synced and none make it a plain sheet; a file with no lyrics
  in it at all says the track is instrumental. An LRC's `[offset:]` is honoured.
* The daemon notices a file added, changed or removed within a second or so:
  no restart, no skipping away and back. Remove it to go back to LRCLIB.

The name is the artist and title as the daemon has them, after Plex or iTunes
has filled them in, so the surest way to get it right is to let nowplaying make
the file:

| | |
|---|---|
| `nowplaying lyrics edit` | the playing track's lyrics in `$EDITOR`: its own file, or else the lyrics showing now, timings and all, so a wrong word is fixed without syncing anything |
| `nowplaying lyrics import FILE` | a `.lrc` or plain text file as the track's lyrics (`--force` replaces ones already saved) |
| `nowplaying lyrics path` | where the track's lyrics are kept, or would be |

In the TUI, `e` does what `nowplaying lyrics edit` does. Either way the lyrics
are saved as written: for lyrics shown in Latin letters, in their own script.

### Syncing them to the track

`s` in the TUI syncs the lyrics showing to the track as it plays: tap enter as
each line starts. Each line is listed after its timing, with a marker on the
next one to tap, and the timings so far play along as you go.

* Lyrics already synced keep their timings, and the marker starts on the next
  line to be sung, so only the stretch that's off needs tapping again. A sheet
  that's out by the same amount all the way through just needs `-` or `+`.
* A plain sheet starts from the top. The blank between two stanzas is a line
  too: tap it where the singing stops for a break, or leave it.
* Tap as the line starts rather than once you've read it. If your taps came
  consistently late, `-` takes every line a tenth of a second earlier.
* `w` saves once every line with words has a time. The panel, the desktop
  widget and the TUI follow the new timings from then on, every play.

| Key while syncing | |
|---|---|
| `enter` | the marked line starts now |
| `backspace` | take the last tap back |
| `↑` `↓` `j` `k` | move the marker |
| `←` `→` | the track back or on 5 s, to tap a stretch again |
| `0` | the track back to the start |
| `-` `+` | every line a tenth of a second earlier or later |
| `w` | save |
| `esc` | leave without saving; twice, with taps unsaved |

The timings are read off the same clock the lyrics are shown by. Seeking needs
a local player, as the other controls do, and after a seek a tap waits until
the daemon has seen it land rather than stamping where the track was.

## Install

Requires **Python 3.13** (see Notes), KDE Plasma 6, `playerctl`, and
`parec`/`pw-record` only if you want the fingerprint fallback.

```bash
git clone <this repo> ~/nowplaying && cd ~/nowplaying
python3.13 -m venv .venv
.venv/bin/pip install shazamio audioop-lts PyQt6 rich

kpackagetool6 --type Plasma/Applet --install plasmoid   # the widget
```

Add the widget to a panel or the desktop, then start the daemon:

```bash
./bin/nowplaying daemon --source mpris
```

To have it start at login, drop a `.desktop` file into `~/.config/autostart/`
running that same command.

### Optional: Plex metadata

```bash
mkdir -p ~/.config/nowplaying
cat > ~/.config/nowplaying/plex.env <<'EOF'
PLEX_URL=http://your-plex-host:32400
PLEX_TOKEN=your-token
EOF
chmod 600 ~/.config/nowplaying/plex.env
```

Without this it still works — album and artwork just come from iTunes instead.

### Optional: lyrics in Latin letters

```bash
.venv/bin/pip install anyascii pypinyin     # every script; pinyin for Chinese
.venv/bin/pip install cutlet unidic-lite    # Japanese (~250 MB dictionary)
```

Lyrics in any other script — Cyrillic, Greek, Korean, Arabic, Thai, Chinese,
Japanese, … — are then shown spelled out in Latin letters so they can be read
along with. Transliterated, not translated: the same words, just readable.
Latin text inside a line is left exactly as written, and the original script
is still published as `lyrics_original`: the popup's character-set button
switches to it, and remembers the choice.

Japanese needs its own dictionary because a kanji has several readings and only
context picks the right one; without `cutlet`, Japanese lyrics are left as they
are rather than misread. Set `NOWPLAYING_TRANSLITERATE=0` to turn it all off.

### Optional: homelab health

Site-specific, set via environment:

| Variable | Default |
|---|---|
| `NOWPLAYING_PVE_HOST` | `root@192.168.1.9` |
| `NOWPLAYING_KUMA_CTID` | `124` |
| `NOWPLAYING_KUMA_DB` | `file:/opt/uptime-kuma/data/kuma.db?mode=ro` |
| `NOWPLAYING_FLEET_POLL` | `120` (seconds) |

Needs passwordless SSH to a Proxmox node running Kuma in an LXC. If unreachable
the widget just says so and carries on.

## Commands

| Command | |
|---|---|
| `nowplaying daemon --source mpris` | run the detector (no audio capture) |
| `nowplaying daemon --source auto` | MPRIS first, audio fingerprinting as fallback |
| `nowplaying status` | current state as JSON |
| `nowplaying sources` | list audio sources |
| `nowplaying tui` | the popup in a terminal (also plain `nowplaying`) |
| `nowplaying lyrics edit` / `import FILE` / `path` | lyrics of your own for the playing track ([Your own lyrics](#your-own-lyrics)) |
| `nowplaying overlay` | floating desktop HUD |
| `nowplaying stop` | stop the daemon |

### In a terminal

`nowplaying tui` is the popup for a terminal: the track and its progress, the
whole sheet with the line being sung highlighted and followed (with the same
300 ms lead-in as the panel), the homelab readout when the daemon hands over to
it, and the controls and source switch as keys. It reads the daemon's socket
and never starts one, so a stopped daemon shows as not running rather than
being replaced by an `auto` one that listens.

| Key | |
|---|---|
| `space` `n` `p` | play/pause, next, previous — only when a local player has the track |
| `1` `2` `3` `4` | source: Player, Auto, Speaker, Mic |
| `o` | original script or Latin letters, for transliterated lyrics |
| `h` | pin the homelab readout over the lyrics, like the panel's middle-click |
| `v` | a spectrum visualizer, beside the lyrics in a wide terminal; it listens to the speaker output, so the recording indicator is on while it shows (nothing leaves the machine) |
| `e` | write the lyrics, or fix them, in `$EDITOR` ([Your own lyrics](#your-own-lyrics)) |
| `s` | sync the lyrics to the track, a tap a line ([Syncing them to the track](#syncing-them-to-the-track)) |
| `↑` `↓` `j` `k` `PgUp` `PgDn` `Home` `End` | scroll the sheet; it goes back to following the music 4 s later |
| `?` | list the keys and what each source does |
| `q` | quit |

## The fingerprinting fallback

Before MPRIS, this identified music by **fingerprinting the audio itself** via
Shazam, which still exists as the `auto`, `loopback` and `mic` sources for audio
that no player describes — a game, a stream, a phone across the room.

The interesting part is that it recovers the *playback position*, not just the
track. `shazamio-core` fingerprints a 10 s window taken from the middle of
whatever clip you hand it, and Shazam reports `offset` = where that window starts
in the track. So for a clip of length `L`:

```
position at end of clip = offset + L/2 + SEGMENT/2 + capture_latency
```

Measured against playback from a known offset, that lands within **±0.01 s**.
`CLIP_SECONDS` must stay ≥ `SEGMENT_SECONDS` or the centred-window assumption
breaks.

Two things make it hold up in practice: repetitive passages make Shazam localise
to a *different* occurrence of the same music, so a measurement that disagrees
with the running clock by more than 3 s is not believed until a second one
confirms it; and a miss mid-track (a quiet passage, someone talking over it)
keeps the current track rather than dropping the lyrics.

## Notes

**Python 3.13, not 3.14.** There is no `shazamio-core` wheel for 3.14; pip builds
the Rust core from source, it compiles *successfully*, and then segfaults on
import — its PyO3 predates the 3.14 C API. Nothing in the output points at the
version. Also `pydub` imports `audioop`, which PEP 594 removed in 3.13, hence
`audioop-lts`.

**QML cannot read local files over `XMLHttpRequest`** unless the whole session
exports `QML_XHR_ALLOW_FILE_READ=1`. Rather than loosen that session-wide, the
applet reads the state file through a `Plasma5Support` executable poll. (A `?t=`
cache-buster is also fatal on a `file://` URL — it becomes part of the filename.)

**plasmashell caches applet QML.** `kpackagetool6 --upgrade` is not enough; the
panel keeps the old copy until plasmashell restarts. `plasmawindowed
org.kde.nowplaying` surfaces the QML errors the panel silently swallows.

## The state file is an interface

The daemon mirrors its state to `~/.cache/nowplaying/state.json`, written
atomically on every change. Anything can read it — the Plasma applet does, and so
can other displays. Treat these keys as stable:

| Key | |
|---|---|
| `artist` `title` `album` | current track |
| `anchor_wall` `anchor_pos` | position anchor — see below |
| `playing` `duration` | transport state |
| `source_pref` | the source in use: `mpris` `auto` `loopback` `mic`, or a device name from `--source` |
| `player` | playerctl instance playing it, for `playerctl --player`; empty when nothing local is (a Plex client elsewhere, or fingerprinting) |
| `lyrics` | `[[seconds, text], ...]`, sorted; in Latin letters when transliterated |
| `lyrics_original` | same timings in the original script when transliterated, else `[]` |
| `lyrics_synced` | false = plain text only, no timings |
| `lyrics_plain` | the whole text without timings, in Latin letters when transliterated |
| `lyrics_plain_original` | the same in the original script when transliterated, else empty |
| `lyrics_source` | `lrclib` or `own` (a file of your own), either with `-instrumental`; empty when none were found |
| `lyrics_file` | where the track's own lyrics are kept, or would be: where a UI that writes them saves; empty until the lyrics are looked up |
| `cover_file` | local path to artwork, or empty |
| `idle_active` `idle_kind` `idle_line1` `idle_line2` `idle_ok` | idle display |
| `vis_listening` | the daemon is listening to the speaker output for the desktop visualizer |

**Position is published as an anchor, not a ticking number.** Rather than write
the position many times a second, the daemon writes the pair
`(anchor_wall, anchor_pos)` and each reader interpolates:

```python
position = anchor_pos + (time.time() - anchor_wall) if playing else anchor_pos
```

That keeps the file quiet and lets every consumer animate at its own frame rate.

## Layout

```
bin/nowplaying        launcher (uses .venv)
nowplaying/
  mpris.py            playerctl source + title cleanup
  enrich.py           Plex / iTunes metadata and artwork
  lyrics.py           LRCLIB client, LRC parser, disk cache, your own files
  editor.py           your own lyrics: in $EDITOR, and the TUI's sync
  translit.py         other scripts in Latin letters (optional)
  fleet.py            Uptime Kuma health via ssh + sqlite
  daemon.py           detection loop, state file, unix socket
  state.py            shared state, anchor-based position
  audio.py            capture + RMS (fingerprint fallback)
  spectrum.py         the visualizers' listener + bar maths
  visualizer.py       the bars streamed to the desktop widget
  recognizer.py       Shazam wrapper + offset maths
  tui.py / overlay.py optional UIs
plasmoid/             the Plasma 6 applet (QML)
```

Lyrics from [LRCLIB](https://lrclib.net). Cache in `~/.cache/nowplaying/`; your
own lyrics in `~/.local/share/nowplaying/lyrics/`.
