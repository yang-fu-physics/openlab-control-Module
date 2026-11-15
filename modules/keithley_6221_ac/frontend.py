"""Settings for an independent AC current source."""

from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QWidget

from labcontrol.ui.widgets import PrecisionDoubleSpinBox

from .backend import default_settings


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        form = QFormLayout(self)
        self.resource = QComboBox()
        self.resource.setMinimumContentsLength(24)
        self.resource.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.resource.addItem("Select a VISA resource…", "")
        for key, item in api.resources().items():
            self.resource.addItem(f"{item['address']} — {key}", key)
        form.addRow("6221 (VISA)", self.resource)
        self.limit = QLineEdit()
        self.limit.setPlaceholderText("e.g. 100uA")
        form.addRow("Sample current limit (A RMS)", self.limit)
        self.ramp_step = QLineEdit()
        self.ramp_step.setPlaceholderText("e.g. 10uA")
        form.addRow("Maximum ramp step (A RMS)", self.ramp_step)
        self.line = QSpinBox()
        self.line.setRange(1, 6)
        form.addRow("Phase Marker line", self.line)
        self.numbers = {}
        for key, label, minimum, maximum, decimals in (
            ("io_timeout", "I/O timeout (s)", 0.1, 1, 1),
            ("ramp_interval", "Ramp step interval (s)", 0.1, 3600, 1),
            ("frequency", "Frequency (Hz)", 0.001, 100000, 3),
            ("compliance", "Voltage compliance (V)", 0.1, 105, 2),
        ):
            box = PrecisionDoubleSpinBox()
            box.setDecimals(decimals)
            box.setRange(minimum, maximum)
            box.setKeyboardTracking(False)
            self.numbers[key] = box
            form.addRow(label, box)
        note = QLabel("Scan Current sets the RMS excitation. Negative points move the reference marker to 180°, not the physical waveform.")
        note.setWordWrap(True)
        form.addRow(note)
        self.status_widget = QLabel("No measurement yet")
        self.status_widget.setWordWrap(True)
        self.load({})

    def load(self, settings):
        values = default_settings() | dict(settings)
        index = self.resource.findData(values["resource"])
        if index < 0:
            self.resource.addItem(f"Unavailable — {values['resource']}", values["resource"])
            index = self.resource.count() - 1
        self.resource.setCurrentIndex(index)
        self.limit.setText(str(values["max_current"]))
        self.ramp_step.setText(str(values["ramp_step"]))
        self.line.setValue(values["marker_line"])
        for key, box in self.numbers.items():
            box.setValue(values[key])

    def dump(self):
        return {
            "resource": self.resource.currentData(), "max_current": self.limit.text(),
            "ramp_step": self.ramp_step.text(),
            "marker_line": self.line.value(), **{key: box.value() for key, box in self.numbers.items()},
        }

    def show_status(self, status):
        self.status_widget.setText("\n".join(f"{key}: {'—' if value is None else value}" for key, value in status.items()))
