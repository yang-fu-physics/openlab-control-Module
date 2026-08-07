from __future__ import annotations

"""独立模块仓库的最小 QWidget 前端示例。"""

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import ModuleUIAPI


class SimulatedTransportFrontend(QWidget):
    def __init__(self, api: ModuleUIAPI) -> None:
        super().__init__()
        self.api = api
        layout = QVBoxLayout(self)
        group = QGroupBox("Transport Settings")
        form = QFormLayout(group)
        self.excitation = QDoubleSpinBox()
        self.excitation.setRange(0.001, 100.0)
        self.excitation.setDecimals(3)
        self.excitation.setSuffix(" mA")
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.0, 60.0)
        self.delay.setDecimals(3)
        self.delay.setSuffix(" s/channel")
        self.noise = QDoubleSpinBox()
        self.noise.setRange(0.0, 10.0)
        self.noise.setDecimals(6)
        self.noise.setSuffix(" Ohm")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.000001, 1e12)
        self.threshold.setDecimals(6)
        self.threshold.setSuffix(" Ohm")
        form.addRow("Excitation current", self.excitation)
        form.addRow("Channel delay", self.delay)
        form.addRow("Simulated noise", self.noise)
        form.addRow("Warning threshold", self.threshold)
        layout.addWidget(group)
        note = QLabel(
            "Enabling loads these values but does not apply them. "
            "Check them, then use Apply Settings."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        self.status_widget = QWidget()
        status_layout = QVBoxLayout(self.status_widget)
        status_group = QGroupBox("Instrument Status")
        status_form = QFormLayout(status_group)
        self.status_labels: dict[str, QLabel] = {}
        for name in (
            "Connection",
            "Applied Settings",
            "Sequence",
            "Output",
            "Last Channel",
            "Last Resistance (Ohm)",
            "Last Action",
        ):
            label = QLabel("—")
            self.status_labels[name] = label
            status_form.addRow(name, label)
        status_layout.addWidget(status_group)
        buttons = QHBoxLayout()
        self.test_button = QPushButton("Test Connection")
        self.measure_button = QPushButton("Measure Now")
        self.refresh_button = QPushButton("Refresh Status")
        self.test_button.clicked.connect(lambda: api.action("test_connection"))
        self.measure_button.clicked.connect(lambda: api.action("measure_now"))
        self.refresh_button.clicked.connect(api.refresh)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.measure_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        status_layout.addLayout(buttons)
        status_layout.addStretch(1)
        self.load({})

    def dump(self) -> dict[str, Any]:
        return {
            "excitation_current_mA": self.excitation.value(),
            "delay_seconds": self.delay.value(),
            "noise_ohm": self.noise.value(),
            "warning_threshold_ohm": self.threshold.value(),
        }

    def load(self, settings: Mapping[str, Any]) -> None:
        values = {
            "excitation_current_mA": 1.0,
            "delay_seconds": 0.04,
            "noise_ohm": 0.0005,
            "warning_threshold_ohm": 10.0,
            **dict(settings),
        }
        widgets = (
            (self.excitation, "excitation_current_mA"),
            (self.delay, "delay_seconds"),
            (self.noise, "noise_ohm"),
            (self.threshold, "warning_threshold_ohm"),
        )
        blockers = [QSignalBlocker(widget) for widget, _key in widgets]
        for widget, key in widgets:
            widget.setValue(float(values[key]))
        del blockers

    def show_status(self, status: Mapping[str, Any]) -> None:
        for key, value in status.items():
            label = self.status_labels.get(str(key))
            if label is not None:
                label.setText(f"{value:.9g}" if isinstance(value, float) else str(value))


Frontend = SimulatedTransportFrontend
