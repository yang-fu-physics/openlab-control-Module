from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import (
    ModuleFrontend,
)

from .constants import (
    CURRENT_EXCITATIONS,
    FREQUENCIES_HZ,
    RESISTANCE_RANGES,
    VOLTAGE_EXCITATIONS,
    default_settings,
)


class LakeShore372AFrontend(ModuleFrontend):
    def create_settings_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = QWidget(parent)
        outer = QVBoxLayout(page)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)

        communication = QGroupBox(
            "GPIB Communication",
            content,
        )
        communication_layout = QGridLayout(
            communication
        )
        self.resource = QComboBox()
        self.resource.setEditable(True)
        self.resource.setMinimumContentsLength(24)
        self.refresh_resources_button = QPushButton(
            "Refresh GPIB"
        )
        self.refresh_resources_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "refresh_resources"
            )
        )
        self.io_timeout = QDoubleSpinBox()
        self.io_timeout.setRange(0.1, 30.0)
        self.io_timeout.setDecimals(1)
        self.io_timeout.setSingleStep(0.5)
        self.io_timeout.setSuffix(" s")
        self.retry_attempts = QSpinBox()
        self.retry_attempts.setRange(1, 5)
        communication_layout.addWidget(
            QLabel("VISA resource"),
            0,
            0,
        )
        communication_layout.addWidget(
            self.resource,
            0,
            1,
            1,
            3,
        )
        communication_layout.addWidget(
            self.refresh_resources_button,
            0,
            4,
        )
        communication_layout.addWidget(
            QLabel("I/O timeout"),
            1,
            0,
        )
        communication_layout.addWidget(
            self.io_timeout,
            1,
            1,
        )
        communication_layout.addWidget(
            QLabel("Attempts"),
            1,
            2,
        )
        communication_layout.addWidget(
            self.retry_attempts,
            1,
            3,
        )
        layout.addWidget(communication)

        scan = QGroupBox(
            "Input Channel Parameters",
            content,
        )
        scan_layout = QGridLayout(scan)
        self.frequency = QComboBox()
        for index, frequency in FREQUENCIES_HZ:
            self.frequency.addItem(
                f"{frequency:g} Hz",
                index,
            )
        self.pause_seconds = self._seconds_spin(
            3,
            200,
        )
        self.dwell_seconds = self._seconds_spin(
            1,
            200,
        )
        self.filter_enabled = QCheckBox("Enabled")
        self.filter_settle_seconds = (
            self._seconds_spin(1, 200)
        )
        self.filter_window_percent = QSpinBox()
        self.filter_window_percent.setRange(1, 80)
        self.filter_window_percent.setSuffix(" % FS")
        self.shunt_after_read = QCheckBox(
            "Shunt excitation after each channel "
            "(recommended)"
        )
        controls = (
            ("Excitation frequency", self.frequency),
            ("Change pause time", self.pause_seconds),
            ("Scan dwell time", self.dwell_seconds),
            ("Enable filter", self.filter_enabled),
            (
                "Filter settle time",
                self.filter_settle_seconds,
            ),
            (
                "Filter window",
                self.filter_window_percent,
            ),
        )
        for index, (label, widget) in enumerate(
            controls
        ):
            row = index // 3
            column = (index % 3) * 2
            scan_layout.addWidget(
                QLabel(label),
                row,
                column,
            )
            scan_layout.addWidget(
                widget,
                row,
                column + 1,
            )
        scan_layout.addWidget(
            self.shunt_after_read,
            2,
            0,
            1,
            6,
        )
        layout.addWidget(scan)

        channel_grid = QGridLayout()
        self.channel_widgets: dict[
            str,
            dict[str, QWidget],
        ] = {}
        for slot in range(1, 5):
            key = f"r{slot}"
            group = QGroupBox(f"R{slot}")
            form = QFormLayout(group)
            input_channel = QSpinBox()
            input_channel.setRange(1, 16)
            enabled = QCheckBox("Enable channel")
            mode = QComboBox()
            mode.addItem("Current", "current")
            mode.addItem("Voltage", "voltage")
            excitation = QComboBox()
            autorange = QComboBox()
            autorange.addItem("Off", 0)
            autorange.addItem(
                "Autorange current",
                1,
            )
            resistance_range = QComboBox()
            for index, label, _value in (
                RESISTANCE_RANGES
            ):
                resistance_range.addItem(
                    label,
                    index,
                )
            form.addRow(
                "Physical input",
                input_channel,
            )
            form.addRow("", enabled)
            form.addRow("Excitation mode", mode)
            form.addRow(
                "Excitation range",
                excitation,
            )
            form.addRow("Autorange", autorange)
            form.addRow(
                "Resistance range",
                resistance_range,
            )
            self.channel_widgets[key] = {
                "input_channel": input_channel,
                "enabled": enabled,
                "excitation_mode": mode,
                "excitation_range": excitation,
                "autorange": autorange,
                "resistance_range": resistance_range,
            }
            self._populate_excitation(key, 5)
            channel_grid.addWidget(
                group,
                (slot - 1) // 2,
                (slot - 1) % 2,
            )
        layout.addLayout(channel_grid)

        note = QLabel(
            "Enable only initializes this module and discovers "
            "GPIB resources. Apply Settings verifies *IDN? and "
            "configures the selected inputs while keeping "
            "excitation shunted. Measure temporarily enables "
            "excitation channel by channel."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        for widget in self._setting_widgets():
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(
                    self._changed
                )
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._changed)
            elif isinstance(
                widget,
                (QSpinBox, QDoubleSpinBox),
            ):
                widget.valueChanged.connect(
                    self._changed
                )
        for key, widgets in self.channel_widgets.items():
            mode = widgets["excitation_mode"]
            assert isinstance(mode, QComboBox)
            try:
                mode.currentTextChanged.disconnect(
                    self._changed
                )
            except (RuntimeError, TypeError):
                pass
            mode.currentIndexChanged.connect(
                lambda _index, channel_key=key:
                self._mode_changed(channel_key)
            )
        return page

    def create_status_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        group = QGroupBox("Instrument Status")
        form = QFormLayout(group)
        self.status_labels: dict[str, QLabel] = {}
        for name in (
            "Connection",
            "Resource",
            "Identity",
            "Applied Settings",
            "Sequence",
            "Excitation",
            "Resource Discovery",
            "Estimated Measure Time (s)",
            "Last Channel",
            "Last Resistance (Ohm)",
            "Last Phase (deg)",
            "Last Current (A)",
            "Last Status",
            "Last Action",
        ):
            label = QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                label.textInteractionFlags()
            )
            self.status_labels[name] = label
            form.addRow(name, label)
        layout.addWidget(group)
        buttons = QHBoxLayout()
        self.test_connection_button = QPushButton(
            "Test Connection"
        )
        self.status_refresh_resources_button = (
            QPushButton("Refresh GPIB")
        )
        self.refresh_status_button = QPushButton(
            "Refresh Status"
        )
        self.test_connection_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "test_connection",
                {"settings": self.settings()},
            )
        )
        self.status_refresh_resources_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "refresh_resources"
            )
        )
        self.refresh_status_button.clicked.connect(
            self.context.request_status_refresh
        )
        buttons.addWidget(
            self.test_connection_button
        )
        buttons.addWidget(
            self.status_refresh_resources_button
        )
        buttons.addWidget(self.refresh_status_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        channels: dict[str, dict[str, Any]] = {}
        for key, widgets in self.channel_widgets.items():
            input_channel = widgets["input_channel"]
            enabled = widgets["enabled"]
            mode = widgets["excitation_mode"]
            excitation = widgets["excitation_range"]
            autorange = widgets["autorange"]
            resistance = widgets["resistance_range"]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(mode, QComboBox)
            assert isinstance(excitation, QComboBox)
            assert isinstance(autorange, QComboBox)
            assert isinstance(resistance, QComboBox)
            channels[key] = {
                "enabled": enabled.isChecked(),
                "input_channel": input_channel.value(),
                "excitation_mode": str(
                    mode.currentData()
                ),
                "excitation_range": int(
                    excitation.currentData()
                ),
                "autorange": int(
                    autorange.currentData()
                ),
                "resistance_range": int(
                    resistance.currentData()
                ),
            }
        return {
            "resource": self.resource.currentText().strip(),
            "frequency_index": int(
                self.frequency.currentData()
            ),
            "pause_seconds": self.pause_seconds.value(),
            "dwell_seconds": self.dwell_seconds.value(),
            "filter_enabled": (
                self.filter_enabled.isChecked()
            ),
            "filter_settle_seconds": (
                self.filter_settle_seconds.value()
            ),
            "filter_window_percent": (
                self.filter_window_percent.value()
            ),
            "io_timeout_seconds": (
                self.io_timeout.value()
            ),
            "retry_attempts": (
                self.retry_attempts.value()
            ),
            "shunt_after_read": (
                self.shunt_after_read.isChecked()
            ),
            "channels": channels,
        }

    def load_settings(
        self,
        settings: Mapping[str, Any],
    ) -> None:
        defaults = default_settings()
        values = {
            **defaults,
            **dict(settings),
        }
        raw_channels = settings.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raw_channels = {}
        widgets = self._setting_widgets()
        blockers = [
            QSignalBlocker(widget)
            for widget in widgets
        ]
        self._select_resource(str(values["resource"]))
        self._select_data(
            self.frequency,
            int(values["frequency_index"]),
        )
        self.pause_seconds.setValue(
            int(values["pause_seconds"])
        )
        self.dwell_seconds.setValue(
            int(values["dwell_seconds"])
        )
        self.filter_enabled.setChecked(
            bool(values["filter_enabled"])
        )
        self.filter_settle_seconds.setValue(
            int(values["filter_settle_seconds"])
        )
        self.filter_window_percent.setValue(
            int(values["filter_window_percent"])
        )
        self.io_timeout.setValue(
            float(values["io_timeout_seconds"])
        )
        self.retry_attempts.setValue(
            int(values["retry_attempts"])
        )
        self.shunt_after_read.setChecked(
            bool(values["shunt_after_read"])
        )
        for slot in range(1, 5):
            key = f"r{slot}"
            channel = {
                **defaults["channels"][key],
                **dict(
                    raw_channels.get(key, {})
                    if isinstance(
                        raw_channels.get(key, {}),
                        Mapping,
                    )
                    else {}
                ),
            }
            channel_widgets = self.channel_widgets[key]
            input_channel = channel_widgets[
                "input_channel"
            ]
            enabled = channel_widgets["enabled"]
            mode = channel_widgets[
                "excitation_mode"
            ]
            excitation = channel_widgets[
                "excitation_range"
            ]
            autorange = channel_widgets["autorange"]
            resistance = channel_widgets[
                "resistance_range"
            ]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(mode, QComboBox)
            assert isinstance(excitation, QComboBox)
            assert isinstance(autorange, QComboBox)
            assert isinstance(resistance, QComboBox)
            input_channel.setValue(
                int(channel["input_channel"])
            )
            enabled.setChecked(
                bool(channel["enabled"])
            )
            self._select_data(
                mode,
                str(channel["excitation_mode"]),
            )
            self._populate_excitation(
                key,
                int(channel["excitation_range"]),
            )
            self._select_data(
                autorange,
                int(channel["autorange"]),
            )
            self._select_data(
                resistance,
                int(channel["resistance_range"]),
            )
        del blockers

    def update_status(
        self,
        status: Mapping[str, Any],
    ) -> None:
        resources = status.get(
            "Available GPIB Resources"
        )
        if isinstance(resources, (list, tuple)):
            self._update_resources(
                tuple(str(item) for item in resources)
            )
        for key, value in status.items():
            label = self.status_labels.get(str(key))
            if label is None:
                continue
            if isinstance(value, float):
                label.setText(f"{value:.9g}")
            elif isinstance(value, (list, tuple)):
                label.setText(
                    ", ".join(str(item) for item in value)
                    or "-"
                )
            else:
                label.setText(str(value))

    def set_sequence_running(
        self,
        running: bool,
    ) -> None:
        for button in (
            self.refresh_resources_button,
            self.test_connection_button,
            self.status_refresh_resources_button,
            self.refresh_status_button,
        ):
            button.setEnabled(not running)

    def _mode_changed(self, key: str) -> None:
        widgets = self.channel_widgets[key]
        excitation = widgets["excitation_range"]
        assert isinstance(excitation, QComboBox)
        selected = (
            int(excitation.currentData())
            if excitation.currentData() is not None
            else 5
        )
        self._populate_excitation(key, selected)
        self._changed()

    def _populate_excitation(
        self,
        key: str,
        selected: int,
    ) -> None:
        widgets = self.channel_widgets[key]
        mode = widgets["excitation_mode"]
        excitation = widgets["excitation_range"]
        assert isinstance(mode, QComboBox)
        assert isinstance(excitation, QComboBox)
        values = (
            CURRENT_EXCITATIONS
            if mode.currentData() == "current"
            else VOLTAGE_EXCITATIONS
        )
        blocker = QSignalBlocker(excitation)
        excitation.clear()
        for index, label, _value in values:
            excitation.addItem(label, index)
        maximum = values[-1][0]
        self._select_data(
            excitation,
            min(max(1, selected), maximum),
        )
        del blocker

    def _update_resources(
        self,
        resources: tuple[str, ...],
    ) -> None:
        current = self.resource.currentText().strip()
        blocker = QSignalBlocker(self.resource)
        self.resource.clear()
        for resource in sorted(
            set(resources),
            key=str.casefold,
        ):
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
    def _select_data(
        combo: QComboBox,
        value: Any,
    ) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _setting_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.resource,
            self.frequency,
            self.pause_seconds,
            self.dwell_seconds,
            self.filter_enabled,
            self.filter_settle_seconds,
            self.filter_window_percent,
            self.io_timeout,
            self.retry_attempts,
            self.shunt_after_read,
        ]
        for channel in self.channel_widgets.values():
            widgets.extend(channel.values())
        return widgets

    def _changed(self, *_args: Any) -> None:
        self.settingsChanged.emit()

    @staticmethod
    def _seconds_spin(
        minimum: int,
        maximum: int,
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSuffix(" s")
        return spin


__all__ = ["LakeShore372AFrontend"]
