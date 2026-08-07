"""LR-700 + LR-720-16 Measurement Module 的 PySide6 前端。

前端只编辑 desired settings、显示只读状态并发送手动动作请求。所有边界、总超时、
仪表协议、写入读回和最低激励确认都由独立 worker 中的后端重新验证；Load Settings
或 Load SEQ 只填充控件，不会自动连接或 Apply。
"""

from __future__ import annotations

from collections.abc import Mapping
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
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from labcontrol.measurement.frontend_api import ModuleUIAPI

from .constants import (
    EXCITATIONS,
    FILTERS,
    RESISTANCE_RANGES,
    default_settings,
)


class _SettingsPage(QWidget):
    """提供普通 DPI 下的首选尺寸，4K 缩放由 Qt 和内部滚动区共同处理。"""

    def sizeHint(self) -> QSize:  # noqa: N802
        # 四个逻辑槽位按 2×2 排列；1180 宽可完整展示两列参数，更小屏幕或 4K
        # 高缩放仍可使用内部横向、纵向滚动条访问全部控件。
        return QSize(1180, 700)


class LR700Frontend(QWidget):
    """LR-700 Settings/Status 页和设置序列化适配器。"""

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

        communication = QGroupBox(
            "GPIB Communication",
            content,
        )
        communication_layout = QGridLayout(
            communication
        )
        self.resource = QComboBox()
        # 自动发现只用于方便选择；允许手动输入已知 GPIB 地址，便于离线配置。
        self.resource.setEditable(True)
        self.resource.setMinimumContentsLength(24)
        self.refresh_resources_button = QPushButton(
            "Refresh GPIB"
        )
        self.refresh_resources_button.clicked.connect(
            lambda: self.api.action(
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
            QLabel("Read attempts"),
            1,
            2,
        )
        communication_layout.addWidget(
            self.retry_attempts,
            1,
            3,
        )
        layout.addWidget(communication)

        timing = QGroupBox(
            "Scan Timing",
            content,
        )
        timing_layout = QGridLayout(timing)
        self.switch_settle_seconds = (
            self._seconds_spin()
        )
        self.dwell_seconds = self._seconds_spin()
        timing_layout.addWidget(
            QLabel("Switch / filter settle"),
            0,
            0,
        )
        timing_layout.addWidget(
            self.switch_settle_seconds,
            0,
            1,
        )
        timing_layout.addWidget(
            QLabel("Snapshot dwell"),
            0,
            2,
        )
        timing_layout.addWidget(
            self.dwell_seconds,
            0,
            3,
        )
        timing_note = QLabel(
            "Settle must be at least the longest enabled "
            "digital filter. One Measure scans every enabled "
            "R slot and writes one sparse DAT row per slot."
        )
        timing_note.setWordWrap(True)
        timing_layout.addWidget(
            timing_note,
            1,
            0,
            1,
            4,
        )
        layout.addWidget(timing)

        channels_group = QGroupBox(
            "Measurement Channels",
            content,
        )
        channels_layout = QGridLayout(
            channels_group
        )
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
            input_channel.setAccessibleName(
                f"R{slot} physical sensor"
            )
            enabled = QCheckBox("Enable channel")
            enabled.setAccessibleName(f"Enable R{slot}")
            resistance_range = QComboBox()
            for index, label, _value in (
                RESISTANCE_RANGES
            ):
                resistance_range.addItem(
                    label,
                    index,
                )
            excitation = QComboBox()
            for index, label, _value in EXCITATIONS:
                excitation.addItem(label, index)
            excitation_percent = QSpinBox()
            excitation_percent.setRange(5, 100)
            excitation_percent.setSuffix(" %")
            excitation_percent.setSingleStep(5)
            digital_filter = QComboBox()
            for index, label, _seconds in FILTERS:
                digital_filter.addItem(label, index)

            form.addRow(
                "Physical sensor (1-16)",
                input_channel,
            )
            form.addRow("", enabled)
            form.addRow(
                "Resistance range",
                resistance_range,
            )
            form.addRow(
                "Excitation",
                excitation,
            )
            form.addRow(
                "Excitation percent",
                excitation_percent,
            )
            form.addRow(
                "Digital filter",
                digital_filter,
            )
            self.channel_widgets[key] = {
                "input_channel": input_channel,
                "enabled": enabled,
                "range_index": resistance_range,
                "excitation_index": excitation,
                "excitation_percent": (
                    excitation_percent
                ),
                "filter_index": digital_filter,
            }
            channels_layout.addWidget(
                group,
                (slot - 1) // 2,
                (slot - 1) % 2,
            )
        layout.addWidget(channels_group)

        safety_note = QLabel(
            "Important: LR-700 has no excitation-off command. "
            "Before switching sensors and after Measure/Stop/"
            "Error/Disable, the module confirms the minimum "
            "available state: 20 uV x 5% = 1 uV full scale. "
            "Enable only discovers resources. Test Connection "
            "is read-only. Apply Settings is always required."
        )
        safety_note.setWordWrap(True)
        safety_note.setStyleSheet(
            "color: #9a5200; font-weight: 600;"
        )
        layout.addWidget(safety_note)

        gpib_note = QLabel(
            "The LR-700 manual requires GPIB to be enabled "
            "from the instrument front panel (SPECIAL 4 1). "
            "Its factory GPIB address is 18 unless changed "
            "with SPECIAL 43."
        )
        gpib_note.setWordWrap(True)
        layout.addWidget(gpib_note)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        return page

    def _build_status_widget(
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
            "Protocol",
            "Applied Settings",
            "Sequence",
            "Excitation Safety",
            "Resource Discovery",
            "Estimated Measure Time (s)",
            "Current Sensor",
            "Current Range Index",
            "Current Excitation Index",
            "Current Excitation (%)",
            "Current Filter Index",
            "Last Slot / Sensor",
            "Last Resistance (Ohm)",
            "Last Reactance (Ohm)",
            "Last Status",
            "Last Action",
        ):
            label = QLabel("-")
            label.setWordWrap(True)
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
            lambda: self.api.action(
                "test_connection",
                {"settings": self.dump()},
            )
        )
        self.status_refresh_resources_button.clicked.connect(
            lambda: self.api.action(
                "refresh_resources"
            )
        )
        self.refresh_status_button.clicked.connect(
            self.api.refresh
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

    def dump(self) -> dict[str, Any]:
        """返回纯 Python desired settings；后端仍会把它当作不可信输入复检。"""

        channels: dict[str, dict[str, Any]] = {}
        for key, widgets in self.channel_widgets.items():
            input_channel = widgets[
                "input_channel"
            ]
            enabled = widgets["enabled"]
            resistance_range = widgets[
                "range_index"
            ]
            excitation = widgets[
                "excitation_index"
            ]
            percent = widgets[
                "excitation_percent"
            ]
            digital_filter = widgets[
                "filter_index"
            ]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(
                resistance_range,
                QComboBox,
            )
            assert isinstance(
                excitation,
                QComboBox,
            )
            assert isinstance(percent, QSpinBox)
            assert isinstance(
                digital_filter,
                QComboBox,
            )
            channels[key] = {
                "input_channel": input_channel.value(),
                "enabled": enabled.isChecked(),
                "range_index": int(
                    resistance_range.currentData()
                ),
                "excitation_index": int(
                    excitation.currentData()
                ),
                "excitation_percent": (
                    percent.value()
                ),
                "filter_index": int(
                    digital_filter.currentData()
                ),
            }
        return {
            "resource": (
                self.resource.currentText().strip()
            ),
            "switch_settle_seconds": (
                self.switch_settle_seconds.value()
            ),
            "dwell_seconds": (
                self.dwell_seconds.value()
            ),
            "io_timeout_seconds": (
                self.io_timeout.value()
            ),
            "retry_attempts": (
                self.retry_attempts.value()
            ),
            "channels": channels,
        }

    def load(
        self,
        settings: Mapping[str, Any],
    ) -> None:
        """加载保存值但阻断所有信号，不把 Load SEQ 误判为用户修改或 Apply。"""

        defaults = default_settings()
        values = {
            **defaults,
            **dict(settings),
        }
        raw_channels = settings.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raw_channels = {}
        blockers = [
            QSignalBlocker(widget)
            for widget in self._setting_widgets()
        ]
        self._select_resource(str(values["resource"]))
        self.switch_settle_seconds.setValue(
            float(
                values[
                    "switch_settle_seconds"
                ]
            )
        )
        self.dwell_seconds.setValue(
            float(values["dwell_seconds"])
        )
        self.io_timeout.setValue(
            float(values["io_timeout_seconds"])
        )
        self.retry_attempts.setValue(
            int(values["retry_attempts"])
        )
        for slot in range(1, 5):
            key = f"r{slot}"
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                supplied = {}
            channel = {
                **defaults["channels"][key],
                **dict(supplied),
            }
            widgets = self.channel_widgets[key]
            input_channel = widgets[
                "input_channel"
            ]
            enabled = widgets["enabled"]
            resistance_range = widgets[
                "range_index"
            ]
            excitation = widgets[
                "excitation_index"
            ]
            percent = widgets[
                "excitation_percent"
            ]
            digital_filter = widgets[
                "filter_index"
            ]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(
                resistance_range,
                QComboBox,
            )
            assert isinstance(
                excitation,
                QComboBox,
            )
            assert isinstance(percent, QSpinBox)
            assert isinstance(
                digital_filter,
                QComboBox,
            )
            input_channel.setValue(
                int(channel["input_channel"])
            )
            enabled.setChecked(
                bool(channel["enabled"])
            )
            self._select_data(
                resistance_range,
                int(channel["range_index"]),
            )
            self._select_data(
                excitation,
                int(
                    channel[
                        "excitation_index"
                    ]
                ),
            )
            percent.setValue(
                int(
                    channel[
                        "excitation_percent"
                    ]
                )
            )
            self._select_data(
                digital_filter,
                int(channel["filter_index"]),
            )
        del blockers

    def show_status(
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
        if (
            current
            and self.resource.findText(current) < 0
        ):
            self.resource.addItem(current)
        self.resource.setCurrentText(current)
        del blocker

    def _select_resource(self, resource: str) -> None:
        value = resource.strip()
        if (
            value
            and self.resource.findText(value) < 0
        ):
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
            self.switch_settle_seconds,
            self.dwell_seconds,
            self.io_timeout,
            self.retry_attempts,
        ]
        for channel in self.channel_widgets.values():
            widgets.extend(channel.values())
        return widgets

    @staticmethod
    def _seconds_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.1, 300.0)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" s")
        return spin


Frontend = LR700Frontend

__all__ = ["Frontend", "LR700Frontend"]
