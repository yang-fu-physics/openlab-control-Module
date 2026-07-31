"""Keithley 2400 Measurement Module 的 PySide6 设置与状态页面。"""

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

from labcontrol.measurement.frontend_api import (
    ModuleFrontend,
    ModuleFrontendContext,
)

from .constants import (
    SENSE_2WIRE,
    SENSE_4WIRE,
    SOURCE_CURRENT,
    SOURCE_VOLTAGE,
    default_settings,
)
from .quantities import format_quantity


class _SettingsPage(QWidget):
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(780, 620)


class _QuantityEdit(QLineEdit):
    """允许实验人员保留 ``1m``、``500u`` 等紧凑输入。"""

    def __init__(self, unit: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.unit = unit
        self.setPlaceholderText(f"SI value ({unit}); e.g. 1m")
        self.editingFinished.connect(self._normalize)

    def set_quantity(self, value: object) -> None:
        try:
            text = format_quantity(value, expected_unit=self.unit)
        except (TypeError, ValueError):
            text = str(value)
        self.setText(text)
        self._mark(True)

    def _normalize(self) -> None:
        try:
            text = format_quantity(self.text(), expected_unit=self.unit)
        except ValueError:
            self._mark(False)
            return
        blocker = QSignalBlocker(self)
        self.setText(text)
        del blocker
        self._mark(True)

    def _mark(self, valid: bool) -> None:
        self.setStyleSheet("" if valid else "border: 1px solid #b71c1c;")


class Keithley2400Frontend(ModuleFrontend):
    """单仪表设置页、只读状态页和设置序列化适配器。"""

    def __init__(self, context: ModuleFrontendContext) -> None:
        super().__init__(context)
        self._sequence_running = False

    def create_settings_page(
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
            lambda: self.context.request_manual_action("refresh_resources")
        )
        self.test_connection_button = QPushButton("Test Connection")
        self.test_connection_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "test_connection", {"settings": self.settings()}
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
        communication_layout.addWidget(self.test_connection_button, 1, 4)
        communication_layout.addWidget(QLabel("I/O timeout"), 1, 0)
        communication_layout.addWidget(self.io_timeout, 1, 1)
        layout.addWidget(communication)

        source = QGroupBox("Source and Measurement", content)
        source_layout = QGridLayout(source)
        self.source_mode = QComboBox()
        self.source_mode.addItem("Constant current", SOURCE_CURRENT)
        self.source_mode.addItem("Constant voltage", SOURCE_VOLTAGE)
        self.source_current = _QuantityEdit("A")
        self.voltage_compliance = _QuantityEdit("V")
        self.source_voltage = _QuantityEdit("V")
        self.current_compliance = _QuantityEdit("A")
        self.sense_mode = QComboBox()
        self.sense_mode.addItem("2-wire (local sense)", SENSE_2WIRE)
        self.sense_mode.addItem("4-wire (remote sense)", SENSE_4WIRE)
        self.nplc = QDoubleSpinBox()
        self.nplc.setRange(0.01, 10.0)
        self.nplc.setDecimals(2)
        self.nplc.setSingleStep(0.1)
        self.settle_seconds = QDoubleSpinBox()
        self.settle_seconds.setRange(0.0, 3600.0)
        self.settle_seconds.setDecimals(3)
        self.settle_seconds.setSingleStep(0.1)
        self.settle_seconds.setSuffix(" s")
        self.output_off_between_measurements = QCheckBox(
            "Turn output off after each DAT row"
        )

        source_layout.addWidget(QLabel("Source mode"), 0, 0)
        source_layout.addWidget(self.source_mode, 0, 1, 1, 3)
        source_layout.addWidget(QLabel("Source current"), 1, 0)
        source_layout.addWidget(self.source_current, 1, 1)
        source_layout.addWidget(QLabel("Voltage compliance"), 1, 2)
        source_layout.addWidget(self.voltage_compliance, 1, 3)
        source_layout.addWidget(QLabel("Source voltage"), 2, 0)
        source_layout.addWidget(self.source_voltage, 2, 1)
        source_layout.addWidget(QLabel("Current compliance"), 2, 2)
        source_layout.addWidget(self.current_compliance, 2, 3)
        source_layout.addWidget(QLabel("Sense wiring"), 3, 0)
        source_layout.addWidget(self.sense_mode, 3, 1, 1, 3)
        source_layout.addWidget(QLabel("Integration (NPLC)"), 4, 0)
        source_layout.addWidget(self.nplc, 4, 1)
        source_layout.addWidget(QLabel("Source settle"), 4, 2)
        source_layout.addWidget(self.settle_seconds, 4, 3)
        source_layout.addWidget(
            self.output_off_between_measurements,
            5,
            0,
            1,
            4,
        )
        layout.addWidget(source)

        self.mode_note = QLabel(content)
        self.mode_note.setWordWrap(True)
        layout.addWidget(self.mode_note)

        safety = QLabel(
            "Enable only discovers resources. Apply Settings connects while output is "
            "off and reads every critical setting back. By default a Measure confirms "
            "OUTP? = 0 before emitting a DAT row; the row-boundary option may retain "
            "output only while the SEQ is running. Stop, Error, completed and Disable "
            "always request output OFF. "
            "4-wire mode requires both sense leads to remain connected.",
            content,
        )
        safety.setWordWrap(True)
        safety.setStyleSheet("color: #9a5200; font-weight: 600;")
        layout.addWidget(safety)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self.source_mode.currentIndexChanged.connect(self._update_source_mode)
        for widget in self._setting_widgets():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._changed)
                if widget.isEditable():
                    widget.currentTextChanged.connect(self._changed)
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._changed)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._changed)
        self.load_settings(default_settings())
        return page

    def create_status_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.refresh_status_button = QPushButton("Refresh Status")
        self.refresh_status_button.clicked.connect(
            self.context.request_status_refresh
        )
        self.safe_off_button = QPushButton("Safe Off")
        self.safe_off_button.clicked.connect(
            lambda: self.context.request_manual_action("safe_off")
        )
        actions.addWidget(self.refresh_status_button)
        actions.addWidget(self.safe_off_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        form_group = QGroupBox("Instrument Status", page)
        form = QFormLayout(form_group)
        self.status_labels: dict[str, QLabel] = {}
        for key in (
            "Connection",
            "Resource",
            "Identity",
            "Applied Settings",
            "Sequence",
            "Output",
            "Last Status",
            "Last Resistance (Ohm)",
            "Last Voltage (V)",
            "Last Current (A)",
        ):
            label = QLabel("-")
            label.setTextInteractionFlags(label.textInteractionFlags())
            label.setWordWrap(True)
            form.addRow(key, label)
            self.status_labels[key] = label
        layout.addWidget(form_group)
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        """返回可保存到 TOML/JSON 的 desired settings；不代表已 Apply。"""

        return {
            "resource": self.resource.currentText().strip(),
            "io_timeout_seconds": self.io_timeout.value(),
            "source_mode": self.source_mode.currentData(),
            "source_current": self.source_current.text().strip(),
            "voltage_compliance": self.voltage_compliance.text().strip(),
            "source_voltage": self.source_voltage.text().strip(),
            "current_compliance": self.current_compliance.text().strip(),
            "sense_mode": self.sense_mode.currentData(),
            "nplc": self.nplc.value(),
            "settle_seconds": self.settle_seconds.value(),
            "output_off_between_measurements": (
                self.output_off_between_measurements.isChecked()
            ),
        }

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        """加载保存值或 SEQ companion；不会连接、Apply 或打开输出。"""

        merged = self._merged_settings(settings)
        blockers = [QSignalBlocker(widget) for widget in self._setting_widgets()]
        self._select_resource(str(merged["resource"]))
        self.io_timeout.setValue(float(merged["io_timeout_seconds"]))
        self._select_data(self.source_mode, merged["source_mode"])
        self.source_current.set_quantity(merged["source_current"])
        self.voltage_compliance.set_quantity(merged["voltage_compliance"])
        self.source_voltage.set_quantity(merged["source_voltage"])
        self.current_compliance.set_quantity(merged["current_compliance"])
        self._select_data(self.sense_mode, merged["sense_mode"])
        self.nplc.setValue(float(merged["nplc"]))
        self.settle_seconds.setValue(float(merged["settle_seconds"]))
        self.output_off_between_measurements.setChecked(
            bool(merged["output_off_between_measurements"])
        )
        del blockers
        self._update_source_mode()

    def update_status(self, status: Mapping[str, Any]) -> None:
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

    def set_sequence_running(self, running: bool) -> None:
        self._sequence_running = running
        for button in (
            self.refresh_resources_button,
            self.test_connection_button,
            self.refresh_status_button,
            self.safe_off_button,
        ):
            button.setEnabled(not running)

    def _update_source_mode(self, *_args: Any) -> None:
        current = self.source_mode.currentData() == SOURCE_CURRENT
        self.source_current.setEnabled(current)
        self.voltage_compliance.setEnabled(current)
        self.source_voltage.setEnabled(not current)
        self.current_compliance.setEnabled(not current)
        self.mode_note.setText(
            (
                "Constant current: 2400 measures voltage; voltage compliance "
                "limits the source."
                if current
                else "Constant voltage: 2400 measures current; current compliance "
                "limits the source."
            )
        )

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
            self.source_mode,
            self.source_current,
            self.voltage_compliance,
            self.source_voltage,
            self.current_compliance,
            self.sense_mode,
            self.nplc,
            self.settle_seconds,
            self.output_off_between_measurements,
        ]

    def _changed(self, *_args: Any) -> None:
        self.settingsChanged.emit()

    @staticmethod
    def _merged_settings(supplied: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(default_settings())
        if isinstance(supplied, Mapping):
            for key in result:
                if key in supplied:
                    result[key] = supplied[key]
        return result


__all__ = ["Keithley2400Frontend"]
