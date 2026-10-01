import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    property alias cfg_tickerWidth: widthSpin.value
    property alias cfg_leadInMs: leadSpin.value
    property alias cfg_visualizer: visualizerBox.checked

    QQC2.SpinBox {
        id: widthSpin
        Kirigami.FormData.label: i18n("Ticker width in a panel (px):")
        from: 120
        to: 1600
        stepSize: 20
    }

    QQC2.SpinBox {
        id: leadSpin
        Kirigami.FormData.label: i18n("Lead-in (ms):")
        from: 0
        to: 4000
        stepSize: 100
    }

    QQC2.Label {
        text: i18n("Lines slide up this far ahead of being sung.\nRaise it if the words still arrive late.")
        opacity: 0.7
        font: Kirigami.Theme.smallFont
    }

    QQC2.CheckBox {
        id: visualizerBox
        Kirigami.FormData.label: i18n("On the desktop:")
        text: i18n("Visualizer")
    }

    QQC2.Label {
        text: i18n("While a track plays, the daemon listens to the speaker output\nfor the bars, so the recording indicator is on. Nothing leaves\nthis machine.")
        opacity: 0.7
        font: Kirigami.Theme.smallFont
    }
}
