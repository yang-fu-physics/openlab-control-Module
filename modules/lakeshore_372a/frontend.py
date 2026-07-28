"""Lake Shore 372A 模块窗口的 PySide6 前端。

前端只负责展示、编辑和序列化设置；所有范围、跨字段约束、仪表身份和写入读回仍由
独立 worker 中的后端重新验证。读取保存设置不会自动 Apply，连接测试也只验证当前
VISA 地址，不会改变仪表测量参数。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSize, QSignalBlocker
from PySide6.QtGui import QStandardItemModel
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

from labcontrol.measurement.frontend_api import (
    ModuleFrontend,
)

from .constants import (
    CURRENT_EXCITATIONS,
    FREQUENCIES_HZ,
    RESISTANCE_RANGES,
    VOLTAGE_EXCITATIONS,
    compatible_resistance_range_indices,
    default_settings,
)


class _SettingsPage(QWidget):
    """给核心浮动窗口提供合理初始尺寸，内容过大时由内部滚动区承载。"""

    def sizeHint(self) -> QSize:  # noqa: N802
        """返回普通 DPI 下的首选内容尺寸；Qt 仍会按系统缩放因子换算。"""

        return QSize(980, 600)


class LakeShore372AFrontend(ModuleFrontend):
    """372A 的 Settings/Status 两页视图及设置脏状态信号适配器。"""

    def create_settings_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        """创建可滚动 Settings 页，并只连接一次控件信号。"""

        page = _SettingsPage(parent)
        outer = QVBoxLayout(page)
        # 四组通道参数在 4K 缩放或较小屏幕上可能超过可用高度；滚动区让浮动窗口
        # 不必扩大到屏幕之外，setWidgetResizable 同时允许宽屏时自然伸展。
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
        # 下拉框展示自动发现结果，但必须允许手动输入离线配置中已知的 GPIB 地址。
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
            row = index // 2
            column = (index % 2) * 2
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
            3,
            0,
            1,
            4,
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
            self._select_data(resistance_range, 17)
            self._update_resistance_options(
                key,
                adjust_selection=True,
            )
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

        # 所有用户编辑最终只发 settingsChanged；是否允许 Apply、何时保存以及正在
        # Run 时的拒绝逻辑由核心统一管理，前端不直接调用后端。
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
            excitation = widgets["excitation_range"]
            assert isinstance(mode, QComboBox)
            assert isinstance(excitation, QComboBox)
            # mode 需要先重建 excitation 列表，excitation 需要先更新电阻量程可用状态。
            # 两者都先断开通用连接，确保一次用户操作只发一个 settingsChanged。
            for combo in (mode, excitation):
                try:
                    combo.currentTextChanged.disconnect(
                        self._changed
                    )
                except (RuntimeError, TypeError):
                    pass
            mode.currentIndexChanged.connect(
                lambda _index, channel_key=key:
                self._mode_changed(channel_key)
            )
            excitation.currentIndexChanged.connect(
                lambda _index, channel_key=key:
                self._excitation_changed(channel_key)
            )
        return page

    def create_status_page(
        self,
        parent: QWidget | None = None,
    ) -> QWidget:
        """创建只读状态页和 Idle 手动动作按钮。

        Test Connection 会把 Settings 页当前值作为一次性 payload 发送，方便在 Apply
        前验证新地址；后端不会因此保存设置或写入 FILTER/INSET/INTYPE。
        """

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
        """返回当前控件值的纯字典快照，供保存、Apply 或连接测试使用。

        这里不复制后端的完整校验逻辑；即使调用方绕过控件范围构造字典，后端也会把它
        当作不可信输入重新规范化。
        """

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
        """把保存设置与默认值合并后装入控件，但不发脏信号、更不会自动 Apply。"""

        defaults = default_settings()
        values = {
            **defaults,
            **dict(settings),
        }
        raw_channels = settings.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raw_channels = {}
        widgets = self._setting_widgets()
        # 批量加载期间阻断所有值变化信号。尤其是 excitation mode 会重建另一个
        # QComboBox；若不阻断，启动时仅“读取设置”就会被误认为用户修改。
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
            # Load Settings/SEQ 必须忠实保留文件中的值。若旧文件含不兼容组合，界面
            # 会把该项和其他不可用项置灰，后端 Apply 时仍会明确拒绝，而不会静默改值。
            self._update_resistance_options(
                key,
                adjust_selection=False,
            )
        del blockers

    def update_status(
        self,
        status: Mapping[str, Any],
    ) -> None:
        """合并 worker 状态到标签，并用资源发现结果刷新可编辑下拉框。"""

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
        """Run 期间禁用会产生 I/O 的手动按钮。

        这是界面层防误触；即使外部代码直接触发动作，核心服务仍会依据生命周期状态拒绝
        与正在运行的 Measure 冲突的请求。
        """

        for button in (
            self.refresh_resources_button,
            self.test_connection_button,
            self.status_refresh_resources_button,
            self.refresh_status_button,
        ):
            button.setEnabled(not running)

    def _mode_changed(self, key: str) -> None:
        """切换模式后重建激励表、修正电阻量程，并只发送一次变化信号。"""

        widgets = self.channel_widgets[key]
        excitation = widgets["excitation_range"]
        assert isinstance(excitation, QComboBox)
        selected = (
            int(excitation.currentData())
            if excitation.currentData() is not None
            else 5
        )
        self._populate_excitation(key, selected)
        self._update_resistance_options(
            key,
            adjust_selection=True,
        )
        self._changed()

    def _excitation_changed(self, key: str) -> None:
        """激励改变时同步禁用不兼容量程，并将旧选择夹到最近合法范围。"""

        self._update_resistance_options(
            key,
            adjust_selection=True,
        )
        self._changed()

    def _populate_excitation(
        self,
        key: str,
        selected: int,
    ) -> None:
        """按当前模式填充手册量程表，并把旧索引夹在新模式的合法范围内。"""

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

    def _update_resistance_options(
        self,
        key: str,
        *,
        adjust_selection: bool,
    ) -> None:
        """按手册 Figure 1-16 将不可用电阻量程置灰。

        用户主动改变模式或激励时，如果旧量程已经不可用，则选择索引距离最近的合法
        量程，避免产生一个无法 Apply 的新设置。加载文件时 ``adjust_selection`` 为
        False，保留原值供操作者检查；后端会在该槽位 Enabled 并执行 Apply 时再次
        进行同一约束的安全校验。
        """

        widgets = self.channel_widgets[key]
        mode = widgets["excitation_mode"]
        excitation = widgets["excitation_range"]
        resistance = widgets["resistance_range"]
        assert isinstance(mode, QComboBox)
        assert isinstance(excitation, QComboBox)
        assert isinstance(resistance, QComboBox)
        excitation_data = excitation.currentData()
        allowed = compatible_resistance_range_indices(
            str(mode.currentData()),
            (
                int(excitation_data)
                if excitation_data is not None
                else 0
            ),
        )
        allowed_set = set(allowed)
        model = resistance.model()
        if not isinstance(model, QStandardItemModel):
            raise RuntimeError(
                "Resistance range combo uses an "
                "unsupported item model"
            )
        for row in range(resistance.count()):
            range_index = int(
                resistance.itemData(row)
            )
            item = model.item(row)
            if item is None:
                continue
            available = range_index in allowed_set
            item.setEnabled(available)
            item.setToolTip(
                ""
                if available
                else (
                    "Unavailable for the selected "
                    "excitation"
                )
            )

        current_data = resistance.currentData()
        current = (
            int(current_data)
            if current_data is not None
            else None
        )
        if (
            adjust_selection
            and allowed
            and current not in allowed_set
        ):
            nearest = min(
                allowed,
                key=lambda value: (
                    abs(value - (current or value)),
                    value,
                ),
            )
            blocker = QSignalBlocker(resistance)
            self._select_data(resistance, nearest)
            del blocker
            current = nearest

        if current not in allowed_set:
            resistance.setToolTip(
                "Saved resistance range is incompatible "
                "with the selected excitation; choose an "
                "enabled range before enabling this slot "
                "and applying settings."
            )
        elif allowed:
            resistance.setToolTip(
                "Available resistance range indices for "
                f"this excitation: {allowed[0]}-"
                f"{allowed[-1]}"
            )

    def _update_resources(
        self,
        resources: tuple[str, ...],
    ) -> None:
        """更新发现列表，同时保留用户正在编辑但尚未被发现的手动地址。

        更新过程阻断信号，避免一次 Status 自动刷新把设置标成已修改。
        """

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
        """选择保存的资源；不在发现列表时先加入，支持完全离线的手动配置。"""

        value = resource.strip()
        if value and self.resource.findText(value) < 0:
            self.resource.addItem(value)
        self.resource.setCurrentText(value)

    @staticmethod
    def _select_data(
        combo: QComboBox,
        value: Any,
    ) -> None:
        """按 itemData 而不是可翻译的显示文字选择枚举项。"""

        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _setting_widgets(self) -> list[QWidget]:
        """返回所有参与保存和脏状态跟踪的控件，供加载时统一阻断信号。"""

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
        """把不同 Qt 信号携带的参数收敛成核心定义的无参数 settingsChanged。"""

        self.settingsChanged.emit()

    @staticmethod
    def _seconds_spin(
        minimum: int,
        maximum: int,
    ) -> QSpinBox:
        """创建显示秒单位的整数输入框；后端会再次检查相同边界。"""

        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSuffix(" s")
        return spin


__all__ = ["LakeShore372AFrontend"]
