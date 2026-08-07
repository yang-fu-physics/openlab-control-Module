"""Keithley 6517B 高电阻模块的 PySide6 前端。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QSize, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import ModuleUIAPI

from .constants import (
    SOURCE_RANGE_1000_V,
    SOURCE_RANGE_100_V,
    default_settings,
)
from .quantities import format_quantity


class _SettingsPage(QWidget):
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(820, 640)


class _QuantityEdit(QLineEdit):
    def __init__(self, unit: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.unit = unit
        self.setPlaceholderText(f"SI value ({unit}); e.g. 100m")
        self.editingFinished.connect(self._normalize)

    def set_quantity(self, value: object) -> None:
        try:
            text = format_quantity(value, expected_unit=self.unit)
        except (TypeError, ValueError):
            text = str(value)
        self.setText(text)
        self.setStyleSheet("")

    def _normalize(self) -> None:
        try:
            text = format_quantity(self.text(), expected_unit=self.unit)
        except ValueError:
            self.setStyleSheet("border: 1px solid #b71c1c;")
            return
        blocker = QSignalBlocker(self)
        self.setText(text)
        del blocker
        self.setStyleSheet("")


class Keithley6517BFrontend(QWidget):
    """6517B desired settings 与只读安全状态显示。"""

    def __init__(self, api: ModuleUIAPI) -> None:
        super().__init__()
        self.api = api
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_settings(self))
        self.status_widget = self._build_status_widget()

    def _build_settings(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = _SettingsPage(parent)
        outer = QVBoxLayout(page)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        content = QWidget(scroll)
        layout = QVBoxLayout(content)

        communication = QGroupBox("GPIB Communication", content)
        communication_layout = QGridLayout(communication)
        self.resource = QComboBox()
        self.resource.setEditable(True)
        self.resource.setMinimumContentsLength(24)
        self.refresh_resources_button = QPushButton("Refresh GPIB")
        self.refresh_resources_button.clicked.connect(
            lambda: self.api.action("refresh_resources")
        )
        self.test_connection_button = QPushButton("Test Connection")
        self.test_connection_button.clicked.connect(
            lambda: self.api.action(
                "test_connection", {"settings": self.dump()}
            )
        )
        self.io_timeout = QDoubleSpinBox()
        self.io_timeout.setRange(0.1, 30.0)
        self.io_timeout.setDecimals(1)
        self.io_timeout.setSingleStep(0.5)
        self.io_timeout.setSuffix(" s")
        communication_layout.addWidget(QLabel("VISA resource"), 0, 0)
        communication_layout.addWidget(self.resource, 0, 1, 1, 3)
        communication_layout.addWidget(self.refresh_resources_button, 0, 4)
        communication_layout.addWidget(QLabel("I/O timeout"), 1, 0)
        communication_layout.addWidget(self.io_timeout, 1, 1)
        communication_layout.addWidget(self.test_connection_button, 1, 4)
        layout.addWidget(communication)

        measurement = QGroupBox("FVMI High-Resistance Measurement", content)
        measurement_layout = QGridLayout(measurement)
        self.source_range = QComboBox()
        self.source_range.addItem("100 V range", SOURCE_RANGE_100_V)
        self.source_range.addItem("1000 V range", SOURCE_RANGE_1000_V)
        self.source_voltage = _QuantityEdit("V")
        self.voltage_limit = _QuantityEdit("V")
        self.nplc = QDoubleSpinBox()
        self.nplc.setRange(0.01, 10.0)
        self.nplc.setDecimals(2)
        self.nplc.setSingleStep(0.1)
        self.settle_seconds = QDoubleSpinBox()
        self.settle_seconds.setRange(0.0, 3600.0)
        self.settle_seconds.setDecimals(3)
        self.settle_seconds.setSingleStep(0.5)
        self.settle_seconds.setSuffix(" s")
        self.output_off_between_measurements = QCheckBox(
            "Return to standby + zero check after each DAT row"
        )
        measurement_layout.addWidget(QLabel("V-source range"), 0, 0)
        measurement_layout.addWidget(self.source_range, 0, 1)
        measurement_layout.addWidget(QLabel("Source voltage"), 1, 0)
        measurement_layout.addWidget(self.source_voltage, 1, 1)
        measurement_layout.addWidget(QLabel("Hardware voltage limit"), 1, 2)
        measurement_layout.addWidget(self.voltage_limit, 1, 3)
        measurement_layout.addWidget(QLabel("Current integration (NPLC)"), 2, 0)
        measurement_layout.addWidget(self.nplc, 2, 1)
        measurement_layout.addWidget(QLabel("Source settle"), 2, 2)
        measurement_layout.addWidget(self.settle_seconds, 2, 3)
        measurement_layout.addWidget(
            self.output_off_between_measurements,
            3,
            0,
            1,
            4,
        )
        layout.addWidget(measurement)

        meter_connect = QGroupBox("Required Internal Connection", content)
        meter_layout = QVBoxLayout(meter_connect)
        meter_label = QLabel(
            "METER-CONNECT is fixed ON for this module. Apply sends "
            "SOUR:VOLT:MCON ON and reads it back; every Measure verifies it "
            "again immediately before operate. This connects V-source LO to "
            "Ammeter LO as required by the 6517B FVMI connection diagram. "
            "The 1 MOhm resistive limit is fixed OFF and also verified before "
            "operate so it cannot be included in the calculated DUT resistance."
        )
        meter_label.setWordWrap(True)
        meter_label.setStyleSheet("font-weight: 600;")
        meter_layout.addWidget(meter_label)
        layout.addWidget(meter_connect)

        high_voltage = QLabel(
            "HIGH VOLTAGE: 1000 V range requires correctly rated triax/cables, "
            "a closed safety enclosure and a verified physical interlock. The "
            "module cannot certify the fixture and never bypasses the instrument "
            "interlock. Default source voltage is 0 V. Standby and zero check are "
            "confirmed before a DAT row is emitted by default. The row-boundary "
            "option may retain operate with zero check off only while the SEQ is "
            "running; Stop, Error, completed and Disable always request standby "
            "and zero check ON.",
            content,
        )
        high_voltage.setWordWrap(True)
        high_voltage.setStyleSheet("color: #b3261e; font-weight: 700;")
        layout.addWidget(high_voltage)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self.load(default_settings())
        return page

    def _build_status_widget(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.refresh_status_button = QPushButton("Refresh Status")
        self.refresh_status_button.clicked.connect(
            self.api.refresh
        )
        self.safe_off_button = QPushButton("Standby + Zero Check")
        self.safe_off_button.clicked.connect(
            lambda: self.api.action("safe_off")
        )
        actions.addWidget(self.refresh_status_button)
        actions.addWidget(self.safe_off_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        group = QGroupBox("Instrument Status", page)
        form = QFormLayout(group)
        self.status_labels: dict[str, QLabel] = {}
        for key in (
            "Connection",
            "Resource",
            "Identity",
            "Applied Settings",
            "Sequence",
            "V-source Output",
            "Zero Check",
            "METER-CONNECT",
            "Resistive Limit",
            "Last Status",
            "Last Resistance (Ohm)",
            "Last Voltage (V)",
            "Last Current (A)",
        ):
            label = QLabel("-")
            label.setWordWrap(True)
            form.addRow(key, label)
            self.status_labels[key] = label
        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def dump(self) -> dict[str, Any]:
        return {
            "resource": self.resource.currentText().strip(),
            "io_timeout_seconds": self.io_timeout.value(),
            "source_range": self.source_range.currentData(),
            "source_voltage": self.source_voltage.text().strip(),
            "voltage_limit": self.voltage_limit.text().strip(),
            "nplc": self.nplc.value(),
            "settle_seconds": self.settle_seconds.value(),
            "output_off_between_measurements": (
                self.output_off_between_measurements.isChecked()
            ),
        }

    def load(self, settings: Mapping[str, Any]) -> None:
        merged = self._merged_settings(settings)
        blockers = [QSignalBlocker(widget) for widget in self._setting_widgets()]
        self._select_resource(str(merged["resource"]))
        self.io_timeout.setValue(float(merged["io_timeout_seconds"]))
        self._select_data(self.source_range, merged["source_range"])
        self.source_voltage.set_quantity(merged["source_voltage"])
        self.voltage_limit.set_quantity(merged["voltage_limit"])
        self.nplc.setValue(float(merged["nplc"]))
        self.settle_seconds.setValue(float(merged["settle_seconds"]))
        self.output_off_between_measurements.setChecked(
            bool(merged["output_off_between_measurements"])
        )
        del blockers

    def show_status(self, status: Mapping[str, Any]) -> None:
        resources = status.get("Available GPIB Resources")
        if isinstance(resources, (list, tuple)):
            self._update_resources(tuple(str(item) for item in resources))
        for key, value in status.items():
            label = self.status_labels.get(str(key))
            if label is None:
                continue
            if isinstance(value, float):
                label.setText(f"{value:.9g}")
            elif isinstance(value, bool):
                label.setText("Yes" if value else "No")
            else:
                label.setText(str(value))

    def _update_resources(self, resources: tuple[str, ...]) -> None:
        current = self.resource.currentText().strip()
        blocker = QSignalBlocker(self.resource)
        self.resource.clear()
        for resource in sorted(set(resources), key=str.casefold):
            self.resource.addItem(resource)
        if current and self.resource.findText(current) < 0:
            self.resource.addItem(current)
        self.resource.setCurrentText(current)
        del blocker

    def _select_resource(self, resource: str) -> None:
        value = resource.strip()
        if value and self.resource.findText(value) < 0:
            self.resource.addItem(value)
        self.resource.setCurrentText(value)

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _setting_widgets(self) -> list[QWidget]:
        return [
            self.resource,
            self.io_timeout,
            self.source_range,
            self.source_voltage,
            self.voltage_limit,
            self.nplc,
            self.settle_seconds,
            self.output_off_between_measurements,
        ]

    @staticmethod
    def _merged_settings(supplied: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(default_settings())
        if isinstance(supplied, Mapping):
            for key in result:
                if key in supplied:
                    result[key] = supplied[key]
        return result


Frontend = Keithley6517BFrontend

__all__ = ["Frontend", "Keithley6517BFrontend"]
