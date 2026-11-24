"""LR700 common configuration and per-channel fixed overrides. Load never applies hardware settings."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from PySide6.QtCore import QSize, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout, QGroupBox,
    QComboBox, QSpinBox, QCheckBox, QLabel, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from labcontrol.measurement.frontend_api import ModuleUIAPI
from labcontrol.ui.widgets import PrecisionDoubleSpinBox as QDoubleSpinBox
from .constants import (
    default_settings, common_defaults, RANGE_MODES, RESISTANCE_RANGES, FILTERS,
    autorange_trace_excitation_index, generate_trace, fixed_current_settings,
    excitation_current_a, format_current, range_label,
)


class LR700Frontend(QWidget):
    def __init__(self, api: ModuleUIAPI) -> None:
        super().__init__()
        self.api = api
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_settings())
        self.status_widget = self._build_status_widget()
        self.load({"common": common_defaults()})

    def sizeHint(self) -> QSize:
        return QSize(1040, 760)

    @staticmethod
    def _combo(items) -> QComboBox:
        combo = QComboBox()
        for value, label, *_rest in items:
            combo.addItem(label, value)
        return combo

    @staticmethod
    def _current_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(9)
        spin.setRange(0.000000001, 30.0)
        spin.setSingleStep(0.1)
        spin.setSuffix(" mA")
        return spin

    @staticmethod
    def _seconds_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.1, 300.0)
        spin.setDecimals(1)
        spin.setSuffix(" s")
        return spin

    def _build_settings(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        comm = QGroupBox("Communication / Timing")
        form = QGridLayout(comm)
        self.resource = QComboBox()
        self._load_resources()
        self.io_timeout = self._seconds_spin()
        self.io_timeout.setRange(0.1, 30)
        self.retry_attempts = QSpinBox()
        self.retry_attempts.setRange(1, 5)
        self.switch_settle_seconds = self._seconds_spin()
        self.dwell_seconds = self._seconds_spin()
        form.addWidget(QLabel("Instrument resource"), 0, 0)
        form.addWidget(self.resource, 0, 1, 1, 3)
        for row, label, widget, label2, widget2 in (
            (1, "I/O timeout", self.io_timeout, "Read attempts", self.retry_attempts),
            (2, "Switch / filter settle", self.switch_settle_seconds, "Snapshot dwell", self.dwell_seconds),
        ):
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(widget, row, 1)
            form.addWidget(QLabel(label2), row, 2)
            form.addWidget(widget2, row, 3)
        layout.addWidget(comm)
        group = QGroupBox("Common Configuration")
        form = QGridLayout(group)
        self.common_widgets = {
            "range_mode": self._combo(RANGE_MODES),
            "range_index": self._combo(RESISTANCE_RANGES),
            "current_a": self._current_spin(),
            "current_limit_a": self._current_spin(),
            "autorange_min_range": self._combo(RESISTANCE_RANGES[1:9]),
            "autorange_max_range": self._combo(RESISTANCE_RANGES[2:10]),
            "filter_index": self._combo(FILTERS),
        }
        fields = [("Range mode", "range_mode"), ("Maximum excitation current", "current_limit_a"),
                  ("Fixed resistance range", "range_index"), ("Fixed current", "current_a"),
                  ("Autorange minimum", "autorange_min_range"), ("Autorange maximum", "autorange_max_range"),
                  ("Digital filter", "filter_index")]
        for i, (label, name) in enumerate(fields):
            form.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            form.addWidget(self.common_widgets[name], i // 2, (i % 2) * 2 + 1)
        self.trace_preview = QLabel()
        self.trace_preview.setWordWrap(True)
        form.addWidget(self.trace_preview, 4, 0, 1, 4)
        layout.addWidget(group)
        self.channel_table = QTableWidget(4, 7)
        self.channel_table.setHorizontalHeaderLabels(["Enable", "Slot", "Sensor", "Independent fixed", "Fixed range", "Fixed current", "Actual current"])
        self.channel_table.verticalHeader().hide()
        self.channel_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.channel_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.channel_table.setMinimumHeight(165)
        self.channel_widgets = {}
        for row in range(4):
            widgets = {"enabled": QCheckBox(), "input_channel": QSpinBox(),
                       "independent": QCheckBox(), "range_index": self._combo(RESISTANCE_RANGES),
                       "current_a": self._current_spin(), "actual": QLabel()}
            widgets["input_channel"].setRange(1, 16)
            self.channel_widgets[f"r{row + 1}"] = widgets
            self.channel_table.setItem(row, 1, QTableWidgetItem(f"R{row + 1}"))
            for column, name in ((0, "enabled"), (2, "input_channel"), (3, "independent"),
                                 (4, "range_index"), (5, "current_a"), (6, "actual")):
                self.channel_table.setCellWidget(row, column, widgets[name])
        layout.addWidget(self.channel_table)
        note = QLabel(
            "Apply writes custom non-standard Trace T=4 from the current limit (not factory T=1/2/3). "
            "Fixed currents are rounded down to a supported value; the actual value is shown above. "
            "Same range/current: SELECT only. Different configuration: lower excitation, then restore. "
            "Settle must be at least 3x the digital filter. Run end/Stop/Error/Disable restores minimum "
            "excitation at 5%; LR700 has no excitation-off command. Front panel GPIB requires SPECIAL 4 1 "
            "(factory address 18, SPECIAL 43); Enable and Test Connection are read-only."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        scroll.setWidget(page)
        for widget in self._setting_widgets():
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._update_preview)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._update_preview)
            elif widget is not self.resource:
                widget.valueChanged.connect(self._update_preview)
        return scroll

    def dump(self) -> dict[str, Any]:
        common = {}
        for name, widget in self.common_widgets.items():
            common[name] = widget.currentData() if isinstance(widget, QComboBox) else widget.value() / 1000
        channels = {}
        for key, w in self.channel_widgets.items():
            channels[key] = {"enabled": w["enabled"].isChecked(), "input_channel": w["input_channel"].value(),
                             "independent": w["independent"].isChecked(), "range_index": w["range_index"].currentData(),
                             "current_a": w["current_a"].value() / 1000}
        return {"resource": str(self.resource.currentData() or ""),
                "io_timeout_seconds": self.io_timeout.value(), "retry_attempts": self.retry_attempts.value(),
                "switch_settle_seconds": self.switch_settle_seconds.value(), "dwell_seconds": self.dwell_seconds.value(),
                "common": common, "channels": channels}

    def load(self, settings: Mapping[str, Any]) -> None:
        defaults = default_settings()
        values = {**defaults, **settings}
        raw_channels = settings.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raw_channels = {}
        # Existing per-channel files are converted and validated BEFORE mutating any UI widget.
        legacy = "common" not in settings and bool(raw_channels)
        merged_channels: dict[str, dict[str, Any]] = {}
        for slot in range(1, 5):
            key = f"r{slot}"
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                supplied = {}
            merged_channels[key] = {**defaults["channels"][key], **supplied}

        common = {**common_defaults(), **(settings["common"] if isinstance(settings.get("common"), Mapping) else {})}
        if legacy:
            enabled_channels = [c for c in merged_channels.values() if c.get("enabled")]
            first = next(
                (c for c in enabled_channels if c.get("range_mode") == "autorange"),
                enabled_channels[0] if enabled_channels else defaults["channels"]["r1"],
            )
            if any(c["filter_index"] != first["filter_index"] for c in enabled_channels):
                raise ValueError("Legacy channels use different filters; select one common filter before migration")
            common.update({
                k: first[k]
                for k in ("range_mode", "range_index", "autorange_min_range", "autorange_max_range", "filter_index")
            })
            common["current_a"] = excitation_current_a(
                first["range_index"], first["excitation_index"], first["excitation_percent"]
            )
            limit_candidates: list[float] = []
            for channel in merged_channels.values():
                channel["current_a"] = excitation_current_a(
                    channel["range_index"], channel["excitation_index"], channel["excitation_percent"]
                )
                channel["independent"] = channel["range_mode"] == "fixed"
                if channel["enabled"] and channel["range_mode"] == "autorange":
                    if any(
                        channel[k] != common[k] for k in ("autorange_min_range", "autorange_max_range")
                    ):
                        raise ValueError(
                            "Legacy channels use different autorange limits; select common limits before migration"
                        )
                    lo = int(channel["autorange_min_range"])
                    hi = int(channel["autorange_max_range"])
                    limit_candidates.extend(
                        excitation_current_a(r, autorange_trace_excitation_index(r), 100)
                        for r in range(max(1, lo), min(9, hi) + 1)
                    )
                elif channel["enabled"]:
                    limit_candidates.append(channel["current_a"])
            common["current_limit_a"] = max(limit_candidates) if limit_candidates else common["current_a"]

        blockers = [QSignalBlocker(w) for w in self._setting_widgets()]
        self._select_resource(str(values["resource"]))
        for name, widget in (("io_timeout_seconds", self.io_timeout), ("retry_attempts", self.retry_attempts),
                             ("switch_settle_seconds", self.switch_settle_seconds), ("dwell_seconds", self.dwell_seconds)):
            widget.setValue(values[name])
        for name, widget in self.common_widgets.items():
            if isinstance(widget, QComboBox):
                self._select_data(widget, common[name])
            else:
                widget.setValue(float(common[name]) * 1000)
        for slot in range(1, 5):
            key = f"r{slot}"
            channel = merged_channels[key]
            w = self.channel_widgets[key]
            w["enabled"].setChecked(bool(channel["enabled"]))
            w["input_channel"].setValue(int(channel["input_channel"]))
            w["independent"].setChecked(bool(channel.get("independent", False)))
            self._select_data(w["range_index"], int(channel["range_index"]))
            w["current_a"].setValue(float(channel.get("current_a", common["current_a"])) * 1000)
        del blockers
        self._update_preview()

    def _update_preview(self, *_args) -> None:
        settings = self.dump()
        common = settings["common"]
        auto = common["range_mode"] == "autorange"
        for name in ("range_index", "current_a"):
            self.common_widgets[name].setEnabled(not auto)
        for name in ("autorange_min_range", "autorange_max_range"):
            self.common_widgets[name].setEnabled(auto)
        if not auto:
            self.trace_preview.setText("Fixed mode: no Trace is written. Actual fixed currents are shown below.")
        elif common["autorange_min_range"] >= common["autorange_max_range"]:
            self.trace_preview.setText("Autorange maximum must exceed minimum")
        else:
            try:
                trace, lo, hi = generate_trace(
                    common["current_limit_a"],
                    common["autorange_min_range"],
                    common["autorange_max_range"],
                )
                self.trace_preview.setText(
                    f"Effective autorange: {range_label(lo)} - {range_label(hi)}; T=4, U="
                    + "".join(map(str, trace))
                    + "\n"
                    + " | ".join(
                        f"{range_label(r)}: {format_current(excitation_current_a(r, trace[r]))}"
                        for r in range(lo, hi + 1)
                    )
                )
            except ValueError as exc:
                self.trace_preview.setText(str(exc))
        for key, channel in settings["channels"].items():
            w = self.channel_widgets[key]
            independent = channel["independent"]
            for name in ("range_index", "current_a"):
                w[name].setEnabled(independent)
            target = channel if independent else common
            if not independent and auto:
                w["actual"].setText("Trace / automatic")
                continue
            try:
                _, _, actual = fixed_current_settings(target["range_index"], target["current_a"])
                text = format_current(actual)
                if target["current_a"] > common["current_limit_a"]:
                    text += " (exceeds limit)"
                w["actual"].setText(text)
            except ValueError as exc:
                w["actual"].setText(str(exc))

    def _setting_widgets(self) -> list[QWidget]:
        widgets = [self.resource, self.io_timeout, self.retry_attempts, self.switch_settle_seconds, self.dwell_seconds,
                   *self.common_widgets.values()]
        for channel in self.channel_widgets.values():
            widgets.extend(w for k, w in channel.items() if k != "actual")
        return widgets

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
            "Current Difference Mode",
            "10x References",
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


Frontend = LR700Frontend
__all__ = ["Frontend", "LR700Frontend"]
