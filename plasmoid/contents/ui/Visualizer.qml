pragma ComponentBehavior: Bound

/*
 * Bars for whatever the speaker is playing, with a cap above each that the
 * bar throws up and gravity brings back -- the TUI's visualizer, for the
 * desktop.
 *
 * QML can't capture audio, so the daemon listens on our behalf and streams
 * the bars' heights over localhost (nowplaying/visualizer.py): one line a
 * frame, each bar and then each cap a character from "0" up. Each answer
 * runs a couple of seconds and is asked for again, which also keeps the
 * daemon listening; hang up and it stops a few seconds later.
 */
import QtQuick

Item {
    id: vis

    // The daemon's feed, http://127.0.0.1:<port>/<token>; empty if unknown.
    property string url
    // Streaming, and so keeping the daemon listening.
    property bool active: false
    property int bars: 32
    property color color: "white"
    // The url is missing, or stopped answering: the daemon may have
    // restarted on a new port. Whoever set it should look again.
    signal needEndpoint()

    readonly property bool streaming: _xhr !== null

    property var _xhr: null
    // How far into the current answer has been read.
    property int _seen: 0

    function _hangUp() {
        if (_xhr) {
            _xhr.onreadystatechange = null;
            _xhr.abort();
            _xhr = null;
        }
    }

    function _connect() {
        _hangUp();
        retry.stop();
        if (!active) return;
        if (!url.length) {
            needEndpoint();
            retry.start();
            return;
        }
        var xhr = new XMLHttpRequest();
        _seen = 0;
        xhr.onreadystatechange = function () {
            if (xhr !== vis._xhr) return;
            if (xhr.readyState >= XMLHttpRequest.LOADING && xhr.status === 200)
                vis._read(xhr.responseText);
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            vis._xhr = null;
            if (xhr.status === 200) {
                vis._connect();          // the answer ran its course: ask again
            } else {
                vis.needEndpoint();      // refused, or nobody there
                retry.start();
            }
        };
        xhr.open("GET", url + "/" + bars);
        xhr.send();
        _xhr = xhr;
    }

    // The newest whole line, if it's new; the rest went by unseen. Set on
    // each bar directly: sixty times a second, a fresh array for every bar's
    // bindings to pick through costs more than the drawing.
    function _read(text) {
        var end = text.lastIndexOf("\n");
        if (end < 0 || end <= _seen) return;
        var start = text.lastIndexOf("\n", end - 1) + 1;
        _seen = end;
        if (end - start !== bars * 2 || slots.count !== bars) return;
        for (var i = 0; i < bars; i++) {
            var slot = slots.itemAt(i);
            slot.level = (text.charCodeAt(start + i) - 48) / 63;
            slot.cap = (text.charCodeAt(start + bars + i) - 48) / 63;
        }
    }

    function _settle() {
        for (var i = 0; i < slots.count; i++) {
            var slot = slots.itemAt(i);
            slot.level = 0;
            slot.cap = 0;
        }
    }

    onActiveChanged: {
        if (active) {
            _connect();
        } else {
            _hangUp();
            retry.stop();
            // Let the bars sink rather than freeze where they were.
            _settle();
        }
    }
    onUrlChanged: if (active) _connect()
    onBarsChanged: if (active) _connect()
    Component.onDestruction: _hangUp()

    Timer {
        id: retry
        interval: 3000
        onTriggered: vis._connect()
    }

    Repeater {
        id: slots
        model: vis.bars

        delegate: Item {
            id: slot
            required property int index

            readonly property real pitch: vis.width / vis.bars
            // Set from the feed, by _read.
            property real level: 0
            property real cap: 0
            readonly property real capH: Math.max(2, Math.round(vis.height * 0.03))

            x: index * pitch + pitch * 0.18
            width: pitch * 0.64
            height: vis.height

            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: slot.level * (vis.height - slot.capH * 2)
                // A silent band is no bar at all, not a stub on the floor.
                visible: height >= 1
                radius: Math.min(width / 2, 2)
                color: vis.color
                opacity: 0.55
                // Streaming frames are already smooth; only the fall at the
                // end needs easing.
                Behavior on height {
                    enabled: !vis.streaming
                    NumberAnimation { duration: 500; easing.type: Easing.OutCubic }
                }
            }
            Rectangle {
                y: vis.height - slot.capH - slot.cap * (vis.height - slot.capH * 2)
                width: parent.width
                height: slot.capH
                radius: height / 2
                color: vis.color
                opacity: slot.cap > 0 ? 0.9 : 0
                Behavior on y {
                    enabled: !vis.streaming
                    NumberAnimation { duration: 500; easing.type: Easing.OutCubic }
                }
            }
        }
    }
}
