pragma ComponentBehavior: Bound

/*
 * Whether a window lies over the whole of the desktop widget.
 *
 * The desktop draws whether anyone can see it or not: left running behind a
 * maximized browser, the sheet's 20 ms line timer and the visualizer (and the
 * daemon's capture feeding it) kept plasmashell at a quarter of a core and the
 * GPU awake through a whole Instagram session, for a widget nobody could see.
 * main.qml and DesktopFace.qml pause both while `covered` holds.
 *
 * Asks the task manager -- the model Plasma's own panel uses to notice windows
 * touching it -- for the windows on this desktop and activity that overlap the
 * widget, then checks whether any one of them contains it outright. Partly
 * covered still counts as seen. Windows in `seeThrough` never cover: a
 * translucent terminal shows the widget behind it.
 */
import QtQuick
import org.kde.kwindowsystem
import org.kde.taskmanager as TaskManager

Item {
    id: watch

    // The widget, and the screen its desktop is on (global, logical pixels;
    // the desktop window sits at that screen's origin).
    required property Item target
    property rect screenGeometry

    // App ids (lower case, no .desktop) of windows drawn see-through.
    property var seeThrough: ["alacritty"]

    // Debounced on the way in only: a dialog flashes up mid-screen before
    // KWin places it, and a window dragged across shouldn't stop the bars.
    // Uncovering resumes at once.
    property bool covered: false
    readonly property bool coveredNow: _covering && !KWindowSystem.showingDesktop
    onCoveredNowChanged: {
        if (coveredNow) settle.restart();
        else { settle.stop(); covered = false; }
    }
    Timer {
        id: settle
        interval: 500
        onTriggered: watch.covered = watch.coveredNow
    }

    // Where the widget is on screen. Nothing signals when the containment
    // moves an applet, so it is looked up again every two seconds -- cheap,
    // and only assigned when it changed, so the model refilters rarely.
    property rect area
    function locate() {
        if (!target || !target.visible || target.width <= 0) return;
        var p = target.mapToItem(null, 0, 0);
        var r = Qt.rect(Math.round(screenGeometry.x + p.x),
                        Math.round(screenGeometry.y + p.y),
                        Math.round(target.width), Math.round(target.height));
        if (r.x !== area.x || r.y !== area.y
                || r.width !== area.width || r.height !== area.height)
            area = r;
    }
    Timer {
        interval: 2000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: watch.locate()
    }
    Connections {
        target: watch.target
        function onWidthChanged() { watch.locate(); }
        function onHeightChanged() { watch.locate(); }
    }
    onScreenGeometryChanged: locate()

    TaskManager.ActivityInfo {
        id: activityInfo
    }

    property bool _covering: false
    function recount() {
        var any = false;
        for (var i = 0; i < overlapping.count; ++i) {
            var w = overlapping.objectAt(i);
            if (w && w.hides(area)) { any = true; break; }
        }
        _covering = any;
    }
    onAreaChanged: Qt.callLater(recount)

    Instantiator {
        id: overlapping
        model: TaskManager.TasksModel {
            filterByCurrentVirtualDesktop: true
            filterByActivity: true
            filterByScreen: false
            filterByRegion: watch.area.width > 0
                ? TaskManager.RegionFilterMode.Intersect
                : TaskManager.RegionFilterMode.Disabled
            filterHidden: true
            filterMinimized: true
            activity: activityInfo.currentActivity
            regionGeometry: watch.area
            groupMode: TaskManager.TasksModel.GroupDisabled
        }
        delegate: QtObject {
            required property var model
            readonly property rect geometry: model.Geometry
            readonly property string app:
                String(model.AppId || "").toLowerCase().replace(/\.desktop$/, "")

            function hides(r) {
                if (r.width <= 0 || watch.seeThrough.indexOf(app) >= 0) return false;
                return geometry.x <= r.x && geometry.y <= r.y
                    && geometry.x + geometry.width >= r.x + r.width
                    && geometry.y + geometry.height >= r.y + r.height;
            }
            onGeometryChanged: Qt.callLater(watch.recount)
        }
        onObjectAdded: Qt.callLater(watch.recount)
        onObjectRemoved: Qt.callLater(watch.recount)
    }
}
