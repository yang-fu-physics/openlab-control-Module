"""LR-700 + LR-720-16 Measurement Module 的 PySide6 前端。

前端只编辑 desired settings、按手册约束动态同步合法量程/激励选项、显示只读状态并发送
手动动作请求。所有边界、3 倍滤波等待、总超时、仪表协议、写入读回和按量程最低激励
确认都由独立 worker 中的后端重新验证；Load Settings 或 Load SEQ 只填充控件，不会
自动连接或 Apply。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSize, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
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
from labcontrol.ui.widgets import PrecisionDoubleSpinBox as QDoubleSpinBox

from .constants import (
    EXCITATIONS,
    FILTERS,
    RANGE_MODES,
    RESISTANCE_RANGES,
    default_settings,
    excitation_current_a,
    excitation_label,
    format_current,
    range_label,
    valid_excitation_indices,
)


class _SettingsPage(QWidget):
    """提供普通 DPI 下的首选尺寸，4K 缩放由 Qt 和内部滚动区共同处理。"""

    def sizeHint(self) -> QSize:  # noqa: N802
        # 四个逻辑槽位按 2×2 排列；1180 宽可完整展示两列参数，更小屏幕或 4K
        # 高缩放仍可使用内部横向、纵向滚动条访问全部控件。
        return QSize(1180, 760)


class LR700Frontend(QWidget):
    """LR-700 Settings/Status 页和设置序列化适配器。"""

    def __init__(self, api: ModuleUIAPI) -> None:
        super().__init__()
        self.api = api
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_settings(self))
        self.status_widget = self._build_status_widget()
        self.load(default_settings())

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
        self.resource.setMinimumContentsLength(24)
        self._load_resources()
        self.io_timeout = QDoubleSpinBox()
        self.io_timeout.setRange(0.1, 30.0)
        self.io_timeout.setDecimals(1)
        self.io_timeout.setSingleStep(0.5)
        self.io_timeout.setSuffix(" s")
        self.retry_attempts = QSpinBox()
        self.retry_attempts.setRange(1, 5)
        communication_layout.addWidget(
            QLabel("Instrument resource"),
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
            "Settle must be at least 3x the longest enabled "
            "digital filter (1 s -> 3 s, 3 s -> 9 s, 10 s -> 30 s). "
            "One Measure scans every enabled R slot and writes "
            "one sparse DAT row per slot."
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
        self._current_previews: dict[str, QLabel] = {}
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
            range_mode = QComboBox()
            for mode_value, mode_label in RANGE_MODES:
                range_mode.addItem(mode_label, mode_value)
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
            autorange_min_range = QComboBox()
            for index, label, _value in RESISTANCE_RANGES[:-1]:
                autorange_min_range.addItem(label, index)
            autorange_max_range = QComboBox()
            for index, label, _value in RESISTANCE_RANGES[1:]:
                autorange_max_range.addItem(label, index)
            digital_filter = QComboBox()
            for index, label, _seconds in FILTERS:
                digital_filter.addItem(label, index)
            current_preview = QLabel("-")
            current_preview.setWordWrap(True)

            form.addRow(
                "Physical sensor (1-16)",
                input_channel,
            )
            form.addRow("", enabled)
            form.addRow(
                "Range mode",
                range_mode,
            )
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
                "Autorange min (L low)",
                autorange_min_range,
            )
            form.addRow(
                "Autorange max (L high)",
                autorange_max_range,
            )
            form.addRow(
                "Digital filter",
                digital_filter,
            )
            form.addRow(
                "Calculated current",
                current_preview,
            )
            self.channel_widgets[key] = {
                "input_channel": input_channel,
                "enabled": enabled,
                "range_mode": range_mode,
                "range_index": resistance_range,
                "excitation_index": excitation,
                "excitation_percent": (
                    excitation_percent
                ),
                "autorange_min_range": autorange_min_range,
                "autorange_max_range": autorange_max_range,
                "filter_index": digital_filter,
            }
            self._current_previews[key] = current_preview
            channels_layout.addWidget(
                group,
                (slot - 1) // 2,
                (slot - 1) % 2,
            )
        layout.addWidget(channels_group)

        safety_note = QLabel(
            "Important: LR-700 has no excitation-off command. "
            "Apply, Fixed-range switches, and every Run end/Stop/Error/"
            "Disable confirm the minimum available excitation for the "
            "current resistance range at 5% (20 uV on 2 mOhm-20 kOhm, "
            "200 uV on 200 kOhm, 2 mV on 2 MOhm). Continuous Autorange "
            "runs enable AUTORANGE 1 once at Run start and only send "
            "SELECT (plus changed L=/FILTER) between Autorange slots. "
            "Enable only discovers resources. Test Connection is read-only. "
            "Apply Settings is always required."
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

        for key, widgets in self.channel_widgets.items():
            range_mode_combo = widgets["range_mode"]
            range_combo = widgets["range_index"]
            excitation_combo = widgets["excitation_index"]
            percent_spin = widgets["excitation_percent"]
            ar_min_combo = widgets["autorange_min_range"]
            ar_max_combo = widgets["autorange_max_range"]
            assert isinstance(range_mode_combo, QComboBox)
            assert isinstance(range_combo, QComboBox)
            assert isinstance(excitation_combo, QComboBox)
            assert isinstance(percent_spin, QSpinBox)
            assert isinstance(ar_min_combo, QComboBox)
            assert isinstance(ar_max_combo, QComboBox)
            range_mode_combo.currentIndexChanged.connect(
                lambda _idx, ch=key: self._sync_channel_controls(ch)
            )
            range_combo.currentIndexChanged.connect(
                lambda _idx, ch=key: self._on_range_changed(ch)
            )
            excitation_combo.currentIndexChanged.connect(
                lambda _idx, ch=key: self._update_current_preview(ch)
            )
            percent_spin.valueChanged.connect(
                lambda _val, ch=key: self._update_current_preview(ch)
            )
            ar_min_combo.currentIndexChanged.connect(
                lambda _idx, ch=key: self._update_current_preview(ch)
            )
            ar_max_combo.currentIndexChanged.connect(
                lambda _idx, ch=key: self._update_current_preview(ch)
            )

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
            "Estimated Measure Time (s)",
            "Current Sensor",
            "Current Range Index",
            "Current Excitation Index",
            "Current Excitation (%)",
            "Current Excitation Current",
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
        self.refresh_status_button = QPushButton(
            "Refresh Status"
        )
        self.test_connection_button.clicked.connect(
            lambda: self.api.action(
                "test_connection",
                {"settings": self.dump()},
            )
        )
        self.refresh_status_button.clicked.connect(
            self.api.refresh
        )
        buttons.addWidget(
            self.test_connection_button
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
            input_channel = widgets["input_channel"]
            enabled = widgets["enabled"]
            range_mode = widgets["range_mode"]
            resistance_range = widgets["range_index"]
            excitation = widgets["excitation_index"]
            percent = widgets["excitation_percent"]
            ar_min = widgets["autorange_min_range"]
            ar_max = widgets["autorange_max_range"]
            digital_filter = widgets["filter_index"]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(range_mode, QComboBox)
            assert isinstance(resistance_range, QComboBox)
            assert isinstance(excitation, QComboBox)
            assert isinstance(percent, QSpinBox)
            assert isinstance(ar_min, QComboBox)
            assert isinstance(ar_max, QComboBox)
            assert isinstance(digital_filter, QComboBox)
            channels[key] = {
                "input_channel": input_channel.value(),
                "enabled": enabled.isChecked(),
                "range_mode": str(range_mode.currentData() or "fixed"),
                "range_index": int(resistance_range.currentData()),
                "excitation_index": int(excitation.currentData()),
                "excitation_percent": percent.value(),
                "autorange_min_range": int(ar_min.currentData()),
                "autorange_max_range": int(ar_max.currentData()),
                "filter_index": int(digital_filter.currentData()),
            }
        return {
            "resource": str(self.resource.currentData() or ""),
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
            input_channel = widgets["input_channel"]
            enabled = widgets["enabled"]
            range_mode = widgets["range_mode"]
            resistance_range = widgets["range_index"]
            excitation = widgets["excitation_index"]
            percent = widgets["excitation_percent"]
            ar_min = widgets["autorange_min_range"]
            ar_max = widgets["autorange_max_range"]
            digital_filter = widgets["filter_index"]
            assert isinstance(input_channel, QSpinBox)
            assert isinstance(enabled, QCheckBox)
            assert isinstance(range_mode, QComboBox)
            assert isinstance(resistance_range, QComboBox)
            assert isinstance(excitation, QComboBox)
            assert isinstance(percent, QSpinBox)
            assert isinstance(ar_min, QComboBox)
            assert isinstance(ar_max, QComboBox)
            assert isinstance(digital_filter, QComboBox)
            input_channel.setValue(int(channel["input_channel"]))
            enabled.setChecked(bool(channel["enabled"]))
            self._select_data(
                range_mode,
                str(channel.get("range_mode", "fixed")).strip().casefold(),
            )
            range_index = int(channel["range_index"])
            self._select_data(resistance_range, range_index)
            self._populate_excitation_options(
                key,
                range_index,
                preferred=int(channel["excitation_index"]),
                keep_invalid=True,
            )
            percent.setValue(int(channel["excitation_percent"]))
            self._select_data(ar_min, int(channel["autorange_min_range"]))
            self._select_data(ar_max, int(channel["autorange_max_range"]))
            self._select_data(digital_filter, int(channel["filter_index"]))
            self._sync_channel_controls(key)
        del blockers

    def show_status(
        self,
        status: Mapping[str, Any],
    ) -> None:
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

    def _on_range_changed(self, key: str) -> None:
        widgets = self.channel_widgets[key]
        range_combo = widgets["range_index"]
        excitation_combo = widgets["excitation_index"]
        assert isinstance(range_combo, QComboBox)
        assert isinstance(excitation_combo, QComboBox)
        range_index = int(range_combo.currentData())
        previous = excitation_combo.currentData()
        preferred = int(previous) if isinstance(previous, int) else None
        self._populate_excitation_options(
            key,
            range_index,
            preferred=preferred,
            keep_invalid=False,
        )
        self._update_current_preview(key)

    def _populate_excitation_options(
        self,
        key: str,
        range_index: int,
        *,
        preferred: int | None,
        keep_invalid: bool,
    ) -> None:
        excitation_combo = self.channel_widgets[key]["excitation_index"]
        assert isinstance(excitation_combo, QComboBox)
        allowed = valid_excitation_indices(range_index)
        blocker = QSignalBlocker(excitation_combo)
        excitation_combo.clear()
        for idx in allowed:
            excitation_combo.addItem(excitation_label(idx), idx)
        if preferred is not None and preferred in allowed:
            self._select_data(excitation_combo, preferred)
        elif preferred is not None and keep_invalid and 0 <= preferred < len(EXCITATIONS):
            excitation_combo.addItem(
                f"{excitation_label(preferred)} (invalid on {range_label(range_index)})",
                preferred,
            )
            self._select_data(excitation_combo, preferred)
        elif allowed:
            if preferred is not None:
                nearest = min(allowed, key=lambda candidate: abs(candidate - preferred))
                self._select_data(excitation_combo, nearest)
            else:
                excitation_combo.setCurrentIndex(0)
        del blocker

    def _sync_channel_controls(self, key: str) -> None:
        widgets = self.channel_widgets[key]
        range_mode_combo = widgets["range_mode"]
        assert isinstance(range_mode_combo, QComboBox)
        is_autorange = str(range_mode_combo.currentData() or "fixed") == "autorange"
        for name in ("range_index", "excitation_index", "excitation_percent"):
            widgets[name].setEnabled(not is_autorange)
        for name in ("autorange_min_range", "autorange_max_range"):
            widgets[name].setEnabled(is_autorange)
        self._update_current_preview(key)

    def _update_current_preview(self, key: str) -> None:
        label = self._current_previews[key]
        widgets = self.channel_widgets[key]
        range_mode_combo = widgets["range_mode"]
        assert isinstance(range_mode_combo, QComboBox)
        mode = str(range_mode_combo.currentData() or "fixed")
        if mode == "autorange":
            ar_min = widgets["autorange_min_range"]
            ar_max = widgets["autorange_max_range"]
            assert isinstance(ar_min, QComboBox)
            assert isinstance(ar_max, QComboBox)
            lo = int(ar_min.currentData())
            hi = int(ar_max.currentData())
            if lo >= hi:
                label.setText("Invalid: Autorange max must exceed min")
                return
            label.setText(
                f"Uses bridge trace ({range_label(lo)} - {range_label(hi)}, 100%)"
            )
            return
        range_combo = widgets["range_index"]
        excitation_combo = widgets["excitation_index"]
        percent_spin = widgets["excitation_percent"]
        assert isinstance(range_combo, QComboBox)
        assert isinstance(excitation_combo, QComboBox)
        assert isinstance(percent_spin, QSpinBox)
        range_index = int(range_combo.currentData())
        exc_data = excitation_combo.currentData()
        if not isinstance(exc_data, int):
            label.setText("-")
            return
        current = excitation_current_a(
            range_index,
            exc_data,
            percent_spin.value(),
        )
        if exc_data not in valid_excitation_indices(range_index):
            label.setText(
                f"{format_current(current)} (unsupported on {range_label(range_index)})"
            )
            return
        label.setText(format_current(current))

    def _load_resources(self) -> None:
        """从核心资源快照创建不可编辑下拉框，不在模块内扫描 VISA。"""

        blocker = QSignalBlocker(self.resource)
        self.resource.clear()
        self.resource.addItem("Select instrument…", "")
        for resource_id, info in sorted(self.api.resources().items()):
            identity = str(info.get("identity") or "").strip()
            label = resource_id if not identity else f"{resource_id} — {identity}"
            self.resource.addItem(label, resource_id)
            self.resource.setItemData(
                self.resource.count() - 1,
                str(info.get("address", "")),
                Qt.ItemDataRole.ToolTipRole,
            )
        del blocker

    def _select_resource(self, resource: str) -> None:
        value = resource.strip()
        index = self.resource.findData(value)
        if index < 0 and value:
            self.resource.addItem(f"Unavailable — {value}", value)
            index = self.resource.count() - 1
        self.resource.setCurrentIndex(max(0, index))

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
