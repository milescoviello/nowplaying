/*
 * nowplaying panel ticker
 *
 * Reads the state file the daemon mirrors to disk (QML has no unix socket API)
 * and scrolls lyrics upward through a strip in the panel: the line being sung
 * on top, the line coming next below it. Each new line slides in from the
 * bottom, timed off the track position rather than a fixed speed.
 */
import QtCore
import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    // Always render inline in the panel -- never collapse to an icon.
    preferredRepresentation: fullRepresentation

    // QML's XMLHttpRequest refuses to read local files unless the whole session
    // runs with QML_XHR_ALLOW_FILE_READ=1, so the state file is read through
    // Plasma's executable data source instead. Polled once a second -- the
    // position is interpolated locally in between, so the scroll stays smooth.
    readonly property string statePath:
        StandardPaths.writableLocation(StandardPaths.GenericCacheLocation)
            .toString().replace("file://", "")
        + "/nowplaying/state.json"
    readonly property string readCommand: "cat " + statePath

    property var lyrics: []
    property string status: "idle"
    property string artist: ""
    property string title: ""
    property string coverFile: ""
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

    readonly property bool stale:
        writtenAt > 0 && (Date.now() / 1000 - writtenAt) > 20
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
        if (newKey !== trackKey) {
            trackKey = newKey;
            lyrics = d.lyrics || [];
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

    Timer {  // drives the current line, the slide and the sideways creep
        interval: 20
        running: root.playing && root.lyrics.length > 0
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshLine()
    }

    // --- panel strip --------------------------------------------------------
    fullRepresentation: Item {
        id: strip

        readonly property int tickerWidth: plasmoid.configuration.tickerWidth

        // Preferred, not forced: pinning min == max makes a full panel overflow
        // and shove the trailing applets off the screen edge.
        Layout.minimumWidth: Kirigami.Units.iconSizes.small * 3
        Layout.preferredWidth: tickerWidth
        Layout.maximumWidth: tickerWidth
        Layout.fillHeight: true

        // Middle-click anywhere on the widget toggles the pin. Only the middle
        // button is accepted, so left/right clicks still reach the panel.
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
                    // Downloaded at 400x400; let Qt scale it once, smoothly.
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
}
