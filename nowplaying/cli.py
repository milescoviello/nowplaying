"""Command line entry point."""
from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nowplaying",
        description="Detect the song that's playing and follow its lyrics in real time.",
    )
    # `nowplaying` with no subcommand falls through to the TUI, which never
    # visits a subparser -- so the shared defaults have to live up here too.
    p.set_defaults(source="auto", verbose=False, click_through=False,
                   install_kwin_rule=False)
    sub = p.add_subparsers(dest="command")

    def add_source(sp):
        sp.add_argument("--source", default="auto",
                        help="auto | loopback | mic | <pactl source name> "
                             "(auto = loopback when the laptop is playing, else mic)")

    # No --source: the TUI never starts a daemon, so it has none to pass on.
    sub.add_parser("tui", help="the popup in a terminal: lyrics, controls, source (default)")

    ov = sub.add_parser("overlay", help="floating always-on-top desktop HUD")
    add_source(ov)
    ov.add_argument("--click-through", action="store_true",
                    help="ignore mouse events (control it from the tray icon)")
    ov.add_argument("--install-kwin-rule", action="store_true",
                    help="install the KWin rule that keeps it above other windows")

    d = sub.add_parser("daemon", help="run the detection daemon in the foreground")
    add_source(d)
    d.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("status", help="print the current state as JSON and exit")

    ly = sub.add_parser("lyrics", help="lyrics of your own for the track playing")
    lsub = ly.add_subparsers(dest="lyrics_command", required=True)
    lsub.add_parser("edit", help="write them, or fix the ones showing, in $EDITOR")
    imp = lsub.add_parser("import", help="use a .lrc or plain text file")
    imp.add_argument("file")
    imp.add_argument("--force", action="store_true",
                     help="replace lyrics of your own already saved")
    lsub.add_parser("path", help="print the file they're kept in")

    sub.add_parser("sources", help="list available audio sources")
    sub.add_parser("stop", help="stop a running daemon")
    return p


def cmd_sources() -> int:
    from . import audio
    default_mon = audio.default_monitor()
    default_mic = audio.default_mic()
    busy = audio.sink_is_busy()
    print(f"default sink monitor : {default_mon}")
    print(f"default microphone   : {default_mic}")
    print(f"sink currently busy  : {busy}")
    chosen, why = audio.resolve_source("auto")
    print(f"auto would pick      : {chosen}  ({why})")
    print("\nall sources:")
    for s in audio.list_sources():
        print(f"  [{s.index:>3}] {s.kind:<7} {s.name}")
    return 0


def cmd_status() -> int:
    """Report on a running daemon; never start one.

    A status query that quietly spawned a daemon with the default source
    ("auto") would open the audio device the moment nothing was playing --
    the recording indicator the MPRIS setup exists to avoid.
    """
    import json
    from . import client
    try:
        state = client.get_once(autostart=False)
    except OSError:
        print("daemon not running (start it with: nowplaying daemon --source mpris)",
              file=sys.stderr)
        return 1
    if state is None:
        print("no state received", file=sys.stderr)
        return 1
    data = state.to_dict()
    # Keep the dump readable: lyrics can be hundreds of lines.
    data["lyrics"] = f"<{len(state.lyrics)} synced lines>"
    data["lyrics_original"] = f"<{len(state.lyrics_original)} lines>"
    data["lyrics_plain"] = f"<{len(state.lyrics_plain)} chars>"
    data["lyrics_plain_original"] = f"<{len(state.lyrics_plain_original)} chars>"
    data["position"] = round(state.position(), 2)
    print(json.dumps(data, indent=2))
    return 0


def cmd_lyrics(args: argparse.Namespace) -> int:
    """Lyrics of your own for whatever the daemon says is playing. Never
    starts a daemon, for the reason cmd_status gives."""
    from pathlib import Path
    from . import client, editor
    try:
        state = client.get_once(autostart=False)
    except OSError:
        print("daemon not running (start it with: nowplaying daemon --source mpris)",
              file=sys.stderr)
        return 1
    if state is None:
        print("no state received", file=sys.stderr)
        return 1
    try:
        if args.lyrics_command == "path":
            print(editor.target(state))
        elif args.lyrics_command == "edit":
            print(editor.edit(state))
        else:
            print(editor.import_file(state, Path(args.file), args.force))
    except editor.LyricsError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_stop() -> int:
    """Stop via the pidfile.

    Deliberately not `pkill -f nowplaying` -- that pattern also matches the
    shell that invoked it, which kills the caller instead of the daemon.
    """
    import os
    import signal
    import time
    from . import config

    pid_file = config.pid_path()
    if not pid_file.exists():
        print("no daemon running")
        return 0
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        print("unreadable pidfile; removing", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("daemon was not running (stale pidfile)")
        pid_file.unlink(missing_ok=True)
        return 0
    except PermissionError:
        print(f"pid {pid} is not ours", file=sys.stderr)
        return 1
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    pid_file.unlink(missing_ok=True)
    config.socket_path().unlink(missing_ok=True)
    print(f"daemon {pid} stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "tui"

    if command == "sources":
        return cmd_sources()
    if command == "stop":
        return cmd_stop()
    if command == "status":
        return cmd_status()
    if command == "lyrics":
        return cmd_lyrics(args)
    if command == "daemon":
        from . import daemon
        return daemon.main(source=args.source, verbose=args.verbose)
    if command == "overlay":
        from . import overlay
        return overlay.main(source=args.source, click_through=args.click_through,
                            install_rule=args.install_kwin_rule)
    from . import tui
    return tui.main()
