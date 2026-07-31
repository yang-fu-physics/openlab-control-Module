"""Keithley 6221 + 2182A Delta 模块的 PySide6 设置和状态界面。

前端只编辑 desired settings；Load SEQ/Load Settings 不会连接仪表或自动 Apply。
电流、compliance 和 delay 使用文本量输入，支持 ``1m``、``1u``、``1n``、``1p``，
并在失去焦点后规范化为紧凑工程计数。后端仍会重新解析并执行全部安全检查。
"""

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
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import (
    ModuleFrontend,
)

from .constants import (
    FILTER_TYPES,
    MAX_DELTA_COUNT,
    MODE_INDEPENDENT,
    MODE_SHARED,
    VOLTAGE_RANGES,
    default_delta_settings,
    default_settings,
)
from .quantities import format_quantity


class _SettingsPage(QWidget):
    """提供普通 DPI 首选尺寸；较小屏幕和 4K 缩放使用内部滚动区。"""

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1280, 790)


class _QuantityEdit(QLineEdit):
    """显示短 SI 文本的输入框，非法文本保留给 Apply 显示完整错误。"""

    def __init__(
        self,
        expected_unit: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.expected_unit = expected_unit
        self.setMinimumWidth(110)
        self.setPlaceholderText(
            {
                "A": "e.g. 1m, 100u, 1p",
                "V": "e.g. 1, 100m",
                "s": "e.g. 2m, 1",
            }.get(expected_unit, "SI value")
        )
        self.editingFinished.connect(self._normalize_text)

    def set_quantity(self, value: object) -> None:
        try:
            text = format_quantity(
                value,
                expected_unit=self.expected_unit,
            )
        except (TypeError, ValueError):
            text = str(value)
        self.setText(text)
        self._mark_valid(True)

    def _normalize_text(self) -> None:
        try:
            normalized = format_quantity(
                self.text(),
                expected_unit=self.expected_unit,
            )
        except ValueError:
            self._mark_valid(False)
            return
        self.setText(normalized)
        self._mark_valid(True)

    def _mark_valid(self, valid: bool) -> None:
        self.setStyleSheet(
            ""
            if valid
            else "QLineEdit { border: 1px solid #b00020; }"
        )


class Keithley6221DeltaFrontend(ModuleFrontend):
    """Delta Settings/Status 页面与设置序列化适配器。"""

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

        communication = QGroupBox(
            "GPIB Communication",
            content,
        )
        communication_layout = QGridLayout(
            communication
        )
        self.resource_6221 = self._resource_combo()
        self.resource_7001 = self._resource_combo()
        self.refresh_resources_button = QPushButton(
            "Refresh GPIB"
        )
        self.refresh_resources_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "refresh_resources"
            )
        )
        self.test_connection_button = QPushButton(
            "Test Connections"
        )
        self.test_connection_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "test_connection",
                {"settings": self.settings()},
            )
        )
        self.io_timeout = QDoubleSpinBox()
        self.io_timeout.setRange(0.1, 30.0)
        self.io_timeout.setDecimals(1)
        self.io_timeout.setSingleStep(0.5)
        self.io_timeout.setSuffix(" s")
        communication_layout.addWidget(
            QLabel("Keithley 6221 VISA resource"),
            0,
            0,
        )
        communication_layout.addWidget(
            self.resource_6221,
            0,
            1,
            1,
            3,
        )
        communication_layout.addWidget(
            QLabel("Keithley 7001 VISA resource"),
            1,
            0,
        )
        communication_layout.addWidget(
            self.resource_7001,
            1,
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
            self.test_connection_button,
            1,
            4,
        )
        communication_layout.addWidget(
            QLabel("I/O timeout"),
            2,
            0,
        )
        communication_layout.addWidget(
            self.io_timeout,
            2,
            1,
        )
        communication_note = QLabel(
            "2182A is controlled through the 6221 RS-232 "
            "link and Trigger Link; it has no separate VISA "
            "resource here."
        )
        communication_note.setWordWrap(True)
        communication_layout.addWidget(
            communication_note,
            2,
            2,
            1,
            3,
        )
        layout.addWidget(communication)

        operation = QGroupBox(
            "Run Mode and Routing",
            content,
        )
        operation_layout = QGridLayout(operation)
        self.mode = QComboBox()
        self.mode.addItem(
            "Shared configuration / stay Armed",
            MODE_SHARED,
        )
        self.mode.addItem(
            "Independent configuration / re-arm each channel",
            MODE_INDEPENDENT,
        )
        self.switch_settle = QDoubleSpinBox()
        self.switch_settle.setRange(0.0, 300.0)
        self.switch_settle.setDecimals(3)
        self.switch_settle.setSingleStep(0.1)
        self.switch_settle.setSuffix(" s")
        operation_layout.addWidget(
            QLabel("Operating mode"),
            0,
            0,
        )
        operation_layout.addWidget(
            self.mode,
            0,
            1,
            1,
            3,
        )
        operation_layout.addWidget(
            QLabel("7001 settle time"),
            1,
            0,
        )
        operation_layout.addWidget(
            self.switch_settle,
            1,
            1,
        )
        operation_note = QLabel(
            "Shared mode sends ARM before the first SEQ "
            "instruction, waits 3 s, and remains Armed. "
            "Independent mode aborts before every route "
            "change and waits 3 s after every new ARM."
        )
        operation_note.setWordWrap(True)
        operation_layout.addWidget(
            operation_note,
            2,
            0,
            1,
            4,
        )
        layout.addWidget(operation)

        channels = QGroupBox(
            "Logical Channels",
            content,
        )
        channels_layout = QHBoxLayout(channels)
        self.channel_enabled: dict[str, QCheckBox] = {}
        for index in range(1, 5):
            key = f"ch{index}"
            checkbox = QCheckBox(f"Enable CH{index}")
            checkbox.setAccessibleName(
                f"Enable Keithley Delta CH{index}"
            )
            self.channel_enabled[key] = checkbox
            channels_layout.addWidget(checkbox)
        channels_layout.addStretch(1)
        self.switcher_note = QLabel(
            "7001 state has not been checked yet."
        )
        self.switcher_note.setWordWrap(True)
        channels_layout.addWidget(
            self.switcher_note,
            2,
        )
        layout.addWidget(channels)

        self.configuration_stack = QStackedWidget(content)
        shared_page = QWidget(self.configuration_stack)
        shared_layout = QVBoxLayout(shared_page)
        shared_group, self.shared_widgets = (
            self._create_delta_group(
                "Shared Delta Configuration",
                shared_page,
            )
        )
        shared_layout.addWidget(shared_group)
        shared_layout.addStretch(1)
        self.configuration_stack.addWidget(shared_page)

        independent_page = QWidget(
            self.configuration_stack
        )
        independent_layout = QVBoxLayout(
            independent_page
        )
        self.independent_tabs = QTabWidget(
            independent_page
        )
        self.independent_widgets: dict[
            str,
            dict[str, QWidget],
        ] = {}
        for index in range(1, 5):
            key = f"ch{index}"
            tab = QWidget(self.independent_tabs)
            tab_layout = QVBoxLayout(tab)
            group, widgets = self._create_delta_group(
                f"CH{index} Delta Configuration",
                tab,
            )
            self.independent_widgets[key] = widgets
            tab_layout.addWidget(group)
            tab_layout.addStretch(1)
            self.independent_tabs.addTab(
                tab,
                f"CH{index}",
            )
        independent_layout.addWidget(
            self.independent_tabs
        )
        self.configuration_stack.addWidget(
            independent_page
        )
        layout.addWidget(self.configuration_stack)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._sequence_running = False
        self._switcher_available = True
        self._connect_change_signals()
        self.mode.currentIndexChanged.connect(
            self._update_mode_page
        )
        self.load_settings(default_settings())
        return page

    def _create_delta_group(
        self,
        title: str,
        parent: QWidget,
    ) -> tuple[QGroupBox, dict[str, QWidget]]:
        group = QGroupBox(title, parent)
        layout = QGridLayout(group)
        left = QFormLayout()
        right = QFormLayout()

        high = _QuantityEdit("A")
        low = _QuantityEdit("A")
        compliance = _QuantityEdit("V")
        delay = _QuantityEdit("s")
        count = QSpinBox()
        count.setRange(1, MAX_DELTA_COUNT)
        voltage_range = QComboBox()
        for key, label, _value in VOLTAGE_RANGES:
            voltage_range.addItem(label, key)
        nplc = QSpinBox()
        nplc.setRange(1, 50)
        nplc.setSuffix(" PLC")
        analog_filter = QCheckBox("Enable")
        digital_filter = QCheckBox("Enable")
        filter_type = QComboBox()
        for key, label in FILTER_TYPES:
            filter_type.addItem(label, key)
        filter_count = QSpinBox()
        filter_count.setRange(1, 100)
        filter_window = QDoubleSpinBox()
        filter_window.setRange(0.0, 10.0)
        filter_window.setDecimals(3)
        filter_window.setSingleStep(0.01)
        filter_window.setSuffix(" %")
        left.addRow("High current (A)", high)
        left.addRow("Low current (A)", low)
        left.addRow("Voltage compliance (V)", compliance)
        left.addRow("Delta delay (s)", delay)
        left.addRow("Delta count", count)
        right.addRow("2182A voltage range", voltage_range)
        right.addRow("2182A integration", nplc)
        right.addRow("2182A analog filter", analog_filter)
        right.addRow("2182A digital filter", digital_filter)
        right.addRow("Digital filter type", filter_type)
        right.addRow("Digital filter count", filter_count)
        right.addRow(
            "Digital filter window",
            filter_window,
        )
        layout.addLayout(left, 0, 0)
        layout.addLayout(right, 0, 1)
        note = QLabel(
            "Current and delay accept SI prefixes: 1m, "
            "100u, 1n, 1p. A zero current span is safe to "
            "load but Apply will reject it before a SEQ. "
            "There is no module software current/compliance "
            "cap; 6221 Compliance Abort and Cold Switching "
            "remain enabled."
        )
        note.setWordWrap(True)
        layout.addWidget(note, 1, 0, 1, 2)
        widgets: dict[str, QWidget] = {
            "high_current": high,
            "low_current": low,
            "compliance": compliance,
            "delta_delay": delay,
            "count": count,
            "voltage_range": voltage_range,
            "nplc": nplc,
            "analog_filter_enabled": analog_filter,
            "digital_filter_enabled": digital_filter,
            "digital_filter_type": filter_type,
            "digital_filter_count": filter_count,
            "digital_filter_window_percent": filter_window,
        }
        return group, widgets

    def create_status_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        summary = QGroupBox("Instrument Status", page)
        form = QFormLayout(summary)
        self.status_labels: dict[str, QLabel] = {}
        for key in (
            "State",
            "6221",
            "2182A",
            "7001",
            "Armed",
            "Sequence Active",
            "Active Channel",
            "Last Resistance (Ohm)",
            "Last Current (A)",
            "Last StdDev (Ohm)",
            "ARM Wait",
            "Routing Config",
        ):
            label = QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                label.textInteractionFlags()
            )
            self.status_labels[key] = label
            form.addRow(key, label)
        layout.addWidget(summary)
        buttons = QHBoxLayout()
        self.status_refresh_resources_button = QPushButton(
            "Refresh GPIB"
        )
        self.status_refresh_resources_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "refresh_resources"
            )
        )
        self.refresh_status_button = QPushButton(
            "Refresh Status"
        )
        self.refresh_status_button.clicked.connect(
            self.context.request_status_refresh
        )
        self.safe_off_button = QPushButton(
            "Safe Output Off"
        )
        self.safe_off_button.clicked.connect(
            lambda: self.context.request_manual_action(
                "safe_off"
            )
        )
        buttons.addWidget(
            self.status_refresh_resources_button
        )
        buttons.addWidget(self.refresh_status_button)
        buttons.addWidget(self.safe_off_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def settings(self) -> dict[str, Any]:
        """读取界面文本；SI 数值由 worker 后端统一解析和验证。"""

        return {
            "resource_6221": (
                self.resource_6221.currentText().strip()
            ),
            "resource_7001": (
                self.resource_7001.currentText().strip()
            ),
            "mode": str(self.mode.currentData()),
            "io_timeout_seconds": self.io_timeout.value(),
            "switch_settle_seconds": (
                self.switch_settle.value()
            ),
            "channels": {
                key: {
                    "enabled": checkbox.isChecked(),
                }
                for key, checkbox
                in self.channel_enabled.items()
            },
            "shared": self._delta_settings(
                self.shared_widgets
            ),
            "independent": {
                key: self._delta_settings(widgets)
                for key, widgets
                in self.independent_widgets.items()
            },
        }

    @staticmethod
    def _delta_settings(
        widgets: Mapping[str, QWidget],
    ) -> dict[str, Any]:
        high = widgets["high_current"]
        low = widgets["low_current"]
        compliance = widgets["compliance"]
        delay = widgets["delta_delay"]
        count = widgets["count"]
        voltage_range = widgets["voltage_range"]
        nplc = widgets["nplc"]
        analog = widgets["analog_filter_enabled"]
        digital = widgets["digital_filter_enabled"]
        filter_type = widgets["digital_filter_type"]
        filter_count = widgets["digital_filter_count"]
        filter_window = widgets[
            "digital_filter_window_percent"
        ]
        assert isinstance(high, QLineEdit)
        assert isinstance(low, QLineEdit)
        assert isinstance(compliance, QLineEdit)
        assert isinstance(delay, QLineEdit)
        assert isinstance(count, QSpinBox)
        assert isinstance(voltage_range, QComboBox)
        assert isinstance(nplc, QSpinBox)
        assert isinstance(analog, QCheckBox)
        assert isinstance(digital, QCheckBox)
        assert isinstance(filter_type, QComboBox)
        assert isinstance(filter_count, QSpinBox)
        assert isinstance(filter_window, QDoubleSpinBox)
        return {
            "high_current": high.text().strip(),
            "low_current": low.text().strip(),
            "compliance": compliance.text().strip(),
            "delta_delay": delay.text().strip(),
            "count": count.value(),
            "voltage_range": str(
                voltage_range.currentData()
            ),
            "nplc": nplc.value(),
            "analog_filter_enabled": (
                analog.isChecked()
            ),
            "digital_filter_enabled": (
                digital.isChecked()
            ),
            "digital_filter_type": str(
                filter_type.currentData()
            ),
            "digital_filter_count": (
                filter_count.value()
            ),
            "digital_filter_window_percent": (
                filter_window.value()
            ),
        }

    def load_settings(
        self,
        settings: Mapping[str, Any],
    ) -> None:
        """加载保存/SEQ 配套设置但不触发 Apply。"""

        merged = self._merged_settings(settings)
        blockers = [
            QSignalBlocker(widget)
            for widget in self._setting_widgets()
        ]
        self._select_resource(
            self.resource_6221,
            str(merged["resource_6221"]),
        )
        self._select_resource(
            self.resource_7001,
            str(merged["resource_7001"]),
        )
        self._select_data(
            self.mode,
            merged["mode"],
        )
        self.io_timeout.setValue(
            float(merged["io_timeout_seconds"])
        )
        self.switch_settle.setValue(
            float(merged["switch_settle_seconds"])
        )
        for key, checkbox in self.channel_enabled.items():
            checkbox.setChecked(
                bool(
                    merged["channels"][key][
                        "enabled"
                    ]
                )
            )
        self._load_delta(
            self.shared_widgets,
            merged["shared"],
        )
        for key, widgets in (
            self.independent_widgets.items()
        ):
            self._load_delta(
                widgets,
                merged["independent"][key],
            )
        del blockers
        self._update_mode_page()
        self._update_control_availability()

    @staticmethod
    def _load_delta(
        widgets: Mapping[str, QWidget],
        values: Mapping[str, Any],
    ) -> None:
        for key, unit in (
            ("high_current", "A"),
            ("low_current", "A"),
            ("compliance", "V"),
            ("delta_delay", "s"),
        ):
            widget = widgets[key]
            assert isinstance(widget, _QuantityEdit)
            widget.set_quantity(values[key])
        count = widgets["count"]
        voltage_range = widgets["voltage_range"]
        nplc = widgets["nplc"]
        analog = widgets["analog_filter_enabled"]
        digital = widgets["digital_filter_enabled"]
        filter_type = widgets["digital_filter_type"]
        filter_count = widgets["digital_filter_count"]
        filter_window = widgets[
            "digital_filter_window_percent"
        ]
        assert isinstance(count, QSpinBox)
        assert isinstance(voltage_range, QComboBox)
        assert isinstance(nplc, QSpinBox)
        assert isinstance(analog, QCheckBox)
        assert isinstance(digital, QCheckBox)
        assert isinstance(filter_type, QComboBox)
        assert isinstance(filter_count, QSpinBox)
        assert isinstance(filter_window, QDoubleSpinBox)
        count.setValue(int(values["count"]))
        Keithley6221DeltaFrontend._select_data(
            voltage_range,
            values["voltage_range"],
        )
        nplc.setValue(int(values["nplc"]))
        analog.setChecked(
            bool(values["analog_filter_enabled"])
        )
        digital.setChecked(
            bool(values["digital_filter_enabled"])
        )
        Keithley6221DeltaFrontend._select_data(
            filter_type,
            values["digital_filter_type"],
        )
        filter_count.setValue(
            int(values["digital_filter_count"])
        )
        filter_window.setValue(
            float(
                values[
                    "digital_filter_window_percent"
                ]
            )
        )

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
        switcher = str(status.get("7001", ""))
        if switcher:
            self._switcher_available = not (
                switcher.startswith("Unavailable")
            )
            self.switcher_note.setText(
                (
                    "7001 available: CH1-CH4 may be used."
                    if self._switcher_available
                    else (
                        "7001 unavailable: only CH1 will be "
                        "measured. Saved CH2-CH4 selections "
                        "are retained but skipped."
                    )
                )
            )
            self._update_control_availability()
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

    def set_sequence_running(
        self,
        running: bool,
    ) -> None:
        self._sequence_running = running
        for button in (
            self.refresh_resources_button,
            self.test_connection_button,
            self.status_refresh_resources_button,
            self.refresh_status_button,
            self.safe_off_button,
        ):
            button.setEnabled(not running)
        self._update_control_availability()

    def _update_mode_page(
        self,
        *_args: Any,
    ) -> None:
        self.configuration_stack.setCurrentIndex(
            0
            if self.mode.currentData() == MODE_SHARED
            else 1
        )

    def _update_control_availability(self) -> None:
        for index in range(1, 5):
            key = f"ch{index}"
            available = (
                index == 1 or self._switcher_available
            )
            self.channel_enabled[key].setEnabled(
                available and not self._sequence_running
            )
            self.independent_tabs.setTabEnabled(
                index - 1,
                available,
            )

    def _update_resources(
        self,
        resources: tuple[str, ...],
    ) -> None:
        for combo in (
            self.resource_6221,
            self.resource_7001,
        ):
            current = combo.currentText().strip()
            blocker = QSignalBlocker(combo)
            combo.clear()
            for resource in sorted(
                set(resources),
                key=str.casefold,
            ):
                combo.addItem(resource)
            if (
                current
                and combo.findText(current) < 0
            ):
                combo.addItem(current)
            combo.setCurrentText(current)
            del blocker

    @staticmethod
    def _select_resource(
        combo: QComboBox,
        resource: str,
    ) -> None:
        value = resource.strip()
        if value and combo.findText(value) < 0:
            combo.addItem(value)
        combo.setCurrentText(value)

    @staticmethod
    def _select_data(
        combo: QComboBox,
        value: Any,
    ) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _resource_combo() -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setMinimumContentsLength(24)
        return combo

    def _connect_change_signals(self) -> None:
        for widget in self._setting_widgets():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(
                    self._changed
                )
                if widget.isEditable():
                    widget.currentTextChanged.connect(
                        self._changed
                    )
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._changed)
            elif isinstance(
                widget,
                (QSpinBox, QDoubleSpinBox),
            ):
                widget.valueChanged.connect(self._changed)

    def _setting_widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = [
            self.resource_6221,
            self.resource_7001,
            self.mode,
            self.io_timeout,
            self.switch_settle,
            *self.channel_enabled.values(),
            *self.shared_widgets.values(),
        ]
        for channel in self.independent_widgets.values():
            widgets.extend(channel.values())
        # 同一个控件只应安装一个 QSignalBlocker/信号连接。
        return list(dict.fromkeys(widgets))

    def _changed(self, *_args: Any) -> None:
        self.settingsChanged.emit()

    @staticmethod
    def _merged_settings(
        supplied: Mapping[str, Any],
    ) -> dict[str, Any]:
        defaults = default_settings()
        result = deepcopy(defaults)
        if not isinstance(supplied, Mapping):
            return result
        for key in (
            "resource_6221",
            "resource_7001",
            "mode",
            "io_timeout_seconds",
            "switch_settle_seconds",
        ):
            if key in supplied:
                result[key] = supplied[key]
        channels = supplied.get("channels")
        if isinstance(channels, Mapping):
            for key in result["channels"]:
                value = channels.get(key)
                if isinstance(value, Mapping):
                    result["channels"][key].update(value)
        shared = supplied.get("shared")
        if isinstance(shared, Mapping):
            result["shared"].update(shared)
        independent = supplied.get("independent")
        if isinstance(independent, Mapping):
            for key in result["independent"]:
                value = independent.get(key)
                if isinstance(value, Mapping):
                    result["independent"][key].update(
                        value
                    )
        return result


__all__ = ["Keithley6221DeltaFrontend"]
