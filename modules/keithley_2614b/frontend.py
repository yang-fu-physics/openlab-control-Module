"""Keithley 2614B 双通道模块的 PySide6 前端。"""

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
        return QSize(1120, 720)


class _QuantityEdit(QLineEdit):
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


class Keithley2614BFrontend(ModuleFrontend):
    """SMU A/B 独立设置、固定行模型和安全状态显示。"""

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
        self.settle_seconds = QDoubleSpinBox()
        self.settle_seconds.setRange(0.0, 3600.0)
        self.settle_seconds.setDecimals(3)
        self.settle_seconds.setSingleStep(0.1)
        self.settle_seconds.setSuffix(" s")
        self.output_off_between_measurements = QCheckBox(
            "Turn SMU A/B outputs off after each DAT row"
        )
        communication_layout.addWidget(QLabel("VISA resource"), 0, 0)
        communication_layout.addWidget(self.resource, 0, 1, 1, 3)
        communication_layout.addWidget(self.refresh_resources_button, 0, 4)
        communication_layout.addWidget(QLabel("I/O timeout"), 1, 0)
        communication_layout.addWidget(self.io_timeout, 1, 1)
        communication_layout.addWidget(QLabel("Shared source settle"), 1, 2)
        communication_layout.addWidget(self.settle_seconds, 1, 3)
        communication_layout.addWidget(self.test_connection_button, 1, 4)
        communication_layout.addWidget(
            self.output_off_between_measurements,
            2,
            0,
            1,
            5,
        )
        layout.addWidget(communication)

        channels_group = QGroupBox("SMU Channels", content)
        channels_layout = QGridLayout(channels_group)
        self.channel_widgets: dict[str, dict[str, QWidget]] = {}
        for index, (key, title) in enumerate(
            (("ch1", "Channel 1 / SMU A"), ("ch2", "Channel 2 / SMU B"))
        ):
            group = QGroupBox(title, channels_group)
            form = QFormLayout(group)
            enabled = QCheckBox("Enable channel")
            source_mode = QComboBox()
            source_mode.addItem("Constant current", SOURCE_CURRENT)
            source_mode.addItem("Constant voltage", SOURCE_VOLTAGE)
            source_current = _QuantityEdit("A")
            voltage_limit = _QuantityEdit("V")
            source_voltage = _QuantityEdit("V")
            current_limit = _QuantityEdit("A")
            sense_mode = QComboBox()
            sense_mode.addItem("2-wire (local sense)", SENSE_2WIRE)
            sense_mode.addItem("4-wire (remote sense)", SENSE_4WIRE)
            nplc = QDoubleSpinBox()
            nplc.setRange(0.001, 25.0)
            nplc.setDecimals(3)
            nplc.setSingleStep(0.1)
            mode_note = QLabel()
            mode_note.setWordWrap(True)
            form.addRow("", enabled)
            form.addRow("Source mode", source_mode)
            form.addRow("Source current", source_current)
            form.addRow("Voltage limit", voltage_limit)
            form.addRow("Source voltage", source_voltage)
            form.addRow("Current limit", current_limit)
            form.addRow("Sense wiring", sense_mode)
            form.addRow("Integration (NPLC)", nplc)
            form.addRow("", mode_note)
            widgets: dict[str, QWidget] = {
                "enabled": enabled,
                "source_mode": source_mode,
                "source_current": source_current,
                "voltage_limit": voltage_limit,
                "source_voltage": source_voltage,
                "current_limit": current_limit,
                "sense_mode": sense_mode,
                "nplc": nplc,
                "mode_note": mode_note,
            }
            self.channel_widgets[key] = widgets
            source_mode.currentIndexChanged.connect(
                lambda _value, channel=key: self._update_channel_mode(channel)
            )
            channels_layout.addWidget(group, 0, index)
        layout.addWidget(channels_group)

        high_voltage = QLabel(
            "HIGH VOLTAGE: the 200 V source range is enabled only by the physical "
            "2614B interlock. This module never drives or bypasses that interlock. "
            "Use rated connectors, shielding, protective earth and an interlocked "
            "fixture. Apply, Stop, Error, completed and Disable always request both "
            "outputs OFF. The row-boundary option controls only whether outputs remain "
            "active between successful measurements while a SEQ is running.",
            content,
        )
        high_voltage.setWordWrap(True)
        high_voltage.setStyleSheet("color: #b3261e; font-weight: 700;")
        layout.addWidget(high_voltage)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        for widget in self._setting_widgets():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._changed)
                if widget.isEditable():
                    widget.currentTextChanged.connect(self._changed)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._changed)
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._changed)
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
        self.safe_off_button = QPushButton("SMU A/B Off")
        self.safe_off_button.clicked.connect(
            lambda: self.context.request_manual_action("safe_off")
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
            "SMU A Output",
            "SMU B Output",
            "Last Channel",
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

    def settings(self) -> dict[str, Any]:
        channels: dict[str, dict[str, Any]] = {}
        for key, widgets in self.channel_widgets.items():
            enabled = widgets["enabled"]
            source_mode = widgets["source_mode"]
            source_current = widgets["source_current"]
            voltage_limit = widgets["voltage_limit"]
            source_voltage = widgets["source_voltage"]
            current_limit = widgets["current_limit"]
            sense_mode = widgets["sense_mode"]
            nplc = widgets["nplc"]
            assert isinstance(enabled, QCheckBox)
            assert isinstance(source_mode, QComboBox)
            assert isinstance(source_current, QLineEdit)
            assert isinstance(voltage_limit, QLineEdit)
            assert isinstance(source_voltage, QLineEdit)
            assert isinstance(current_limit, QLineEdit)
            assert isinstance(sense_mode, QComboBox)
            assert isinstance(nplc, QDoubleSpinBox)
            channels[key] = {
                "enabled": enabled.isChecked(),
                "source_mode": source_mode.currentData(),
                "source_current": source_current.text().strip(),
                "voltage_limit": voltage_limit.text().strip(),
                "source_voltage": source_voltage.text().strip(),
                "current_limit": current_limit.text().strip(),
                "sense_mode": sense_mode.currentData(),
                "nplc": nplc.value(),
            }
        return {
            "resource": self.resource.currentText().strip(),
            "io_timeout_seconds": self.io_timeout.value(),
            "settle_seconds": self.settle_seconds.value(),
            "output_off_between_measurements": (
                self.output_off_between_measurements.isChecked()
            ),
            "channels": channels,
        }

    def load_settings(self, settings: Mapping[str, Any]) -> None:
        merged = self._merged_settings(settings)
        blockers = [QSignalBlocker(widget) for widget in self._setting_widgets()]
        self._select_resource(str(merged["resource"]))
        self.io_timeout.setValue(float(merged["io_timeout_seconds"]))
        self.settle_seconds.setValue(float(merged["settle_seconds"]))
        self.output_off_between_measurements.setChecked(
            bool(merged["output_off_between_measurements"])
        )
        for key, widgets in self.channel_widgets.items():
            values = merged["channels"][key]
            enabled = widgets["enabled"]
            source_mode = widgets["source_mode"]
            source_current = widgets["source_current"]
            voltage_limit = widgets["voltage_limit"]
            source_voltage = widgets["source_voltage"]
            current_limit = widgets["current_limit"]
            sense_mode = widgets["sense_mode"]
            nplc = widgets["nplc"]
            assert isinstance(enabled, QCheckBox)
            assert isinstance(source_mode, QComboBox)
            assert isinstance(source_current, _QuantityEdit)
            assert isinstance(voltage_limit, _QuantityEdit)
            assert isinstance(source_voltage, _QuantityEdit)
            assert isinstance(current_limit, _QuantityEdit)
            assert isinstance(sense_mode, QComboBox)
            assert isinstance(nplc, QDoubleSpinBox)
            enabled.setChecked(bool(values["enabled"]))
            self._select_data(source_mode, values["source_mode"])
            source_current.set_quantity(values["source_current"])
            voltage_limit.set_quantity(values["voltage_limit"])
            source_voltage.set_quantity(values["source_voltage"])
            current_limit.set_quantity(values["current_limit"])
            self._select_data(sense_mode, values["sense_mode"])
            nplc.setValue(float(values["nplc"]))
        del blockers
        for key in self.channel_widgets:
            self._update_channel_mode(key)

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

    def _update_channel_mode(self, key: str) -> None:
        widgets = self.channel_widgets[key]
        mode = widgets["source_mode"]
        assert isinstance(mode, QComboBox)
        current = mode.currentData() == SOURCE_CURRENT
        widgets["source_current"].setEnabled(current)
        widgets["voltage_limit"].setEnabled(current)
        widgets["source_voltage"].setEnabled(not current)
        widgets["current_limit"].setEnabled(not current)
        note = widgets["mode_note"]
        assert isinstance(note, QLabel)
        note.setText(
            "Voltage limit applies to current source."
            if current
            else "Current limit applies to voltage source."
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
        result: list[QWidget] = [
            self.resource,
            self.io_timeout,
            self.settle_seconds,
            self.output_off_between_measurements,
        ]
        for widgets in self.channel_widgets.values():
            result.extend(
                widget
                for key, widget in widgets.items()
                if key != "mode_note"
            )
        return result

    def _changed(self, *_args: Any) -> None:
        self.settingsChanged.emit()

    @staticmethod
    def _merged_settings(supplied: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(default_settings())
        if not isinstance(supplied, Mapping):
            return result
        for key in (
            "resource",
            "io_timeout_seconds",
            "settle_seconds",
            "output_off_between_measurements",
        ):
            if key in supplied:
                result[key] = supplied[key]
        channels = supplied.get("channels")
        if isinstance(channels, Mapping):
            for key in result["channels"]:
                value = channels.get(key)
                if isinstance(value, Mapping):
                    result["channels"][key].update(value)
        return result


__all__ = ["Keithley2614BFrontend"]
