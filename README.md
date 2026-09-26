# nowplaying

A KDE Plasma 6 panel widget that scrolls the **lyrics of whatever you're
listening to** through your taskbar, line by line, in time with the music — and
shows **homelab health** when nothing is playing.

```
♪  I heard there was a secret chord          ← the line being sung
   that David played and it pleased the Lord ← the line coming next
```

```
▤  all 88 services up
   homelab healthy
```

Left-click it for the whole lyrics sheet in a popup, with the album art on top
and the line being sung highlighted and followed. Middle-click pins the homelab
readout over the lyrics until you middle-click again.

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
a repeat play is instant and works offline.

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

The daemon decides when the idle display takes over — no player, or paused for
longer than `PAUSE_IDLE_SECONDS` (15 s) — and publishes a single `idle_active`
flag. The widget just obeys it, so the rule lives in exactly one place.

## Install

Requires **Python 3.13** (see Notes), KDE Plasma 6, `playerctl`, and
`parec`/`pw-record` only if you want the fingerprint fallback.

```bash
git clone <this repo> ~/nowplaying && cd ~/nowplaying
python3.13 -m venv .venv
.venv/bin/pip install shazamio audioop-lts PyQt6 rich

kpackagetool6 --type Plasma/Applet --install plasmoid   # the panel widget
```

Add the widget to a panel, then start the daemon:

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
is still published as `lyrics_original`.

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
| `nowplaying tui` | terminal karaoke view |
| `nowplaying overlay` | floating desktop HUD |
| `nowplaying stop` | stop the daemon |

## The fingerprinting fallback

Before MPRIS, this identified music by **fingerprinting the audio itself** via
Shazam, which still exists behind `--source auto|loopback|mic` for audio that no
player describes — a game, a stream, a phone across the room.

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
| `player` | playerctl instance playing it, for `playerctl --player`; empty when nothing local is (a Plex client elsewhere, or fingerprinting) |
| `lyrics` | `[[seconds, text], ...]`, sorted; in Latin letters when transliterated |
| `lyrics_original` | same timings in the original script when transliterated, else `[]` |
| `lyrics_synced` | false = plain text only, no timings |
| `lyrics_plain` | the whole text without timings, in Latin letters when transliterated |
| `cover_file` | local path to artwork, or empty |
| `idle_active` `idle_kind` `idle_line1` `idle_line2` `idle_ok` | idle display |

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
  lyrics.py           LRCLIB client, LRC parser, disk cache
  translit.py         other scripts in Latin letters (optional)
  fleet.py            Uptime Kuma health via ssh + sqlite
  daemon.py           detection loop, state file, unix socket
  state.py            shared state, anchor-based position
  audio.py            capture + RMS (fingerprint fallback)
  recognizer.py       Shazam wrapper + offset maths
  tui.py / overlay.py optional UIs
plasmoid/             the Plasma 6 applet (QML)
```

Lyrics from [LRCLIB](https://lrclib.net). Cache in `~/.cache/nowplaying/`.
