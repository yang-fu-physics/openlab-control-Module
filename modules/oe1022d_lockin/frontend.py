"""Source, Channel 1 and Channel 2 settings; the host supplies the Status tab."""

from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from labcontrol.ui.widgets import PrecisionDoubleSpinBox

from .backend import CHANNELS, EXTERNAL, SINE_OUT, default_settings
from .oe1022d import CHANNEL_SETTINGS


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        tabs = QTabWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(tabs)
        source_page = QWidget()
        self.form = QFormLayout(source_page)
        tabs.addTab(source_page, "Source")
        resources = api.resources()
        self.source = {
            "mode": self._choice([("External reference (read only)", EXTERNAL), ("OE1022D Sine Out", SINE_OUT)]),
            "lockin_resource": self._choice([("Select a VISA resource…", "")] + [
                (f"{item['address']} — {key}", key) for key, item in resources.items()
                if item["address"].upper().startswith("ASRL")
            ]),
            "baud": self._choice([(str(n), n) for n in (921600, 460800, 230400, 115200, 57600, 38400, 19200, 9600)]),
            "io_timeout": self._number(0.1, 1, 1),
            "frequency": self._number(0.001, 102000, 3),
            "sine_channel": self._choice([("A", 1), ("B", 2)]),
            "amplitude": QLineEdit(),
            "max_voltage": QLineEdit(),
            "settle_seconds": self._number(0, 3600, 1),
        }
        for key, label in (
            ("mode", "Excitation source"), ("lockin_resource", "OE1022D (VISA)"),
            ("baud", "OE1022D baud rate"),
            ("io_timeout", "I/O timeout (s)"), ("frequency", "Reference frequency (Hz)"),
            ("sine_channel", "Sine Out channel"), ("amplitude", "Initial RMS voltage"),
            ("max_voltage", "Sample voltage limit (V RMS)"),
            ("settle_seconds", "Settle before first reading (s)"),
        ):
            self.form.addRow(label, self.source[key])
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.form.addRow(self.note)
        self.source["mode"].currentIndexChanged.connect(self._source_changed)
        self.channels = []
        for title in ("Channel 1 (A)", "Channel 2 (B)"):
            page = QWidget()
            form = QFormLayout(page)
            tabs.addTab(page, title)
            controls = {}
            for key, _, label, options, _ in CHANNEL_SETTINGS:
                controls[key] = self._choice([(text, index) for index, text in enumerate(options)])
                form.addRow(label, controls[key])
            for key, label, widget in (
                ("phase", "Reference phase (°)", self._number(-180, 180, 2)),
                ("primary", "Primary harmonic order", self._number(1, 32767)),
                ("secondary", "Secondary harmonic order", self._number(1, 32767)),
            ):
                controls[key] = widget
                form.addRow(label, widget)
            self.channels.append(controls)
        self.status_widget = QLabel("No measurement yet")
        self.status_widget.setWordWrap(True)
        self.load({})

    @staticmethod
    def _choice(items):
        widget = QComboBox()
        widget.setMinimumContentsLength(24)
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for text, value in items:
            widget.addItem(text, value)
        return widget

    @staticmethod
    def _number(minimum, maximum, decimals=None):
        widget = QSpinBox() if decimals is None else PrecisionDoubleSpinBox()
        if decimals is not None:
            widget.setDecimals(decimals)
        widget.setRange(minimum, maximum)
        widget.setKeyboardTracking(False)
        return widget

    def _source_changed(self):
        standalone = self.source["mode"].currentData() == SINE_OUT
        for key in ("sine_channel", "amplitude", "max_voltage", "settle_seconds"):
            self.form.setRowVisible(self.source[key], standalone)
        self.source["amplitude"].setPlaceholderText("e.g. 10mV")
        self.note.setText(
            "Route the selected channel’s TTL Out to the other channel’s external reference. At SEQ end the selected Sine Out goes to DC 0 V."
            if standalone else "Connect both external references. This mode does not write to Sine Out."
        )

    def load(self, settings):
        values = default_settings() | dict(settings)
        for widgets, fields in ((self.source, values), *zip(self.channels, (values[name] for name in CHANNELS))):
            for key, widget in widgets.items():
                value = fields[key]
                if isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index < 0 and key.endswith("_resource"):
                        widget.addItem(f"Unavailable — {value}", value)
                        index = widget.count() - 1
                    if index < 0:
                        raise ValueError(f"{key}: unsupported choice {value!r}")
                    widget.setCurrentIndex(index)
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                else:
                    widget.setValue(value)
        self._source_changed()

    def dump(self):
        return self._values(self.source) | {
            name: self._values(widgets) for name, widgets in zip(CHANNELS, self.channels)
        }

    @staticmethod
    def _values(widgets):
        return {
            key: widget.currentData() if isinstance(widget, QComboBox)
            else widget.text() if isinstance(widget, QLineEdit)
            else widget.value()
            for key, widget in widgets.items()
        }

    def show_status(self, status):
        self.status_widget.setText("\n".join(f"{key}: {'—' if value is None else value}" for key, value in status.items()))
