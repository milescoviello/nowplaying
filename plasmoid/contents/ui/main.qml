/*
 * nowplaying panel ticker
 *
 * Reads the state file the daemon mirrors to disk (QML has no unix socket API)
 * and scrolls lyrics upward through a strip in the panel: the line being sung
 * on top, the line coming next below it. Each new line slides in from the
 * bottom, timed off the track position rather than a fixed speed.
 *
 * Left-click opens a popup with the whole lyrics sheet and the player's
 * controls. The daemon still decides everything; the popup only renders the
 * same state and hands play/pause/next to playerctl.
 */
import QtCore
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    // The ticker is the compact representation, so it stays inline in the
    // panel while the full lyrics open in the standard applet popup.
    preferredRepresentation: compactRepresentation

    // QML's XMLHttpRequest refuses to read local files unless the whole session
    // runs with QML_XHR_ALLOW_FILE_READ=1, so the state file is read through
    // Plasma's executable data source instead. Polled once a second -- the
    // position is interpolated locally in between, so the scroll stays smooth.
    readonly property string statePath:
        StandardPaths.writableLocation(StandardPaths.GenericCacheLocation)
            .toString().replace("file://", "")
        + "/nowplaying/state.json"
    readonly property string readCommand: "cat " + statePath
    // Where a source picked in the popup is saved for the daemon to pick up.
    readonly property string configDir:
        StandardPaths.writableLocation(StandardPaths.GenericConfigLocation)
            .toString().replace("file://", "")
        + "/nowplaying"

    property var lyrics: []
    // The same lines in their own script, when the daemon spelled them out in
    // Latin letters for `lyrics`; otherwise empty.
    property var lyricsOriginal: []
    // Persisted like the pin: someone who reads the script wants it for every
    // track, not just this one. Popup only -- the strip stays readable to all.
    readonly property bool showOriginal: plasmoid.configuration.originalScript
        && lyricsOriginal.length > 0 && lyricsOriginal.length === lyrics.length
    property string status: "idle"
    property string artist: ""
    property string title: ""
    property string album: ""
    property string coverFile: ""
    property real duration: 0
    // Empty when nothing local is playing the track (a Plex client on another
    // device): there is nobody to send controls to, so the popup hides them.
    property string player: ""
    // The source the daemon reports it is using -- what the switch shows.
    property string sourcePref: ""
    // The last pick, until the daemon either reports it or gives up waiting.
    property string requestedSource: ""
    onSourcePrefChanged: if (sourcePref === requestedSource) requestedSource = ""
    // Only set when there are no timings: the popup shows it as a sheet.
    property string lyricsPlain: ""
    property real anchorPos: 0
    property real anchorWall: 0
    property bool playing: false
    property real writtenAt: 0
    property string daemonMessage: ""
    // Shown instead of dead space when nothing is playing.
    property string idleKind: ""
    property string idleLine1: ""
    property string idleLine2: ""
    property bool idleOk: true
    // The daemon decides when idle content takes over (no player, or paused
    // long enough) -- don't re-derive that rule here.
    property bool idleActive: false
    // Middle-click pin: force the homelab readout even while music plays.
    // Persisted, so it survives a plasmashell restart.
    readonly property bool pinFleet: plasmoid.configuration.pinFleet
    // Either the daemon decided to take over, or the user pinned it.
    readonly property bool showFleet:
        !stale && idleKind.length > 0 && (idleActive || pinFleet)

    property int lineIndex: -1
    property real nowPos: 0
    property real lineStart: 0
    property real lineEnd: 0
    readonly property real leadSeconds: plasmoid.configuration.leadInMs / 1000

    // A binding never re-runs just because Date.now() moved on, so staleness
    // needs a clock that ticks: re-reading an unchanged written_at from a dead
    // daemon's file changes nothing, and its last lyrics would scroll forever.
    property real clock: Date.now() / 1000
    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: root.clock = Date.now() / 1000
    }
    readonly property bool stale:
        writtenAt > 0 && (clock - writtenAt) > 20
    readonly property bool hasTrack: title.length > 0
    readonly property string trackLabel:
        hasTrack ? (artist.length ? artist + " — " + title : title) : ""
    readonly property bool showLyrics:
        !stale && !idleActive && !pinFleet && lyrics.length > 0

    function lineAt(i) {
        if (!lyrics || i < 0 || i >= lyrics.length) return "";
        return lyrics[i][1] || "♪";
    }
    // [outgoing, current, next] -- the column slides up by one row per line.
    readonly property var windowLines:
        [lineAt(lineIndex - 1), lineAt(lineIndex), lineAt(lineIndex + 1)]

    toolTipMainText: hasTrack ? trackLabel : "nowplaying"
    toolTipSubText: {
        if (pinFleet) return "pinned to homelab stats — middle-click to unpin";
        if (stale) return "daemon not running";
        if (hasTrack && lyrics.length === 0)
            return daemonMessage.length ? daemonMessage : "no synced lyrics";
        if (hasTrack) return status;
        return status === "searching" ? "listening…" : "waiting for audio…";
    }

    // --- state feed ---------------------------------------------------------
    function applyState(text) {
        if (!text) return;
        try {
            var d = JSON.parse(text);
        } catch (e) {
            return;
        }
        var newKey = (d.key || "") + "|" + (d.lyrics ? d.lyrics.length : 0);
        status = d.status || "idle";
        artist = d.artist || "";
        title = d.title || "";
        album = d.album || "";
        duration = d.duration || 0;
        player = d.player || "";
        sourcePref = d.source_pref || "";
        anchorPos = d.anchor_pos || 0;
        anchorWall = d.anchor_wall || 0;
        playing = !!d.playing;
        writtenAt = d.written_at || 0;
        daemonMessage = d.message || "";
        coverFile = d.cover_file || "";
        idleKind = d.idle_kind || "";
        idleLine1 = d.idle_line1 || "";
        idleLine2 = d.idle_line2 || "";
        idleOk = d.idle_ok !== false;
        idleActive = !!d.idle_active;
        // Outside the key check: plain text arrives without changing the
        // (empty) line count, so trackKey would never notice it.
        lyricsPlain = (d.lyrics && d.lyrics.length) ? "" : (d.lyrics_plain || "");
        if (newKey !== trackKey) {
            trackKey = newKey;
            lyrics = d.lyrics || [];
            lyricsOriginal = d.lyrics_original || [];
            lineIndex = -1;
            snapColumn();
        }
        refreshLine();
    }
    property string trackKey: ""

    Plasma5Support.DataSource {
        id: stateSource
        engine: "executable"
        connectedSources: [root.readCommand]
        interval: 1000
        onNewData: function(sourceName, data) {
            if (data["exit code"] === 0) {
                root.applyState(data["stdout"]);
            }
        }
    }

    function position() {
        if (!playing) return anchorPos;
        return anchorPos + (Date.now() / 1000 - anchorWall);
    }

    function refreshLine() {
        nowPos = position();
        if (!lyrics || lyrics.length === 0) {
            lineIndex = -1;
            return;
        }
        // Select on (position + lead) so the slide finishes just as the line
        // is sung, instead of starting to move only once it is already late.
        var t = nowPos + leadSeconds;
        var lo = 0, hi = lyrics.length - 1, best = -1;
        while (lo <= hi) {
            var mid = Math.floor((lo + hi) / 2);
            if (lyrics[mid][0] <= t) { best = mid; lo = mid + 1; }
            else { hi = mid - 1; }
        }
        if (best !== lineIndex) {
            var jumped = Math.abs(best - lineIndex) > 1;
            lineIndex = best;
            if (jumped) snapColumn(); else slideColumn();
        }
        if (best >= 0) {
            lineStart = lyrics[best][0];
            lineEnd = (best + 1 < lyrics.length) ? lyrics[best + 1][0]
                                                 : lineStart + 6;
        }
    }

    // A line wider than the strip creeps sideways just far enough to expose its
    // tail exactly as the line ends -- so nothing is permanently cut off.
    function horizontalOffset(viewportWidth, textWidth) {
        if (textWidth <= viewportWidth) return 0;
        var span = Math.max(0.001, lineEnd - lineStart);
        var g = Math.max(0, Math.min(1, (nowPos - lineStart) / span));
        return (viewportWidth - textWidth) * g;
    }

    property var _snap: null
    property var _slide: null
    function snapColumn() { if (_snap) _snap(); }
    function slideColumn() { if (_slide) _slide(); }

    // Transport commands, one-shot: dropped once playerctl exits. The daemon
    // notices the change on its next poll, so no state is touched here.
    Plasma5Support.DataSource {
        id: runner
        engine: "executable"
        onNewData: function(sourceName) {
            disconnectSource(sourceName);
        }
    }

    function shellQuote(s) {
        return "'" + s.replace(/'/g, "'\\''") + "'";
    }

    function control(verb) {
        if (!player.length) return;
        runner.connectSource("playerctl --player " + shellQuote(player) + " " + verb);
    }

    // Saved rather than sent: the daemon has no channel QML can speak, and a
    // file outlives a restart. Written whole and renamed into place, so the
    // daemon never reads half a word.
    function pickSource(value) {
        if (value === sourcePref) return;
        var dir = shellQuote(configDir);
        runner.connectSource("mkdir -p " + dir
            + " && printf %s " + shellQuote(value) + " > " + dir + "/source.tmp"
            + " && mv " + dir + "/source.tmp " + dir + "/source");
        requestedSource = value;
        sourceTimeout.restart();
    }

    Timer {
        id: sourceTimeout
        // Longer than the daemon's slowest tick, a fingerprint lookup.
        interval: 15000
        onTriggered: root.requestedSource = ""
    }

    function clockText(seconds) {
        var t = Math.max(0, Math.floor(seconds));
        var h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
        var ss = (s < 10 ? "0" : "") + s;
        return h > 0 ? h + ":" + (m < 10 ? "0" : "") + m + ":" + ss
                     : m + ":" + ss;
    }

    Timer {  // drives the current line, the slide and the sideways creep
        interval: 20
        // Also while the popup is open: its progress bar needs a moving
        // position even when there are no lyrics to time.
        running: root.playing && !root.stale
            && (root.lyrics.length > 0 || root.expanded)
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshLine()
    }

    // --- panel strip --------------------------------------------------------
    compactRepresentation: Item {
        id: strip

        readonly property int tickerWidth: plasmoid.configuration.tickerWidth

        // Preferred, not forced: pinning min == max makes a full panel overflow
        // and shove the trailing applets off the screen edge.
        Layout.minimumWidth: Kirigami.Units.iconSizes.small * 3
        Layout.preferredWidth: tickerWidth
        Layout.maximumWidth: tickerWidth
        Layout.fillHeight: true

        // Left-click opens the popup. Only the left button is accepted, so
        // right-click still reaches the panel for its context menu.
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            property bool wasExpanded: false
            // Read on press: the popup has already closed by the time a click
            // on the strip lands, so toggling `expanded` would reopen it.
            onPressed: wasExpanded = root.expanded
            onClicked: root.expanded = !wasExpanded
        }

        // Middle-click anywhere on the widget toggles the pin.
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.MiddleButton
            onClicked: {
                plasmoid.configuration.pinFleet = !plasmoid.configuration.pinFleet;
                plasmoid.configuration.writeConfig();
            }
        }

        Component.onCompleted: {
            root._snap = function() {
                slideAnim.stop();
                col.y = -viewport.rowH;
            };
            root._slide = function() {
                slideAnim.stop();
                col.y = 0;
                slideAnim.start();
            };
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Kirigami.Units.smallSpacing
            anchors.rightMargin: Kirigami.Units.smallSpacing
            spacing: Kirigami.Units.smallSpacing

            // Album art, sized to the strip's height. Falls back to the generic
            // icon when there is no art (or it failed to download).
            Item {
                id: artSlot

                // Fill nearly the full panel height -- a 2px breathing gap top
                // and bottom rather than a full smallSpacing each side.
                readonly property int side:
                    Math.max(Kirigami.Units.iconSizes.small, strip.height - 4)

                Layout.preferredWidth: side
                Layout.preferredHeight: side
                Layout.alignment: Qt.AlignVCenter

                Image {
                    id: art
                    anchors.fill: parent
                    source: root.coverFile.length
                        ? "file://" + root.coverFile
                        : ""
                    // Downloaded at up to 600x600; let Qt scale it once, smoothly.
                    sourceSize.width: artSlot.side * 2
                    sourceSize.height: artSlot.side * 2
                    fillMode: Image.PreserveAspectCrop
                    smooth: true
                    asynchronous: true
                    cache: true
                    // Stand aside for the homelab glyph -- the cover stays
                    // loaded so it reappears the moment music resumes.
                    visible: status === Image.Ready && !artSlot.showingFleet
                    opacity: root.stale ? 0.45 : 1.0
                }

                // Custom rack glyph while showing homelab health, otherwise the
                // generic media icon. isMask lets the theme tint it, which is
                // how the red "something is down" state comes for free.
                readonly property bool showingFleet:
                    root.showFleet && root.idleKind === "fleet"

                Kirigami.Icon {
                    anchors.centerIn: parent
                    width: artSlot.showingFleet
                        ? Math.round(artSlot.side * 0.86)
                        : Kirigami.Units.iconSizes.small
                    height: width
                    source: artSlot.showingFleet
                        ? Qt.resolvedUrl("homelab.svg")
                        : "media-optical-audio"
                    isMask: artSlot.showingFleet
                    color: root.idleOk ? Kirigami.Theme.textColor
                                       : Kirigami.Theme.negativeTextColor
                    visible: !art.visible
                    opacity: (root.hasTrack || artSlot.showingFleet) && !root.stale
                        ? 1.0 : 0.45
                }
            }

            Item {
                id: viewport
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                // Two rows visible: the line being sung, and the one after it.
                readonly property real rowH: Math.max(1, height / 2)

                Column {
                    id: col
                    width: viewport.width
                    y: -viewport.rowH
                    visible: root.showLyrics

                    NumberAnimation {
                        id: slideAnim
                        target: col
                        property: "y"
                        to: -viewport.rowH
                        duration: 240
                        easing.type: Easing.OutCubic
                    }

                    Repeater {
                        model: root.windowLines
                        delegate: Item {
                            width: viewport.width
                            height: viewport.rowH
                            clip: true

                            readonly property bool isCurrent: index === 1

                            PlasmaComponents.Label {
                                id: lineText
                                text: modelData
                                height: parent.height
                                verticalAlignment: Text.AlignVCenter
                                // Only the current line creeps sideways; the
                                // others just elide.
                                width: isCurrent
                                    ? Math.max(implicitWidth, viewport.width)
                                    : viewport.width
                                elide: isCurrent ? Text.ElideNone : Text.ElideRight
                                x: isCurrent
                                    ? root.horizontalOffset(viewport.width,
                                                            lineText.implicitWidth)
                                    : 0
                                font.pixelSize: Math.max(
                                    9, viewport.rowH * (isCurrent ? 0.70 : 0.62))
                                font.weight: isCurrent ? Font.DemiBold : Font.Normal
                                opacity: isCurrent ? 1.0 : 0.55
                            }
                        }
                    }
                }

                // Nothing playing: hand the space to the homelab readout, in the
                // same two-row shape the lyrics use.
                Column {
                    anchors.fill: parent
                    visible: !col.visible && root.showFleet

                    PlasmaComponents.Label {
                        width: viewport.width
                        height: viewport.rowH
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                        text: root.idleLine1
                        font.pixelSize: Math.max(9, viewport.rowH * 0.70)
                        font.weight: Font.DemiBold
                        // Only shout when something is actually wrong.
                        color: root.idleOk
                            ? Kirigami.Theme.textColor
                            : Kirigami.Theme.negativeTextColor
                    }
                    PlasmaComponents.Label {
                        width: viewport.width
                        height: viewport.rowH
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                        text: root.idleLine2
                        font.pixelSize: Math.max(9, viewport.rowH * 0.62)
                        opacity: 0.55
                    }
                }

                // Last resort: daemon down, or idle data not in yet.
                PlasmaComponents.Label {
                    anchors.fill: parent
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    opacity: 0.6
                    visible: !col.visible && !root.showFleet
                    font.pixelSize: Math.max(9, viewport.rowH * 0.70)
                    text: {
                        if (root.stale) return "nowplaying: not running";
                        if (root.hasTrack) return root.trackLabel;
                        if (root.status === "searching") return "listening…";
                        if (root.status === "paused") return "paused";
                        return "nowplaying";
                    }
                }
            }
        }
    }

    // --- popup --------------------------------------------------------------
    fullRepresentation: PlasmaExtras.Representation {
        id: popup

        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
        Layout.minimumHeight: Kirigami.Units.gridUnit * 18
        Layout.preferredWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredHeight: Kirigami.Units.gridUnit * 28

        // A dead daemon's last track is not "now playing"; show nothing of it.
        readonly property bool live: root.hasTrack && !root.stale

        header: PlasmaExtras.PlasmoidHeading {
            visible: popup.live

            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing

                Item {
                    id: coverSlot
                    readonly property int side: Kirigami.Units.gridUnit * 4
                    Layout.preferredWidth: side
                    Layout.preferredHeight: side

                    Image {
                        id: cover
                        anchors.fill: parent
                        source: root.coverFile.length
                            ? "file://" + root.coverFile
                            : ""
                        sourceSize.width: coverSlot.side * 2
                        sourceSize.height: coverSlot.side * 2
                        fillMode: Image.PreserveAspectCrop
                        smooth: true
                        asynchronous: true
                        visible: status === Image.Ready
                    }
                    Kirigami.Icon {
                        anchors.centerIn: parent
                        width: Kirigami.Units.iconSizes.large
                        height: width
                        source: "media-optical-audio"
                        visible: !cover.visible
                        opacity: 0.45
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 0

                    PlasmaExtras.Heading {
                        Layout.fillWidth: true
                        level: 3
                        text: root.title
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: root.artist
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                    }
                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: root.album
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        opacity: 0.7
                    }
                }

                PlasmaComponents.ToolButton {
                    Layout.alignment: Qt.AlignTop
                    visible: root.lyricsOriginal.length > 0 && syncedView.visible
                    icon.name: "character-set"
                    checkable: true
                    checked: plasmoid.configuration.originalScript
                    onToggled: {
                        plasmoid.configuration.originalScript = checked;
                        plasmoid.configuration.writeConfig();
                    }
                    Accessible.name: "Original script"
                    PlasmaComponents.ToolTip.text: checked
                        ? "Showing the original script; click for Latin letters"
                        : "Showing Latin letters; click for the original script"
                    PlasmaComponents.ToolTip.visible: hovered
                    PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
            }
        }

        footer: PlasmaExtras.PlasmoidHeading {
            position: PlasmaComponents.ToolBar.Footer
            // Not just while a track plays: nothing playing is exactly when
            // someone reaches for a different source.
            visible: !root.stale && (popup.live || root.sourcePref.length > 0)

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    visible: popup.live
                    spacing: Kirigami.Units.smallSpacing

                    // Tabular figures, so the bar doesn't twitch as digits change.
                    PlasmaComponents.Label {
                        text: root.clockText(root.duration > 0
                            ? Math.min(root.nowPos, root.duration) : root.nowPos)
                        font.features: ({ "tnum": 1 })
                        opacity: 0.7
                    }
                    PlasmaComponents.ProgressBar {
                        Layout.fillWidth: true
                        // An unknown length has nothing to be a fraction of.
                        visible: root.duration > 0
                        from: 0
                        to: Math.max(1, root.duration)
                        value: root.nowPos
                    }
                    Item {
                        Layout.fillWidth: true
                        visible: root.duration <= 0
                    }
                    PlasmaComponents.Label {
                        visible: root.duration > 0
                        text: root.clockText(root.duration)
                        font.features: ({ "tnum": 1 })
                        opacity: 0.7
                    }
                }

                // Only when there's a local player to obey them: a Plex client
                // on another device is out of playerctl's reach.
                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    visible: popup.live && root.player.length > 0
                    spacing: Kirigami.Units.largeSpacing

                    PlasmaComponents.ToolButton {
                        icon.name: "media-skip-backward"
                        Accessible.name: "Previous track"
                        onClicked: root.control("previous")
                    }
                    PlasmaComponents.ToolButton {
                        icon.name: root.playing ? "media-playback-pause"
                                                : "media-playback-start"
                        icon.width: Kirigami.Units.iconSizes.medium
                        icon.height: Kirigami.Units.iconSizes.medium
                        Accessible.name: root.playing ? "Pause" : "Play"
                        onClicked: root.control("play-pause")
                    }
                    PlasmaComponents.ToolButton {
                        icon.name: "media-skip-forward"
                        Accessible.name: "Next track"
                        onClicked: root.control("next")
                    }
                }

                Kirigami.Separator {
                    Layout.fillWidth: true
                    visible: popup.live && sourceRow.visible
                }

                // Where the daemon gets the track from. A button is checked
                // when the daemon says so, never on the click itself, so a
                // switch that didn't take is plain to see.
                RowLayout {
                    id: sourceRow
                    Layout.alignment: Qt.AlignHCenter
                    // An older daemon doesn't publish its source.
                    visible: root.sourcePref.length > 0
                    spacing: 0

                    PlasmaComponents.Label {
                        rightPadding: Kirigami.Units.smallSpacing
                        text: "Source"
                        opacity: 0.7
                    }
                    Repeater {
                        model: [
                            { value: "mpris", label: "Player",
                              hint: "Player metadata only. Nothing is recorded." },
                            { value: "auto", label: "Auto",
                              hint: "Player metadata when a player has it, else "
                                    + "listens: speaker output or the mic." },
                            { value: "loopback", label: "Speaker",
                              hint: "Identify whatever this machine is playing "
                                    + "by listening to its output." },
                            { value: "mic", label: "Mic",
                              hint: "Identify whatever is playing in the room "
                                    + "through the microphone." },
                        ]
                        delegate: PlasmaComponents.ToolButton {
                            text: modelData.label
                            // Not checkable: that would tick it on click,
                            // before the daemon has agreed to anything.
                            checked: root.sourcePref === modelData.value
                            onClicked: root.pickSource(modelData.value)
                            PlasmaComponents.ToolTip.text: modelData.hint
                            PlasmaComponents.ToolTip.visible: hovered
                            PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                        }
                    }
                }

                // The cost of the current choice, spelled out: listening sends
                // fingerprints to Shazam and lights the recording indicator.
                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    visible: sourceRow.visible
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    text: {
                        if (root.requestedSource.length) return "switching…";
                        switch (root.sourcePref) {
                        case "mpris": return "No audio capture.";
                        case "auto": return "Listens when no player describes the "
                            + "track: sent to Shazam, recording indicator on.";
                        case "loopback": return "Listening to the speaker output: "
                            + "sent to Shazam, recording indicator on.";
                        case "mic": return "Listening through the mic: sent to "
                            + "Shazam, recording indicator on.";
                        default: return "Listening to " + root.sourcePref
                            + ": sent to Shazam, recording indicator on.";
                        }
                    }
                }
            }
        }

        contentItem: Item {
            id: body

            // Set while the view is moved on the music's behalf, so the
            // contentY handler can tell that apart from the user scrolling.
            property bool steering: false
            property real seenContentHeight: 0
            readonly property bool holding:
                flick.moving || syncedView.T.ScrollBar.vertical.pressed
            // Scrolling away to read ahead shouldn't be yanked back on the
            // next line: hand the view back to the music a few seconds after
            // the user lets go.
            readonly property bool following: !holding && !resumeFollow.running

            // Keep the line being sung 40% of the way down rather than dead
            // centre: lyrics are read downward, so show more of what's coming.
            function follow(animate) {
                followAnim.stop();
                lines.forceLayout();
                var item = lineRepeater.itemAt(root.lineIndex);
                var target = item ? item.y + item.height / 2 - flick.height * 0.4 : 0;
                target = Math.max(0, Math.min(target, flick.contentHeight - flick.height));
                if (animate) {
                    followAnim.to = target;
                    followAnim.start();
                } else {
                    steering = true;
                    flick.contentY = target;
                    steering = false;
                }
            }

            function userScrolled() {
                followAnim.stop();
                resumeFollow.restart();
            }

            onHoldingChanged: if (holding) userScrolled()

            Timer {
                id: resumeFollow
                interval: 4000
                onTriggered: {
                    if (body.holding) restart();
                    else body.follow(true);
                }
            }

            Connections {
                target: root
                function onLineIndexChanged() {
                    if (root.expanded && body.following) body.follow(true);
                }
                function onExpandedChanged() {
                    if (!root.expanded) return;
                    // Reopening always lands on the line being sung.
                    resumeFollow.stop();
                    body.follow(false);
                    plainFlick.contentY = 0;
                }
                function onTrackKeyChanged() {
                    plainFlick.contentY = 0;
                }
            }

            NumberAnimation {
                id: followAnim
                target: flick
                property: "contentY"
                duration: 400
                easing.type: Easing.OutCubic
            }

            PlasmaComponents.ScrollView {
                id: syncedView
                anchors.fill: parent
                visible: popup.live && root.lyrics.length > 0

                // The Flickable must be the first child: anything declared
                // before it makes ScrollView wrap it in a Flickable of its own.
                Flickable {
                    id: flick
                    contentWidth: width
                    contentHeight: lines.height
                    boundsBehavior: Flickable.StopAtBounds

                    // Kirigami's wheel handling moves contentY directly, so the
                    // Flickable never reports it as movement. Any other scroll
                    // needs the pointer over the view; the music's never does.
                    HoverHandler {
                        id: lyricsHover
                    }

                    onContentYChanged: {
                        // A relayout (new track, rewrapped line) clamps contentY
                        // before contentHeightChanged fires; that's not the user.
                        var relayout = contentHeight !== body.seenContentHeight;
                        body.seenContentHeight = contentHeight;
                        if (lyricsHover.hovered && !relayout && !body.steering
                                && !followAnim.running) {
                            body.userScrolled();
                        }
                    }
                    onContentHeightChanged: {
                        body.seenContentHeight = contentHeight;
                        if (body.following) body.follow(false);
                    }
                    onHeightChanged: if (body.following) body.follow(false)

                    Column {
                        id: lines
                        width: flick.width
                        topPadding: Kirigami.Units.largeSpacing
                        bottomPadding: Kirigami.Units.largeSpacing
                        spacing: Kirigami.Units.smallSpacing

                        Repeater {
                            id: lineRepeater
                            // Same timings either way, so lineIndex holds.
                            model: root.showOriginal ? root.lyricsOriginal : root.lyrics
                            delegate: PlasmaComponents.Label {
                                readonly property bool isCurrent: index === root.lineIndex

                                width: lines.width
                                leftPadding: Kirigami.Units.largeSpacing
                                rightPadding: Kirigami.Units.largeSpacing
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                                textFormat: Text.PlainText
                                text: modelData[1] || "♪"
                                font.weight: isCurrent ? Font.DemiBold : Font.Normal
                                opacity: isCurrent ? 1.0 : 0.5
                                Behavior on opacity {
                                    NumberAnimation { duration: 200 }
                                }
                            }
                        }
                    }
                }
            }

            // No timings: the sheet as published, from the top. Nothing to
            // scroll it by, so it stays put.
            PlasmaComponents.ScrollView {
                anchors.fill: parent
                visible: popup.live && !syncedView.visible && root.lyricsPlain.length > 0

                Flickable {
                    id: plainFlick
                    contentWidth: width
                    contentHeight: plainText.height
                    boundsBehavior: Flickable.StopAtBounds

                    PlasmaComponents.Label {
                        id: plainText
                        width: plainFlick.width
                        padding: Kirigami.Units.largeSpacing
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        text: root.lyricsPlain
                    }
                }
            }

            PlasmaExtras.PlaceholderMessage {
                anchors.centerIn: parent
                width: parent.width - Kirigami.Units.gridUnit * 4
                visible: !syncedView.visible && root.lyricsPlain.length === 0
                iconName: root.stale ? "dialog-warning" : "media-optical-audio"
                text: {
                    if (root.stale) return "nowplaying is not running";
                    if (!root.hasTrack) return "Nothing playing";
                    // The daemon only says why once the lookup has answered.
                    return root.daemonMessage.length ? "No lyrics" : "Looking up lyrics…";
                }
                explanation: {
                    if (root.stale) return "The daemon has stopped writing its state.";
                    if (!root.hasTrack)
                        return root.status === "searching" ? "listening…" : "";
                    return root.daemonMessage;
                }
            }
        }
    }
}
