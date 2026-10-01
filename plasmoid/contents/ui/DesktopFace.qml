pragma ComponentBehavior: Bound

/*
 * The widget on the desktop: the lyrics sheet laid straight on the wallpaper,
 * the line being sung held a little above the middle while the rest fade out
 * towards the edges, with the track above and a progress hairline below.
 * White with a soft shadow so it reads over a photo; theme colours once the
 * widget is given a background from its edit handle. Type scales with the
 * widget, so resizing it is how the words get bigger.
 *
 * Everything shown comes from the applet in main.qml, which owns the state
 * feed and the line timing -- so the panel, the popup and this always agree.
 */
import QtQuick
import QtQuick.Effects
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Item {
    id: face

    // The PlasmoidItem in main.qml: the state, and the controls to act on it.
    required property var applet
    // Nothing behind us but the wallpaper.
    property bool overWallpaper: true
    // The shadow, the rounded cover and the faded edges are GPU effects; a
    // software-rendered desktop would draw nothing at all through them.
    property bool effects: GraphicsInfo.api !== GraphicsInfo.Software

    Layout.minimumWidth: Kirigami.Units.gridUnit * 14
    Layout.minimumHeight: Kirigami.Units.gridUnit * 9
    Layout.preferredWidth: Kirigami.Units.gridUnit * 30
    Layout.preferredHeight: Kirigami.Units.gridUnit * 20

    readonly property color ink: overWallpaper ? "white" : Kirigami.Theme.textColor
    readonly property color alarm: overWallpaper
        ? Qt.lighter(Kirigami.Theme.negativeTextColor, 1.3)
        : Kirigami.Theme.negativeTextColor

    readonly property real lyricSize: Math.max(Kirigami.Theme.defaultFont.pixelSize * 1.2,
                                               Math.min(height / 12, width / 18))
    readonly property real metaSize: Math.max(Kirigami.Theme.smallFont.pixelSize,
                                              lyricSize * 0.48)

    // One thing at a time, picked by the same rules as the panel strip.
    readonly property string mode: {
        if (applet.stale) return "stale";
        if (applet.showFleet) return "fleet";
        // Paused long enough (or pinned without any homelab data): the
        // daemon has handed the space back, so don't hold the lyrics up.
        if (!applet.hasTrack || applet.idleActive || applet.pinFleet) return "quiet";
        if (applet.lyrics.length > 0) return "synced";
        if (applet.lyricsPlain.length > 0) return "plain";
        return "missing";
    }
    readonly property bool showsTrack:
        mode === "synced" || mode === "plain" || mode === "missing"

    HoverHandler {
        id: hover
    }

    // Middle-click pins the homelab readout, as it does in the panel.
    TapHandler {
        acceptedButtons: Qt.MiddleButton
        onTapped: face.applet.togglePin()
    }

    component GlyphButton: Item {
        id: glyph

        property string iconName
        property string label
        property bool checked: true
        signal activated()

        implicitWidth: face.metaSize * 2.4
        implicitHeight: implicitWidth
        Accessible.role: Accessible.Button
        Accessible.name: label

        Kirigami.Icon {
            anchors.centerIn: parent
            width: Math.round(face.metaSize * 1.6)
            height: width
            source: glyph.iconName
            isMask: true
            color: face.ink
            opacity: glyphHover.hovered ? 1 : (glyph.checked ? 0.8 : 0.4)
        }
        HoverHandler {
            id: glyphHover
            cursorShape: Qt.PointingHandCursor
        }
        TapHandler {
            onTapped: glyph.activated()
        }
    }

    component Words: Text {
        color: face.ink
        textFormat: Text.PlainText
        elide: Text.ElideRight
    }

    Item {
        id: content
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing

        // One shadow under everything, so no single word needs its own.
        layer.enabled: face.effects && face.overWallpaper
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: "#99000000"
            shadowBlur: 0.6
            shadowVerticalOffset: 2
        }

        ColumnLayout {
            anchors.fill: parent
            visible: face.showsTrack
            spacing: face.metaSize * 0.8

            // --- the track: cover beside title and artist, centred as one
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(coverSlot.visible ? coverSlot.side : 0,
                                                 trackWords.implicitHeight)

                Row {
                    id: trackRow
                    anchors.centerIn: parent
                    spacing: face.metaSize

                    Item {
                        id: coverSlot
                        readonly property real side: Math.round(face.metaSize * 3.6)
                        width: side
                        height: side
                        anchors.verticalCenter: parent.verticalCenter
                        // No art, no placeholder: the title stands alone.
                        visible: cover.status === Image.Ready

                        Image {
                            id: cover
                            anchors.fill: parent
                            source: face.applet.coverFile.length
                                ? "file://" + face.applet.coverFile : ""
                            sourceSize.width: coverSlot.side * 2
                            sourceSize.height: coverSlot.side * 2
                            fillMode: Image.PreserveAspectCrop
                            smooth: true
                            asynchronous: true
                            visible: !face.effects
                        }
                        MultiEffect {
                            anchors.fill: cover
                            source: cover
                            visible: face.effects
                            maskEnabled: true
                            maskSource: coverMask
                            maskThresholdMin: 0.5
                            maskSpreadAtMin: 1.0
                        }
                        Rectangle {
                            id: coverMask
                            anchors.fill: cover
                            radius: coverSlot.side * 0.08
                            layer.enabled: true
                            visible: false
                        }
                    }

                    Column {
                        id: trackWords
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: face.metaSize * 0.2
                        // As wide as the words, and no wider than the room the
                        // cover leaves, so the pair centres and long ones elide.
                        width: Math.min(Math.ceil(Math.max(title.implicitWidth, byline.implicitWidth)),
                                        trackRow.parent.width
                                        - (coverSlot.visible ? coverSlot.side + trackRow.spacing : 0))

                        Words {
                            id: title
                            width: parent.width
                            text: face.applet.title
                            font.pixelSize: face.metaSize * 1.3
                            font.weight: Font.DemiBold
                        }
                        Words {
                            id: byline
                            width: parent.width
                            visible: text.length > 0
                            text: [face.applet.artist, face.applet.album]
                                .filter(function (s) { return s.length > 0; })
                                .join("  ·  ").toUpperCase()
                            font.pixelSize: face.metaSize
                            font.letterSpacing: face.metaSize * 0.12
                            opacity: 0.8
                        }
                    }
                }
            }

            // --- the words
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true

                Item {
                    id: stage
                    anchors.fill: parent
                    visible: face.mode === "synced"
                    clip: true

                    // Transparent at the top and bottom edges, so lines fade
                    // in and out of view however tall they wrap.
                    layer.enabled: face.effects
                    layer.effect: MultiEffect {
                        maskEnabled: true
                        maskSource: stageMask
                        // Smoothstep across the whole gradient; left at
                        // its defaults the mask is a hard cut-off.
                        maskThresholdMin: 0.5
                        maskSpreadAtMin: 1.0
                    }

                    // A little above the middle: lyrics are read downward, so
                    // show more of what's coming than of what's gone.
                    readonly property real focusY: height * 0.42
                    // The line the sheet was last moved to, to tell the next
                    // line (slide) from a seek or a new track (jump).
                    property int shownIndex: -1

                    function follow(animate) {
                        followAnim.stop();
                        sheet.forceLayout();
                        var item = lineRepeater.itemAt(Math.max(0, face.applet.lineIndex));
                        if (!item) return;
                        var target = focusY - (item.y + item.height / 2);
                        if (animate) {
                            followAnim.to = target;
                            followAnim.start();
                        } else {
                            sheet.y = target;
                        }
                    }

                    onHeightChanged: follow(false)
                    onVisibleChanged: if (visible) follow(false)

                    Connections {
                        target: face.applet
                        function onLineIndexChanged() {
                            var i = face.applet.lineIndex;
                            stage.follow(Math.abs(i - stage.shownIndex) === 1);
                            stage.shownIndex = i;
                        }
                    }

                    NumberAnimation {
                        id: followAnim
                        target: sheet
                        property: "y"
                        duration: 400
                        easing.type: Easing.OutCubic
                    }

                    Column {
                        id: sheet
                        width: stage.width
                        spacing: face.lyricSize * 0.4
                        // New lines, or the same lines rewrapped: land on the
                        // line being sung without sliding there.
                        onHeightChanged: stage.follow(false)

                        Repeater {
                            id: lineRepeater
                            // Same timings either way, so lineIndex holds.
                            model: face.applet.showOriginal
                                ? face.applet.lyricsOriginal : face.applet.lyrics

                            delegate: Words {
                                required property int index
                                required property var modelData

                                // Every line in the same weight and size, so
                                // becoming current never rewraps a line and
                                // shoves the sheet under the slide.
                                property real lit: index === face.applet.lineIndex ? 1 : 0
                                Behavior on lit {
                                    NumberAnimation { duration: 250 }
                                }
                                // Without the mask: near 1 by the focus
                                // line, 0 by the time the line's far edge
                                // reaches the stage's, so a wrapped line fades
                                // out whole rather than sliced by the clip.
                                readonly property real lineTop: sheet.y + y
                                readonly property real fade: lineTop + height / 2 < stage.focusY
                                    ? Math.max(0, Math.min(1, lineTop / Math.max(1, stage.focusY)))
                                    : Math.max(0, Math.min(1, (stage.height - lineTop - height)
                                        / Math.max(1, stage.height - stage.focusY)))

                                width: sheet.width
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                                elide: Text.ElideNone
                                text: modelData[1] || "♪"
                                font.pixelSize: face.lyricSize
                                font.weight: Font.DemiBold
                                opacity: lit + (1 - lit)
                                    * (face.effects ? 0.6 : 0.75 * Math.sqrt(fade))
                                scale: 0.94 + 0.06 * lit
                            }
                        }
                    }
                }

                Rectangle {
                    id: stageMask
                    anchors.fill: stage
                    visible: false
                    layer.enabled: true
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "transparent" }
                        GradientStop { position: 0.22; color: "white" }
                        GradientStop { position: 0.7; color: "white" }
                        GradientStop { position: 1.0; color: "transparent" }
                    }
                }

                // No timings: the sheet as published, from the top, to scroll
                // by hand.
                Flickable {
                    id: plainFlick
                    anchors.fill: parent
                    visible: face.mode === "plain"
                    clip: true
                    contentWidth: width
                    contentHeight: plainText.height
                    boundsBehavior: Flickable.StopAtBounds

                    Connections {
                        target: face.applet
                        function onTrackKeyChanged() { plainFlick.contentY = 0; }
                    }

                    Words {
                        id: plainText
                        width: plainFlick.width
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        elide: Text.ElideNone
                        text: face.applet.lyricsPlain
                        font.pixelSize: face.lyricSize * 0.8
                        lineHeight: 1.2
                        opacity: 0.85
                    }
                }

                ColumnLayout {
                    anchors.centerIn: parent
                    width: parent.width
                    visible: face.mode === "missing"
                    spacing: face.metaSize * 0.3

                    Words {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        // The daemon only says why once the lookup has answered.
                        text: face.applet.daemonMessage.length ? "No lyrics" : "Looking up lyrics…"
                        font.pixelSize: face.lyricSize * 0.8
                        opacity: 0.7
                    }
                    Words {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        visible: text.length > 0
                        text: face.applet.daemonMessage
                        font.pixelSize: face.metaSize
                        opacity: 0.55
                    }
                }
            }

            // --- on hover: where we are, and the player's controls
            // Its room is kept while hidden: showing it must not shrink the
            // stage and jolt the sheet.
            RowLayout {
                Layout.fillWidth: true
                // A button's height, buttons or not, for the same reason.
                Layout.preferredHeight: face.metaSize * 2.4
                spacing: face.metaSize * 0.4
                opacity: hover.hovered ? 1 : 0
                enabled: hover.hovered
                Behavior on opacity {
                    NumberAnimation { duration: 200 }
                }

                // Tabular figures, so the times don't twitch as digits change.
                Words {
                    text: face.applet.clockText(face.applet.duration > 0
                        ? Math.min(face.applet.nowPos, face.applet.duration)
                        : face.applet.nowPos)
                    font.pixelSize: face.metaSize
                    font.features: ({ "tnum": 1 })
                    opacity: 0.75
                }

                Item { Layout.fillWidth: true }

                // Only when there's a local player to obey them: a Plex
                // client on another device is out of playerctl's reach.
                GlyphButton {
                    visible: face.applet.player.length > 0
                    iconName: "media-skip-backward"
                    label: "Previous track"
                    onActivated: face.applet.control("previous")
                }
                GlyphButton {
                    visible: face.applet.player.length > 0
                    iconName: face.applet.playing ? "media-playback-pause"
                                                  : "media-playback-start"
                    label: face.applet.playing ? "Pause" : "Play"
                    onActivated: face.applet.control("play-pause")
                }
                GlyphButton {
                    visible: face.applet.player.length > 0
                    iconName: "media-skip-forward"
                    label: "Next track"
                    onActivated: face.applet.control("next")
                }
                GlyphButton {
                    visible: face.mode === "synced" && face.applet.lyricsOriginal.length > 0
                    iconName: "character-set"
                    label: "Original script"
                    checked: face.applet.showOriginal
                    onActivated: face.applet.setOriginalScript(!face.applet.showOriginal)
                }

                Item { Layout.fillWidth: true }

                Words {
                    visible: face.applet.duration > 0
                    text: face.applet.clockText(face.applet.duration)
                    font.pixelSize: face.metaSize
                    font.features: ({ "tnum": 1 })
                    opacity: 0.75
                }
            }

            // --- progress hairline; an unknown length has nothing to be a
            // fraction of.
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(2, Math.round(face.metaSize * 0.14))
                visible: face.applet.duration > 0

                Rectangle {
                    anchors.fill: parent
                    radius: height / 2
                    color: face.ink
                    opacity: 0.25
                }
                Rectangle {
                    height: parent.height
                    width: parent.width * Math.max(0, Math.min(1,
                        face.applet.nowPos / Math.max(1, face.applet.duration)))
                    radius: height / 2
                    color: face.ink
                    opacity: 0.85
                }
            }
        }

        // --- nothing to sing along to: the homelab readout, or why not
        RowLayout {
            id: readout
            anchors.centerIn: parent
            width: Math.min(implicitWidth, parent.width)
            visible: !face.showsTrack
            spacing: face.metaSize

            readonly property bool fleet: face.mode === "fleet"
            readonly property color tint: fleet && !face.applet.idleOk ? face.alarm : face.ink

            Kirigami.Icon {
                Layout.preferredWidth: Math.round(face.lyricSize * 2.2)
                Layout.preferredHeight: Layout.preferredWidth
                source: readout.fleet ? Qt.resolvedUrl("homelab.svg")
                    : face.mode === "stale" ? "dialog-warning" : "media-optical-audio"
                // Lets the colour follow the words, red included.
                isMask: true
                color: readout.tint
                opacity: readout.fleet ? 1 : 0.6
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                Words {
                    Layout.fillWidth: true
                    text: {
                        switch (face.mode) {
                        case "stale": return "nowplaying is not running";
                        case "fleet": return face.applet.idleLine1;
                        }
                        if (face.applet.hasTrack) return face.applet.trackLabel;
                        return face.applet.status === "searching" ? "Listening…"
                                                                  : "Nothing playing";
                    }
                    color: readout.tint
                    font.pixelSize: face.lyricSize * 0.9
                    font.weight: Font.DemiBold
                    opacity: readout.fleet ? 1 : 0.7
                }
                Words {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: {
                        switch (face.mode) {
                        case "stale": return "The daemon has stopped writing its state.";
                        case "fleet": return face.applet.idleLine2;
                        }
                        return face.applet.hasTrack && face.applet.status === "paused"
                            ? "paused" : "";
                    }
                    font.pixelSize: face.metaSize
                    opacity: 0.6
                }
            }
        }
    }
}
