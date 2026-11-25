"""Linear Research LR-700 + LR-720-16 Measurement Module 后端。

实现依据用户提供的 LR-700 v1.3 手册及真实 LR-700/LR-720 实测核对结果：

- ``SELECT S=nn`` 选择 LR-720-16 的 1-16 号传感器；
- ``RANGE / EXCITATION / VAREXC / FILTER / AUTORANGE`` 设置固定或自动量程桥参数；
- ``GET 0 / GET 1`` 分别读取 R 与 X；
- ``GET 6`` 读回量程、激励、滤波、模式和传感器号；
- ``GET 7`` 读取 8 位 ``OVERLOADS`` 状态（含共模过载排查提示）。

LR-700 没有真正的 excitation-off 命令。软件能确认的最低状态是“当前电阻量程允许的
最低激励档 × 5%”（0-7 档为 20 uV，200 kOhm 为 200 uV，2 MOhm 为 2 mV）；
配置变化、Stop、Error、Disable 和退出时回到并读回确认该状态；同配置通道切换只发送 SELECT。任何无法读回确认
最低激励的情况都是 Error，不能仅凭 worker 已退出宣称仪表安全。
"""

from __future__ import annotations

import math
import time
from labcontrol.formatting import require_precision
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from labcontrol.module_api import (
    ModuleError,
    ModuleAPI,
    ModuleWarning,
)

from .constants import (
    AUTORANGE_MAX_RANGE_INDEX,
    AUTORANGE_MIN_RANGE_INDEX,
    COMMON_MODE_OVERLOAD_HINT,
    COMMON_MODE_OVERLOAD_MASK,
    FILTERS,
    FILTER_SETTLE_MULTIPLIER,
    OVERLOAD_BITS,
    SAFE_EXCITATION_PERCENT,
    STATUS_CODE_INVALID_READING,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVERLOAD,
    STATUS_CODE_OVER_RANGE,
    autorange_trace_excitation_index,
    default_settings,
    common_defaults,
    generate_trace,
    fixed_current_settings,
    excitation_current_a,
    excitation_label,
    format_current,
    is_valid_range_excitation,
    minimum_excitation_index,
    range_label,
    range_ohms,
    valid_excitation_indices,
)
from . import lr700 as instrument


TransportFactory = Callable[
    [str, float],
    instrument.Transport,
]
Waiter = Callable[[ModuleAPI, float], None]


class LR700Backend:
    """LR-700 生命周期、四槽位扫描、读回验证和安全恢复状态机。

    每个 Enabled 传感器固定执行以下事务：

    1. 先确认桥处于最低激励，再切换 LR-720-16；
    2. 发送当前通道的量程、激励、比例、滤波和 R/X 模式；
    3. 用 ``GET 6`` 核对每个可读回字段；
    4. 等待切换/滤波稳定时间，获取第一份核心温场快照；
    5. 等待 dwell，获取第二份温场快照；
    6. 读取 R、X、过载字，并再次核对设置未被前面板改变；
    7. 写一行只包含当前 Rn/Xn 和公共 StatusCode 的稀疏数据；
    8. 无论成功、Stop 或异常，都尝试恢复并确认最低激励。
    """

    columns = {
        "TemperatureAverage": "K",
        "FieldAverage": "Oe",
        "R1": "Ohm",
        "X1": "Ohm",
        "R2": "Ohm",
        "X2": "Ohm",
        "R3": "Ohm",
        "X3": "Ohm",
        "R4": "Ohm",
        "X4": "Ohm",
        "StatusCode": "",
    }
    display_columns = ("R1", "R2", "R3", "R4")

    sequence_commands = [
        {"id": "x10_on", "label": "10x Delta R On", "kind": "command",
         "description": "Capture fixed R Set references for every enabled sensor, freeze their settled range/excitation and enable 10x. Restore each sensor's reference during the field sweep.", "fields": []},
        {"id": "x10_off", "label": "10x Delta R Off", "kind": "command",
         "description": "Return to 1x difference gain, keeping R Set unchanged.", "fields": []},
    ]

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or instrument.PyVisaTransport
        )
        self._custom_waiter = waiter is not None
        self._waiter = (
            waiter
            or (
                lambda api, seconds:
                api.sleep(seconds)
            )
        )
        self.transport: instrument.Transport | None = None
        self.desired_settings = default_settings()
        self.applied_settings: dict[str, Any] = {}
        self.protocol_signature = ""
        self.sequence_active = False
        self.last_values: dict[str, Any] = {}
        self._last_safe_bridge: dict[str, int] | None = None
        self._measurement_mode = 0
        self._x10_channels: dict[int, dict[str, Any]] = {}
        self._autorange_running = False
        self._autorange_active_limits: tuple[int, int] | None = None
        self._autorange_active_filter: int | None = None
        self._autorange_last_range: dict[int, int] = {}

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 只读取核心资源表，不连接、不写 LR-700。"""

        self._require_live_context(api)
        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_resource=False,
            operation_timeout_seconds=(
                api.timeout
            ),
        )
        api.resources()
        status = {
            "Connection": "Disconnected",
            "Resource": (
                self.desired_settings["resource"]
                or "Not selected"
            ),
            "Protocol": "Not queried",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Excitation Safety": (
                "Not connected; instrument state unknown"
            ),
            **self._cleared_bridge_status_fields("-"),
            "Last Slot / Sensor": "-",
            "Last Resistance (Ohm)": "-",
            "Last Reactance (Ohm)": "-",
            "Last Status": "-",
        }
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """验证通用/独立配置，连接并降至最低激励；自动量程 Trace 在此写入并保存。"""

        desired = self._normalized_settings(
            settings,
            require_resource=True,
            operation_timeout_seconds=(
                api.timeout
            ),
        )
        self.desired_settings = deepcopy(desired)

        # 重新 Apply 前不能直接关闭旧 session。LR-700 没有真正的 Output Off，
        # Idle 期间前面板也可能把激励调高，因此必须先按旧连接写入并读回最低
        # 激励；确认失败时保留旧配置，等待用户处理或重试。
        if self.transport is not None:
            if self.applied_settings:
                try:
                    self._set_safe_state(api, reset_mode=True)
                except Exception as failure:
                    cleanup_error = self._best_effort_safe_state(api, reset_mode=True)
                    if cleanup_error is not None:
                        raise ModuleError(
                            "Cannot replace the existing LR-700 session because "
                            "minimum excitation could not be confirmed: "
                            f"{cleanup_error}",
                            "LR700_SAFE_STATE_FAILED",
                            "configure",
                        ) from failure
                    raise
            self._close_transport()
        self.applied_settings = {}
        connected = False
        try:
            self._connect(
                desired["resource"],
                float(desired["io_timeout_seconds"]),
                api,
            )
            connected = True
            safe_bridge = self._set_safe_state(api, reset_mode=True)
            if "common" in desired:
                limit_a = float(desired["common"]["current_limit_a"])
                current_range = int(safe_bridge["range_index"])
                for channel in desired["channels"].values():
                    if not channel["enabled"]:
                        continue
                    if channel["range_mode"] == "fixed":
                        self._require_safe_range_transition(
                            current_range,
                            int(channel["range_index"]),
                            limit_a,
                        )
                    else:
                        lo = int(channel["autorange_min_range"])
                        hi = int(channel["autorange_max_range"])
                        clamped = max(lo, min(hi, current_range))
                        self._require_safe_range_transition(
                            current_range,
                            clamped,
                            limit_a,
                        )
            if "common" in desired and any(
                c["enabled"] and c["range_mode"] == "autorange"
                for c in desired["channels"].values()
            ):
                for trace_cmd in instrument.autorange_trace_commands(
                    desired["autorange_trace"]
                ):
                    self._write_absolute(trace_cmd, api)
                safe_bridge = self._query_bridge_settings(api)
                self._verify_safe_state(safe_bridge)
        except Exception as failure:
            cleanup_error = (
                self._best_effort_safe_state(api, reset_mode=True)
                if connected
                else None
            )
            self._close_transport()
            self.applied_settings = {}
            self.sequence_active = False
            if cleanup_error is not None:
                raise ModuleError(
                    "Apply failed and LR-700 minimum "
                    "excitation could not be confirmed: "
                    f"{cleanup_error}",
                    "LR700_SAFE_STATE_FAILED",
                ) from failure
            raise
        self.applied_settings = deepcopy(desired)
        self._x10_channels.clear()
        self._measurement_mode = 0
        self._autorange_last_range.clear()
        status = {
            "Connection": "Connected",
            "Resource": desired["resource"],
            "Protocol": self.protocol_signature,
            "Applied Settings": (
                "Applied; generated trace saved; "
                "channel settings resolved for Measure"
            ),
            "Excitation Safety": self._safe_state_text(safe_bridge),
            **self._bridge_status_fields(safe_bridge),
            "10x References": "Inactive",
            "Estimated Measure Time (s)": (
                self._estimated_measure_seconds(desired)
            ),
        }
        api.status(status)
        return status

    def execute_sequence_command(self, command_id, parameters, api):
        self._require_ready()
        settings = self.applied_settings
        settle = float(settings["switch_settle_seconds"])
        io_margin = self._io_retry_margin(settings)
        if command_id == "x10_off":
            estimate = settle + io_margin
            if estimate > api.timeout - 2.0:
                raise ModuleError(
                    f"Disabling LR700 10x mode may require {estimate:.1f} s; "
                    "increase the module operation timeout",
                    "LR700_REFERENCE_TIMEOUT_UNSAFE",
                )
            self._write_absolute(instrument.mode_command(0), api)
            bridge = self._query_bridge_settings(api)
            if bridge["mode"] != 0:
                raise ModuleError("LR700 did not confirm 1x mode", "LR700_SETTINGS_VERIFY_FAILED")
            self._measurement_mode = 0
            self._x10_channels.clear()
            self._waiter(api, settle)
            status = {**self._bridge_status_fields(bridge), "10x References": "Inactive",
                      "Last Action": "10x off; R Set retained; normal channel settings resume on Measure"}
        elif command_id == "x10_on":
            attempts = sum(
                self._slot_measure_attempts(settings["channels"][f"r{slot}"], settings)
                for slot in self.slots
            )
            estimate = attempts * (settle + 2.0) + settle + io_margin
            if estimate > api.timeout - 2.0:
                raise ModuleError(
                    f"Capturing all LR700 references may require {estimate:.1f} s; "
                    "increase the module operation timeout before activation",
                    "LR700_REFERENCE_TIMEOUT_UNSAFE",
                )
            try:
                self._write_absolute(instrument.mode_command(0), api)
                mode_bridge = self._query_bridge_settings(api)
                if mode_bridge["mode"] != 0:
                    raise ModuleError("LR700 did not confirm 1x mode", "LR700_SETTINGS_VERIFY_FAILED")
                self._measurement_mode = 0
                self._x10_channels.clear()
                captured: dict[int, dict[str, Any]] = {}
                last_sensor: int | None = None
                last_frozen: dict[str, Any] | None = None
                for slot in self.slots:
                    channel = settings["channels"][f"r{slot}"]
                    sensor = int(channel["input_channel"])
                    # Clear both RSET and any stale XSET BEFORE switching/autorange starts
                    # so AUTORANGE 1 never sees a leftover offset from a prior channel.
                    self._write_absolute(instrument.OFFSET_CLEAR_ALL, api)
                    self._prepare_sensor(sensor, channel, settings, api)
                    for _attempt in range(self._slot_measure_attempts(channel, settings)):
                        if channel["range_mode"] == "autorange":
                            self._waiter(api, settle / 4)
                            mid = self._query_bridge_settings(api)
                            self._verify_channel_readback(sensor, channel, mid)
                            self._waiter(api, settle * 3 / 4)
                            before = self._query_bridge_settings(api)
                            self._verify_channel_readback(sensor, channel, before)
                            if not self._same_bridge_range_state(mid, before):
                                continue
                        else:
                            self._waiter(api, settle)
                            before = self._query_bridge_settings(api)
                            self._verify_channel_readback(sensor, channel, before)
                        resistance = self._query_measurement(
                            instrument.GET_RESISTANCE, "R", before["range_index"], api
                        )
                        bits = self._query_overloads(api)
                        after = self._query_bridge_settings(api)
                        self._verify_channel_readback(sensor, channel, after)
                        if not self._same_bridge_range_state(before, after):
                            continue
                        if bits:
                            raise ModuleWarning(
                                f"Cannot capture R{slot}/S{sensor} reference while the bridge reports overload",
                                "LR700_REFERENCE_OVERLOAD",
                            )
                        break
                    else:
                        raise ModuleWarning(
                            f"LR700 did not settle before capturing R{slot}/S{sensor}",
                            "LR700_AUTORANGE_UNSTABLE",
                        )
                    self._write_absolute(instrument.AUTORANGE_OFF, api)
                    self._reset_autorange_session()
                    frozen = {**channel, "range_mode": "fixed", **{name: after[name] for name in (
                        "range_index", "excitation_index", "excitation_percent", "filter_index")}}
                    full_scale = range_ohms(after["range_index"])
                    counts = round(resistance / full_scale * 200000)
                    if not -199999 <= counts <= 199999:
                        raise ModuleWarning(
                            f"R{slot} cannot be represented by R Set on this range",
                            "LR700_REFERENCE_OUT_OF_RANGE",
                        )
                    reference = self._set_reference(counts, after["range_index"], api)
                    captured[slot] = {"channel": frozen, "counts": counts, "reference": reference}
                    self._autorange_last_range[sensor] = after["range_index"]
                    last_sensor = sensor
                    last_frozen = frozen
                self._write_absolute(instrument.mode_command(1), api)
                bridge = self._query_bridge_settings(api)
                if bridge["mode"] != 1:
                    raise ModuleError("LR700 did not confirm 10x mode", "LR700_SETTINGS_VERIFY_FAILED")
                self._measurement_mode = 1
                self._waiter(api, settle)
                bridge = self._query_bridge_settings(api)
                assert last_sensor is not None and last_frozen is not None
                self._verify_channel_readback(last_sensor, last_frozen, bridge)
                self._x10_channels = captured
                references = "; ".join(
                    f"R{slot}/S{entry['channel']['input_channel']}: "
                    f"{entry['reference']:.12g} Ohm @ {range_label(entry['channel']['range_index'])}"
                    for slot, entry in captured.items()
                )
                status = {
                    **self._bridge_status_fields(bridge),
                    "10x References": references,
                    "Last Action": "All enabled sensor references captured; 10x on, ranges fixed",
                }
            except BaseException as failure:
                self._x10_channels.clear()
                cleanup_error = self._best_effort_safe_state(api, reset_mode=True)
                if cleanup_error is None and self._last_safe_bridge is not None:
                    api.status({
                        **self._bridge_status_fields(self._last_safe_bridge),
                        "10x References": "Inactive",
                        "Excitation Safety": self._safe_state_text(self._last_safe_bridge),
                    })
                else:
                    api.status({
                        **self._cleared_bridge_status_fields("Unknown"),
                        "10x References": "Inactive",
                        "Excitation Safety": "Minimum excitation unconfirmed",
                    })
                if cleanup_error is not None:
                    raise ModuleError(
                        "LR-700 10x capture aborted and minimum excitation could not be confirmed: "
                        f"{cleanup_error}",
                        "LR700_SAFE_STATE_FAILED",
                    ) from failure
                raise
        else:
            raise ModuleError(f"Unknown LR700 command: {command_id}", "LR700_INVALID_COMMAND")
        api.status(status)
        return status

    def _set_reference(self, counts: int, range_index: int, api: ModuleAPI) -> float:
        self._write_absolute(instrument.offset_r_command(counts), api)
        reference = self._query_measurement(instrument.GET_RSET, "RSET", range_index, api)
        full_scale = range_ohms(range_index)
        if abs(reference - counts * full_scale / 200000) > full_scale / 200000 * 0.51:
            raise ModuleError("LR700 R Set readback mismatch", "LR700_SETTINGS_VERIFY_FAILED")
        return reference

    def _prepare_x10_sensor(self, slot, input_channel, channel, settings, api):
        current = self._query_bridge_settings(api)
        if current["sensor"] == input_channel and current["mode"] == 1 and all(
            current[name] == int(channel[name])
            for name in ("range_index", "excitation_index", "excitation_percent", "filter_index")
        ):
            self._verify_channel_readback(input_channel, channel, current)
            return current
        self._write_absolute(instrument.mode_command(0), api)
        mode_bridge = self._query_bridge_settings(api)
        if mode_bridge["mode"] != 0:
            raise ModuleError("LR700 did not confirm 1x mode before sensor switch", "LR700_SETTINGS_VERIFY_FAILED")
        self._measurement_mode = 0
        self._prepare_sensor(input_channel, channel, settings, api)
        self._set_reference(self._x10_channels[slot]["counts"], channel["range_index"], api)
        self._write_absolute(instrument.mode_command(1), api)
        bridge = self._query_bridge_settings(api)
        if bridge["mode"] != 1:
            raise ModuleError("LR700 did not confirm 10x mode after sensor switch", "LR700_SETTINGS_VERIFY_FAILED")
        self._measurement_mode = 1
        self._verify_channel_readback(input_channel, channel, bridge)
        return bridge

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """开始 Run 前确认安全态；若首个启用槽位为自动量程则一次性开启 AUTORANGE 1。"""

        self._require_ready()
        first_slot = self.slots[0]
        first_channel = self.applied_settings["channels"][f"r{first_slot}"]
        first_is_autorange = (
            str(first_channel.get("range_mode", "fixed")).strip().casefold()
            == "autorange"
        )
        try:
            safe_bridge = self._set_safe_state(api)
            active_bridge = safe_bridge
            safety_text = self._safe_state_text(safe_bridge)
            if first_is_autorange:
                first_input = int(first_channel["input_channel"])
                active_bridge = self._start_autorange_session(
                    first_input,
                    first_channel,
                    api,
                    current_range_index=int(safe_bridge["range_index"]),
                    target_range_index=self._autorange_last_range.get(first_input),
                )
                safety_text = (
                    f"Autorange active (R{first_slot} / S{first_input}); "
                    "minimum excitation will be restored on "
                    "Run end/Stop/Error/Disable"
                )
        except Exception as failure:
            cleanup_error = (
                self._best_effort_safe_state(api)
            )
            if cleanup_error is not None:
                raise ModuleError(
                    "Begin Sequence failed and LR-700 "
                    "minimum excitation could not be "
                    f"confirmed: {cleanup_error}",
                    "LR700_SAFE_STATE_FAILED",
                ) from failure
            raise
        self.sequence_active = True
        status = {
            "Sequence": "Running",
            "Excitation Safety": safety_text,
            **self._bridge_status_fields(active_bridge),
        }
        api.status(status)
        return status

    def measure(
        self,
        slot: int,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """只测量核心当前调度的一个逻辑槽位，并返回一行稀疏结果。"""

        self._require_ready()
        settings = self.applied_settings
        if not 1 <= slot <= 4:
            raise ModuleError(
                "LR-700 received an invalid logical slot",
                "LR700_LOGICAL_SLOT_INVALID",
            )
        channel = settings["channels"][f"r{slot}"]
        if not channel["enabled"]:
            raise ModuleError(
                f"R{slot} is not enabled for this sequence",
                "LR700_LOGICAL_SLOT_DISABLED",
                f"r{slot}",
            )
        if self._x10_channels:
            channel = self._x10_channels[slot]["channel"]
        self._validate_measure_duration(
            settings,
            api.timeout,
        )
        api.sleep(0)
        return self._measure_sensor(
            slot,
            int(channel["input_channel"]),
            channel,
            settings,
            api,
        )

    @property
    def slots(self) -> tuple[int, ...]:
        self._require_ready()
        assert self.applied_settings is not None
        return tuple(
            slot
            for slot in range(1, 5)
            if self.applied_settings["channels"][f"r{slot}"]["enabled"]
        )

    def _prepare_sensor(self, input_channel, channel, settings, api):
        is_autorange = channel["range_mode"] == "autorange"
        if is_autorange and self._autorange_running:
            configured_bridge = self._switch_autorange_sensor(
                input_channel,
                channel,
                api,
            )
        elif is_autorange:
            safe_bridge = self._set_safe_state(api)
            configured_bridge = self._start_autorange_session(
                input_channel,
                channel,
                api,
                current_range_index=int(safe_bridge["range_index"]),
                target_range_index=self._autorange_last_range.get(input_channel),
            )
        else:
            current = (
                self._query_bridge_settings(api)
                if ("common" in settings or self._x10_channels) and self.sequence_active
                else None
            )
            same_configuration = (
                current is not None
                and not self._autorange_running
                and current["mode"] == self._measurement_mode
                and all(
                    current[name] == int(channel[name])
                    for name in ("range_index", "excitation_index", "excitation_percent", "filter_index")
                )
            )
            if same_configuration:
                self._write_absolute(instrument.select_sensor_command(input_channel), api)
                configured_bridge = self._query_bridge_settings(api)
                self._verify_channel_readback(input_channel, channel, configured_bridge)
            else:
                safe_bridge = self._set_safe_state(api)
                configured_bridge = self._configure_sensor(
                    input_channel, channel, api,
                    current_range_index=int(safe_bridge["range_index"]),
                )
        return configured_bridge

    def _measure_sensor(
        self,
        slot: int,
        input_channel: int,
        channel: Mapping[str, Any],
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """执行一个逻辑槽位的完整事务。

        固定量程槽位每次测量前后都恢复并确认最低激励；连续自动量程 Run 期间
        保持 ``AUTORANGE 1``，切通道只下发 ``SELECT S=nn``（及发生变化的 ``L=``
        与 ``FILTER``）。运行中触发可恢复的量程未稳警告（``LR700_AUTORANGE_UNSTABLE``）
        不退出自动量程也不发额外清理命令，仅在 Run 结束、Stop、Error、Disable 或
        其他异常时恢复 ``AUTORANGE 0`` + 5% 最低激励。
        """

        is_autorange = (
            str(channel.get("range_mode", "fixed")).strip().casefold()
            == "autorange"
        )
        failure: BaseException | None = None
        try:
            if self._x10_channels:
                configured_bridge = self._prepare_x10_sensor(slot, input_channel, channel, settings, api)
            else:
                configured_bridge = self._prepare_sensor(input_channel, channel, settings, api)
            api.status({
                **self._bridge_status_fields(configured_bridge),
                "Excitation Safety": (
                    (
                        f"Autorange active on R{slot} / S{input_channel}; "
                        "minimum excitation will be restored on "
                        "Run end/Stop/Error/Disable"
                    )
                    if is_autorange and self.sequence_active
                    else (
                        "Measurement excitation active on "
                        f"R{slot} / sensor {input_channel}"
                    )
                ),
                "Last Slot / Sensor": (
                    f"R{slot} / S{input_channel}"
                ),
            })

            # 在读取数值前后都核对当前传感器和设置。LR-700 未被 LocalLockout，
            # 操作者仍可使用前面板；两次读回可避免把中途改变后的值写入错误通道。
            # 自动量程模式下把等待拆为 1/4 与结束两次 GET 6 核对：不一致则直接
            # 重等一整轮；若进入 dwell 或读数瞬间又发生换挡，同样重等并刷新温场快照。
            read_attempts = self._slot_measure_attempts(channel, settings)
            temperature: float | None = None
            field: float | None = None
            resistance = 0.0
            reactance = 0.0
            overload_bits = 0
            data_failure: ModuleError | None = None
            settled_bridge: dict[str, int] | None = None
            for _read_attempt in range(1, read_attempts + 1):
                settle_seconds = float(settings["switch_settle_seconds"])
                settle_bridge: dict[str, int] | None = None
                if is_autorange:
                    first_quarter = settle_seconds / 4.0
                    self._waiter(api, first_quarter)
                    mid = self._query_bridge_settings(api)
                    self._verify_channel_readback(
                        input_channel,
                        channel,
                        mid,
                    )
                    self._waiter(api, settle_seconds - first_quarter)
                    end_settle = self._query_bridge_settings(api)
                    self._verify_channel_readback(
                        input_channel,
                        channel,
                        end_settle,
                    )
                    if not self._same_bridge_range_state(mid, end_settle):
                        continue
                    settle_bridge = end_settle
                else:
                    self._waiter(api, settle_seconds)

                first = api.instruments()
                self._waiter(
                    api,
                    float(settings["dwell_seconds"]),
                )
                second = api.instruments()
                temperature, field = (
                    self._averaged_system_values(
                        first,
                        second,
                    )
                )

                before = self._query_bridge_settings(api)
                self._verify_channel_readback(
                    input_channel,
                    channel,
                    before,
                )
                if (
                    is_autorange
                    and settle_bridge is not None
                    and not self._same_bridge_range_state(
                        settle_bridge,
                        before,
                    )
                ):
                    continue
                range_index = int(before["range_index"])
                data_failure = None
                try:
                    resistance = self._query_measurement(
                        instrument.GET_RESISTANCE,
                        "R",
                        range_index,
                        api,
                    )
                    reactance = self._query_measurement(
                        instrument.GET_REACTANCE,
                        "X",
                        range_index,
                        api,
                    )
                    overload_bits = self._query_overloads(api)
                except ModuleError as exc:
                    # 仅把已经收到但无法解析的 R/X/OVERLOAD 读数视为数据问题。GET 6
                    # 设置读回、通信耗尽和安全状态失败仍是系统问题，必须终止 SEQ。
                    if exc.code != "LR700_INVALID_REPLY":
                        raise
                    data_failure = exc
                after = self._query_bridge_settings(api)
                self._verify_channel_readback(
                    input_channel,
                    channel,
                    after,
                )
                if not is_autorange or self._same_bridge_range_state(
                    before,
                    after,
                ):
                    settled_bridge = after
                    break
            if settled_bridge is None:
                raise ModuleWarning(
                    "LR-700 autorange range or excitation kept changing "
                    f"during measurement on sensor {input_channel}; "
                    "increase Switch / filter settle time or narrow "
                    "autorange limits",
                    "LR700_AUTORANGE_UNSTABLE",
                    f"sensor {input_channel}",
                )

            bridge_status = self._bridge_status_fields(settled_bridge)

            warning_context = (
                f"R{slot} / sensor {input_channel}"
            )
            if data_failure is not None:
                api.warn("LR700_READING_WARNING", None, warning_context)
                api.warn(
                    "LR700_READING_INVALID",
                    f"LR-700 R{slot} / sensor "
                    f"{input_channel} returned an invalid "
                    "measurement; this channel was recorded "
                    f"as ERROR: {data_failure}",
                    warning_context,
                )
                row: dict[str, Any] = {
                    "StatusCode": (
                        STATUS_CODE_INVALID_READING
                    ),
                }
                if temperature is not None:
                    row["TemperatureAverage"] = temperature
                if field is not None:
                    row["FieldAverage"] = field
                self.last_values = {
                    "slot": slot,
                    "input_channel": input_channel,
                    "status": "ERROR",
                }
                api.status({
                    **bridge_status,
                    "Last Slot / Sensor": (
                        f"R{slot} / S{input_channel}"
                    ),
                    "Last Status": (
                        f"ERROR: {data_failure}"
                    ),
                })
                return row

            api.warn("LR700_READING_INVALID", None, warning_context)
            status, details = self._status(
                overload_bits
            )
            self._publish_reading_warning(
                slot,
                input_channel,
                status,
                details,
                api,
            )
            status_code = {
                "NORMAL": STATUS_CODE_NORMAL,
                "OVER_RANGE": STATUS_CODE_OVER_RANGE,
                "OVERLOAD": STATUS_CODE_OVERLOAD,
            }[status]
            row: dict[str, Any] = {
                "StatusCode": status_code,
            }
            if temperature is not None:
                row["TemperatureAverage"] = temperature
            if field is not None:
                row["FieldAverage"] = field
            # OVER_RANGE/OVERLOAD 下桥读数不可信；保持当前 Rn/Xn 以及所有未测槽位
            # 为空，状态码和温场快照足以说明这次测量尝试。
            if status_code == STATUS_CODE_NORMAL:
                if is_autorange:
                    self._autorange_last_range[input_channel] = int(
                        settled_bridge["range_index"]
                    )
                row.update({
                    f"R{slot}": resistance,
                    f"X{slot}": reactance,
                })
            self.last_values = {
                "slot": slot,
                "input_channel": input_channel,
                "status": status,
            }
            if status_code == STATUS_CODE_NORMAL:
                self.last_values.update({
                    "resistance": resistance,
                    "reactance": reactance,
                })
            api.status({
                **bridge_status,
                "Last Slot / Sensor": (
                    f"R{slot} / S{input_channel}"
                ),
                "Last Resistance (Ohm)": (
                    resistance
                    if status_code == STATUS_CODE_NORMAL
                    else "-"
                ),
                "Last Reactance (Ohm)": (
                    reactance
                    if status_code == STATUS_CODE_NORMAL
                    else "-"
                ),
                "Last Status": (
                    status
                    if not details
                    else f"{status}: {details}"
                ),
            })
            return row
        except BaseException as exc:
            failure = exc
            raise
        finally:
            unstable_warning_only = (
                isinstance(failure, ModuleWarning)
                and failure.code == "LR700_AUTORANGE_UNSTABLE"
            )
            keep_autorange_running = (
                (failure is None or unstable_warning_only)
                and self.sequence_active
                and (
                    (is_autorange and self._autorange_running)
                    or (not is_autorange and ("common" in settings or bool(self._x10_channels)))
                )
            )
            if not keep_autorange_running:
                # Stop 会让普通 api checkpoint 立即取消，因此清理路径直接使用带
                # VISA timeout 的底层 transport，不依赖协作上下文。
                reset_x10 = bool(self._x10_channels or self._measurement_mode)
                cleanup_error = (
                    self._best_effort_safe_state(api, reset_mode=True)
                    if reset_x10
                    else self._best_effort_safe_state(api)
                )
                extra_status = {"10x References": "Inactive"} if reset_x10 else {}
                if cleanup_error is None and self._last_safe_bridge is not None:
                    api.status({
                        **self._bridge_status_fields(self._last_safe_bridge),
                        **extra_status,
                        "Excitation Safety": self._safe_state_text(
                            self._last_safe_bridge
                        ),
                    })
                else:
                    api.status({
                        **self._cleared_bridge_status_fields("Unknown"),
                        **extra_status,
                        "Excitation Safety": "Minimum excitation unconfirmed",
                    })
                if cleanup_error is not None:
                    raise ModuleError(
                        "Could not confirm LR-700 minimum "
                        f"excitation after sensor "
                        f"{input_channel}: "
                        f"{cleanup_error}",
                        "LR700_SAFE_STATE_FAILED",
                        f"sensor {input_channel}",
                    ) from failure

    def _run_end(
        self,
        reason: str,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """completed/stopped/error 都直接确认最低激励，模块仍保持 Enabled。"""

        if not self.applied_settings and self.transport is None:
            self.sequence_active = False
            status = {
                "Sequence": reason.title(),
                "10x References": "Inactive",
                "Excitation Safety": "No instrument connection was opened or changed",
            }
            api.status(status)
            return status
        error = self._best_effort_safe_state(api, reset_mode=True)
        self.sequence_active = False
        if error is not None:
            api.status({
                "Sequence": reason.title(),
                "10x References": "Inactive",
                **self._cleared_bridge_status_fields("Unknown"),
                "Excitation Safety": (
                    "Minimum excitation unconfirmed"
                ),
            })
            raise ModuleError(
                "Could not confirm LR-700 minimum "
                f"excitation at end of sequence: {error}",
                "LR700_SAFE_STATE_FAILED",
            )
        status = {
            "Sequence": reason.title(),
            "10x References": "Inactive",
            "Excitation Safety": self._safe_state_text(self._last_safe_bridge),
            **(
                self._bridge_status_fields(self._last_safe_bridge)
                if self._last_safe_bridge is not None
                else {}
            ),
        }
        api.status(status)
        return status

    def close(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """Disable/退出时先尽力确认最低激励，再无条件释放 VISA 句柄。"""

        had_applied_connection = bool(
            self.applied_settings
        )
        error = (
            self._best_effort_safe_state(api, reset_mode=True)
            if had_applied_connection
            else None
        )
        self._close_transport()
        self.sequence_active = False
        self.applied_settings = {}
        self._x10_channels.clear()
        self._measurement_mode = 0
        self._autorange_last_range.clear()
        status = {
            "Connection": "Disconnected",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "10x References": "Inactive",
            **self._cleared_bridge_status_fields("-"),
            "Excitation Safety": (
                (
                    "Minimum excitation was confirmed "
                    "before disconnect"
                    if error is None
                    else "Minimum excitation unconfirmed"
                )
                if had_applied_connection
                else (
                    "No instrument connection was opened "
                    "or changed"
                )
            ),
        }
        api.status(status)
        if error is not None:
            raise ModuleError(
                "LR-700 transport was released, but minimum "
                f"excitation could not be confirmed: {error}",
                "LR700_SAFE_STATE_FAILED",
            )
        return status

    def _read_status(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """只读 GET 6/GET 7；不会隐式 Apply、切换通道或改变激励。"""

        if self.transport is None:
            return {
                "Connection": "Disconnected",
                "Resource": (
                    self.desired_settings["resource"]
                    or "Not selected"
                ),
                "Protocol": "Not queried",
                **self._cleared_bridge_status_fields("-"),
                "Excitation Safety": (
                    "Instrument state unknown"
                ),
            }
        bridge = self._query_bridge_settings(api)
        bits = self._query_overloads(api)
        status, details = self._status(bits)
        result = {
            "Connection": "Connected",
            "Resource": (
                self.applied_settings
                or self.desired_settings
            )["resource"],
            "Protocol": self.protocol_signature,
            **self._bridge_status_fields(bridge),
            "Last Status": (
                status
                if not details
                else f"{status}: {details}"
            ),
        }
        api.status(result)
        return result

    def _action(
        self,
        action: str,
        payload: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """处理只读连接测试；不会保存或 Apply 设置。"""

        if action == "test_connection":
            candidate = payload.get(
                "settings",
                self.desired_settings,
            )
            if not isinstance(candidate, Mapping):
                raise ModuleError(
                    "Test Connection settings payload is "
                    "invalid",
                    "LR700_INVALID_SETTINGS",
                    "settings",
                )
            desired = self._normalized_settings(
                candidate,
                require_resource=True,
                operation_timeout_seconds=(
                    api.timeout
                ),
            )
            transport: instrument.Transport | None = None
            resource_id = str(desired["resource"])
            resource = api.resource_address(resource_id)
            try:
                transport = self._transport_factory(
                    resource,
                    float(
                        desired[
                            "io_timeout_seconds"
                        ]
                    ),
                )
                raw_settings = str(
                    transport.query(instrument.GET_SETTINGS)
                ).strip()
                bridge = self._parse_bridge_settings(
                    raw_settings,
                )
                raw_overloads = str(
                    transport.query(instrument.GET_OVERLOADS)
                ).strip()
                self._parse_overloads(raw_overloads)
            except Exception as exc:
                raise ModuleError(
                    "LR-700 protocol test failed for "
                    f"resource {resource_id!r}: "
                    f"{type(exc).__name__}: {exc}",
                    "LR700_CONNECTION_FAILED",
                    resource_id,
                ) from exc
            finally:
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass
            status = {
                "Last Action": (
                    "Connection test passed; no settings "
                    "were written"
                ),
                "Protocol": self._protocol_text(bridge),
            }
            api.status(status)
            return status

        raise ModuleWarning(
            f"Unsupported LR-700 action: {action}",
            "LR700_UNSUPPORTED_ACTION",
            action,
        )

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        if event == "run_start":
            return self._run_start(api)
        if event == "run_end":
            return self._run_end(str(data.get("reason", "error")), api)
        if event == "status":
            return self._read_status(api)
        if event == "action":
            payload = data.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ModuleError(
                    "Action payload must be a mapping",
                    "LR700_INVALID_ACTION",
                )
            return self._action(str(data.get("name", "")), payload, api)
        return {}

    def _connect(
        self,
        resource_id: str,
        timeout_seconds: float,
        api: ModuleAPI,
    ) -> None:
        """打开 VISA 并用只读 GET 6 验证 LR-700 协议结构。

        老式 LR-700 不提供 ``*IDN?``。相比仅测试“能读到任意字符串”，严格解析
        ``#R,#E,###%,#F,#M,#L,##S`` 是目前可用的协议身份门槛。
        """

        self._close_transport()
        resource = api.resource_address(resource_id)
        transport: instrument.Transport | None = None
        try:
            transport = self._transport_factory(
                resource,
                timeout_seconds,
            )
            raw = str(
                transport.query(instrument.GET_SETTINGS)
            ).strip()
            bridge = self._parse_bridge_settings(raw)
        except Exception as exc:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            raise ModuleError(
                f"Could not verify LR-700 resource {resource_id!r}: "
                f"{type(exc).__name__}: {exc}",
                "LR700_CONNECTION_FAILED",
                resource_id,
            ) from exc
        self.transport = transport
        self.protocol_signature = (
            self._protocol_text(bridge)
        )

    def _write_absolute(
        self,
        command: str,
        api: ModuleAPI,
    ) -> None:
        """发送一次绝对设置命令并同步回读响应；异常后不自动重放。

        底层 ``LR700VisaTransport.write()`` 会在写入后按手册 p. 3-50 回读至 EOI：
        若仪表明确返回 ``ERROR``，直接报告 ``LR700_COMMAND_REJECTED``；若发生 GPIB
        读写异常或非预期响应，无法判断仪表是否已经执行命令，因此先关闭 session、报告
        ``LR700_WRITE_UNCERTAIN``，再由 finally 使用独立的最低激励恢复路径处理安全状态。
        """

        api.sleep(0)
        transport = self.transport
        if transport is None:
            raise ModuleError(
                "LR-700 transport is disconnected",
                "LR700_COMMUNICATION_FAILED",
                command,
            )
        try:
            transport.write(command)
        except instrument.LR700CommandRejectedError as exc:
            raise ModuleError(
                f"LR-700 rejected command {command}",
                "LR700_COMMAND_REJECTED",
                command,
            ) from exc
        except Exception as exc:
            self._close_transport()
            raise ModuleError(
                "LR-700 write result is uncertain; command "
                f"was not replayed: {command}: "
                f"{type(exc).__name__}: {exc}",
                "LR700_WRITE_UNCERTAIN",
                command,
            ) from exc

    def _query_text(
        self,
        command: str,
        api: ModuleAPI,
    ) -> str:
        """对只读 GET 命令做有限重连重试，并拒绝空串和仪表 ``ERROR``。"""

        settings = (
            self.applied_settings
            or self.desired_settings
        )
        attempts = int(
            settings.get("retry_attempts", 1)
        )
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            api.sleep(0)
            transport = self.transport
            if transport is None:
                if attempt == 1:
                    self._wait_reconnect_delay(api, cancellable=True)
                try:
                    self._reopen_transport(settings, api)
                    transport = self.transport
                except Exception as exc:
                    last_error = exc
            if transport is not None:
                try:
                    reply = str(
                        transport.query(command)
                    ).strip()
                except Exception as exc:
                    last_error = exc
                else:
                    if not reply:
                        last_error = ValueError(
                            "empty response"
                        )
                    elif reply.casefold() == "error":
                        api.warn("LR700_IO_RETRY", None, command)
                        raise ModuleError(
                            "LR-700 rejected the previous "
                            f"command while handling {command}",
                            "LR700_COMMAND_REJECTED",
                            command,
                        )
                    else:
                        api.warn("LR700_IO_RETRY", None, command)
                        return reply
            if attempt < attempts:
                api.warn(
                    "LR700_IO_RETRY",
                    "LR-700 read failed; reopening and "
                    f"retrying ({attempt}/{attempts}): "
                    f"{type(last_error).__name__}: "
                    f"{last_error}",
                    command,
                )
                self._close_transport()
                self._wait_reconnect_delay(api, cancellable=True)
        api.warn("LR700_IO_RETRY", None, command)
        assert last_error is not None
        raise ModuleError(
            f"LR-700 read failed after {attempts} "
            f"attempt(s) for {command}: "
            f"{type(last_error).__name__}: {last_error}",
            "LR700_COMMUNICATION_FAILED",
            command,
        ) from last_error

    def _wait_reconnect_delay(
        self,
        api: ModuleAPI,
        *,
        cancellable: bool,
    ) -> None:
        """异常关闭会话后等待一段冷却时间再重新打开 GPIB 连接。"""

        delay = float(instrument.LR700_RECONNECT_DELAY_SECONDS)
        if delay <= 0:
            return
        if cancellable:
            self._waiter(api, delay)
            return
        if self._custom_waiter:
            try:
                self._waiter(api, delay)
            except InterruptedError:
                pass
            return
        time.sleep(delay)

    def _reopen_transport(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        """重建同一 GPIB session，并只读验证 GET 6 协议结构。"""

        resource = api.resource_address(
            str(settings["resource"])
        )
        timeout = float(
            settings["io_timeout_seconds"]
        )
        self._close_transport()
        transport = self._transport_factory(
            resource,
            timeout,
        )
        try:
            raw = str(
                transport.query(instrument.GET_SETTINGS)
            ).strip()
            bridge = self._parse_bridge_settings(raw)
        except Exception:
            transport.close()
            raise
        self.transport = transport
        self.protocol_signature = (
            self._protocol_text(bridge)
        )

    def _reset_autorange_session(self) -> None:
        self._autorange_running = False
        self._autorange_active_limits = None
        self._autorange_active_filter = None

    @staticmethod
    def _require_safe_range_transition(
        current_range_index: int | None,
        target_range: int,
        current_limit_a: float,
    ) -> None:
        """拒绝任何在 5% 安全态换挡中间步骤会超过 current_limit_a 的量程切换。"""

        if current_range_index is None:
            return
        current = int(current_range_index)
        target = int(target_range)
        if current == target:
            return
        peak = instrument.range_transition_peak_current_a(target, current)
        if peak > current_limit_a * (1.0 + 1.0e-12):
            raise ModuleError(
                f"Switching LR-700 from {range_label(current)} to {range_label(target)} "
                f"reaches an unavoidable hardware intermediate current of {format_current(peak)} "
                f"(at 5% minimum excitation), exceeding the configured current limit "
                f"{format_current(current_limit_a)}. Raise Maximum excitation current to at least "
                f"{format_current(peak)} or switch the bridge range before connecting sensitive samples.",
                "LR700_TRANSITION_OVER_LIMIT",
                "common.current_limit_a",
            )

    def _start_autorange_session(
        self,
        input_channel: int,
        channel: Mapping[str, Any],
        api: ModuleAPI,
        *,
        current_range_index: int | None = None,
        target_range_index: int | None = None,
    ) -> dict[str, int]:
        """在 5% 安全态下切至目标传感器与量程、同步自定义 trace 激励并开启 AUTORANGE 1。"""

        if "common" in self.applied_settings and current_range_index is not None:
            min_range = int(channel["autorange_min_range"])
            max_range = int(channel["autorange_max_range"])
            if target_range_index is not None and min_range <= int(target_range_index) <= max_range:
                resolved_target = int(target_range_index)
            else:
                resolved_target = max(min_range, min(max_range, int(current_range_index)))
            self._require_safe_range_transition(
                current_range_index,
                resolved_target,
                float(self.applied_settings["common"]["current_limit_a"]),
            )
        commands = instrument.autorange_start_commands(
            input_channel,
            channel,
            current_range_index=current_range_index,
            target_range_index=target_range_index,
            measurement_mode=self._measurement_mode,
        )
        for command in commands:
            self._write_absolute(command, api)
        self._autorange_running = True
        bridge = self._query_bridge_settings(api)
        self._verify_channel_readback(
            input_channel,
            channel,
            bridge,
        )
        self._autorange_active_limits = (
            int(channel["autorange_min_range"]),
            int(channel["autorange_max_range"]),
        )
        self._autorange_active_filter = int(channel["filter_index"])
        return bridge

    def _switch_autorange_sensor(
        self,
        input_channel: int,
        channel: Mapping[str, Any],
        api: ModuleAPI,
    ) -> dict[str, int]:
        """连续自动量程期间切换通道：同量程、激励符合 trace 表且在边界内仅切通道，否则先降 5% 再切目标量程与对应激励。"""

        min_range = int(channel["autorange_min_range"])
        max_range = int(channel["autorange_max_range"])
        new_limits = (min_range, max_range)
        current = self._query_bridge_settings(api)
        current_range = int(current["range_index"])
        current_exc = int(current["excitation_index"])
        cached_range = self._autorange_last_range.get(input_channel)
        in_new_bounds = min_range <= current_range <= max_range
        same_as_cached = cached_range is None or cached_range == current_range
        exc_matches_trace = (
            current_exc == channel.get("autorange_trace", tuple(autorange_trace_excitation_index(r) for r in range(10)))[current_range]
        )

        if not in_new_bounds or not same_as_cached or not exc_matches_trace:
            safe_bridge = self._set_safe_state(api)
            return self._start_autorange_session(
                input_channel,
                channel,
                api,
                current_range_index=int(safe_bridge["range_index"]),
                target_range_index=cached_range,
            )

        commands = instrument.autorange_switch_commands(
            input_channel,
            channel,
            active_limits=self._autorange_active_limits,
            active_filter=self._autorange_active_filter,
        )
        for command in commands:
            self._write_absolute(command, api)
        bridge = self._query_bridge_settings(api)
        self._verify_channel_readback(
            input_channel,
            channel,
            bridge,
        )
        self._autorange_active_limits = new_limits
        self._autorange_active_filter = int(channel["filter_index"])
        return bridge

    def _configure_sensor(
        self,
        input_channel: int,
        channel: Mapping[str, Any],
        api: ModuleAPI,
        *,
        current_range_index: int | None = None,
    ) -> dict[str, int]:
        """发送一套完整的当前传感器绝对设置，并用 GET 6 逐字段读回。"""

        if (
            "common" in self.applied_settings
            and current_range_index is not None
            and str(channel.get("range_mode", "fixed")).strip().casefold() == "fixed"
        ):
            self._require_safe_range_transition(
                current_range_index,
                int(channel["range_index"]),
                float(self.applied_settings["common"]["current_limit_a"]),
            )
        commands = instrument.configuration_commands(
            input_channel,
            channel,
            current_range_index=current_range_index,
            measurement_mode=self._measurement_mode,
        )
        for command in commands:
            self._write_absolute(
                command,
                api,
            )
        bridge = self._query_bridge_settings(api)
        self._verify_channel_readback(
            input_channel,
            channel,
            bridge,
        )
        return bridge

    def _set_safe_state(
        self,
        api: ModuleAPI,
        *,
        reset_mode: bool = False,
    ) -> dict[str, int]:
        """通过普通受控 I/O 按当前电阻量程设置并读回最低激励（× 5%）。"""

        self._reset_autorange_session()
        current = self._query_bridge_settings(api)
        current_range = int(current["range_index"])
        for command in instrument.safe_state_commands(current_range):
            self._write_absolute(command, api)
        bridge = self._query_bridge_settings(api)
        # 若退出自动量程瞬间量程刚好变化，按最新量程重发一次绝对安全命令并再次回读确认。
        if int(bridge["range_index"]) != current_range:
            for command in instrument.safe_state_commands(
                int(bridge["range_index"])
            ):
                self._write_absolute(command, api)
            bridge = self._query_bridge_settings(api)
        self._verify_safe_state(bridge)
        if reset_mode:
            if bridge["mode"] != 0:
                self._write_absolute(instrument.mode_command(0), api)
                bridge = self._query_bridge_settings(api)
                if bridge["mode"] != 0:
                    raise ModuleError(
                        "LR700 did not confirm 1x mode",
                        "LR700_SETTINGS_VERIFY_FAILED",
                    )
            self._measurement_mode = 0
            self._x10_channels.clear()
        self._last_safe_bridge = dict(bridge)
        api.status({
            **self._bridge_status_fields(bridge),
            **({"10x References": "Inactive"} if reset_mode else {}),
            "Excitation Safety": self._safe_state_text(bridge),
        })
        return bridge

    def _best_effort_safe_state(
        self,
        api: ModuleAPI,
        *,
        reset_mode: bool = False,
    ) -> str | None:
        """在取消/异常路径直接按当前电阻量程设置最低激励，返回未确认原因而不吞掉原异常。

        先使用当前 session；失败后关闭并有界地重开一次，再重发绝对安全命令。成功
        后保持连接，供仍 Enabled 的模块继续使用。此方法不无限重试，也不把进程回收
        等同于仪表安全。
        """

        self._reset_autorange_session()
        settings = (
            self.applied_settings
            or self.desired_settings
        )
        if not settings.get("resource"):
            return "no instrument resource is configured"
        try:
            resource = api.resource_address(
                str(settings["resource"])
            )
        except Exception as exc:
            return (
                "instrument resource could not be resolved: "
                f"{type(exc).__name__}: {exc}"
            )
        last_error: Exception | None = None
        for attempt in range(2):
            transport = self.transport
            initial: dict[str, int] | None = None
            if transport is None:
                self._wait_reconnect_delay(api, cancellable=False)
                try:
                    transport = self._transport_factory(
                        resource,
                        float(
                            settings[
                                "io_timeout_seconds"
                            ]
                        ),
                    )
                    initial = self._parse_bridge_settings(
                        str(
                            transport.query(instrument.GET_SETTINGS)
                        ).strip()
                    )
                    self.transport = transport
                    self.protocol_signature = (
                        self._protocol_text(initial)
                    )
                except Exception as exc:
                    last_error = exc
                    if transport is not None:
                        try:
                            transport.close()
                        except Exception:
                            pass
                    self.transport = None
                    continue
            try:
                if initial is None:
                    initial = self._parse_bridge_settings(
                        str(
                            transport.query(instrument.GET_SETTINGS)
                        ).strip()
                    )
                current_range = int(initial["range_index"])
                for command in instrument.safe_state_commands(current_range):
                    transport.write(command)
                bridge = (
                    self._parse_bridge_settings(
                        str(
                            transport.query(instrument.GET_SETTINGS)
                        ).strip()
                    )
                )
                if int(bridge["range_index"]) != current_range:
                    for command in instrument.safe_state_commands(
                        int(bridge["range_index"])
                    ):
                        transport.write(command)
                    bridge = self._parse_bridge_settings(
                        str(
                            transport.query(instrument.GET_SETTINGS)
                        ).strip()
                    )
                self._verify_safe_state(bridge)
                if reset_mode:
                    if bridge["mode"] != 0:
                        transport.write(instrument.mode_command(0))
                        bridge = self._parse_bridge_settings(str(transport.query(instrument.GET_SETTINGS)).strip())
                        if bridge["mode"] != 0:
                            raise ModuleError("LR700 did not confirm 1x during cleanup", "LR700_SETTINGS_VERIFY_FAILED")
                    self._measurement_mode = 0
                    self._x10_channels.clear()
                self._last_safe_bridge = dict(bridge)
                return None
            except Exception as exc:
                last_error = exc
                # 第一次失败后丢弃可能损坏的 session；第二次失败直接报告，避免
                # 安全清理无限阻塞 worker 退出。
                if attempt == 0:
                    self._close_transport()
        assert last_error is not None
        return (
            f"{type(last_error).__name__}: "
            f"{last_error}"
        )

    @staticmethod
    def _bridge_status_fields(
        bridge: Mapping[str, int],
    ) -> dict[str, Any]:
        range_index = int(bridge["range_index"])
        excitation_index = int(bridge["excitation_index"])
        percent = int(bridge["excitation_percent"])
        sensor = int(bridge["sensor"])
        current_text = format_current(
            excitation_current_a(range_index, excitation_index, percent)
        )
        return {
            "Current Sensor": (
                f"S{sensor}" if sensor else "Direct / sensor 0"
            ),
            "Current Range Index": (
                f"{range_index} ({range_label(range_index)})"
            ),
            "Current Excitation Index": (
                f"{excitation_index} ({excitation_label(excitation_index)})"
            ),
            "Current Excitation (%)": percent,
            "Current Excitation Current": current_text,
            "Current Filter Index": int(bridge["filter_index"]),
            "Current Difference Mode": "10x" if bridge["mode"] else "1x",
        }

    @staticmethod
    def _cleared_bridge_status_fields(
        placeholder: str = "-",
    ) -> dict[str, Any]:
        return {
            "Current Sensor": placeholder,
            "Current Range Index": placeholder,
            "Current Excitation Index": placeholder,
            "Current Excitation (%)": placeholder,
            "Current Excitation Current": placeholder,
            "Current Filter Index": placeholder,
            "Current Difference Mode": placeholder,
        }

    @staticmethod
    def _safe_state_text(
        bridge: Mapping[str, int] | None = None,
    ) -> str:
        if bridge is not None:
            range_index = int(bridge["range_index"])
            excitation_index = int(bridge["excitation_index"])
            percent = int(bridge["excitation_percent"])
            current = format_current(
                excitation_current_a(range_index, excitation_index, percent)
            )
            return (
                f"Minimum confirmed for {range_label(range_index)}: "
                f"{excitation_label(excitation_index)} x {percent}% "
                f"({current}; LR-700 has no Off command)"
            )
        return (
            "Minimum confirmed for current resistance range x 5% "
            "(LR-700 has no Off command)"
        )

    def _query_bridge_settings(
        self,
        api: ModuleAPI,
    ) -> dict[str, int]:
        bridge = self._parse_bridge_settings(self._query_text(instrument.GET_SETTINGS, api))
        return bridge

    @classmethod
    def _parse_bridge_settings(
        cls,
        reply: str,
    ) -> dict[str, int]:
        """解析 ``9R,6E,100%,0F,0M,0L,00S`` 形式的 GET 6 响应。"""
        try:
            return instrument.parse_bridge_settings(reply)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                "LR-700 GET 6 response does not match "
                f"the documented protocol: {reply!r}",
                "LR700_INVALID_REPLY",
                instrument.GET_SETTINGS,
            ) from exc

    @staticmethod
    def _protocol_text(
        bridge: Mapping[str, int],
    ) -> str:
        return (
            "LR-700-compatible GET 6; "
            f"sensor {bridge['sensor']}"
        )

    @staticmethod
    def _same_bridge_range_state(
        left: Mapping[str, int],
        right: Mapping[str, int],
    ) -> bool:
        return (
            int(left["range_index"]) == int(right["range_index"])
            and int(left["excitation_index"]) == int(right["excitation_index"])
            and int(left["excitation_percent"])
            == int(right["excitation_percent"])
        )

    def _verify_channel_readback(
        self,
        input_channel: int,
        channel: Mapping[str, Any],
        bridge: Mapping[str, int],
        *,
        check_autorange_bounds: bool = True,
    ) -> None:
        range_mode = (
            str(channel.get("range_mode", "fixed")).strip().casefold()
        )
        if range_mode == "autorange":
            expected_common = {
                "filter_index": int(channel["filter_index"]),
                "excitation_percent": 100,
                "mode": self._measurement_mode,
                "sensor": input_channel,
            }
            actual_common = {
                key: int(bridge[key])
                for key in expected_common
            }
            actual_range = int(bridge["range_index"])
            actual_excitation = int(bridge["excitation_index"])
            min_range = int(channel["autorange_min_range"])
            max_range = int(channel["autorange_max_range"])
            out_of_bounds = (
                check_autorange_bounds
                and not min_range <= actual_range <= max_range
            )
            if (
                actual_common != expected_common
                or out_of_bounds
                or not is_valid_range_excitation(
                    actual_range,
                    actual_excitation,
                )
            ):
                raise ModuleError(
                    "LR-700 autorange readback mismatch for "
                    f"sensor {input_channel}: expected "
                    f"{expected_common} with range in "
                    f"[{min_range}, {max_range}], read back "
                    f"{dict(bridge)}",
                    "LR700_SETTINGS_VERIFY_FAILED",
                    f"sensor {input_channel}",
                )
            return

        expected = {
            "range_index": int(
                channel["range_index"]
            ),
            "excitation_index": int(
                channel["excitation_index"]
            ),
            "excitation_percent": int(
                channel["excitation_percent"]
            ),
            "filter_index": int(
                channel["filter_index"]
            ),
            "mode": self._measurement_mode,
            "sensor": input_channel,
        }
        actual = {
            key: int(bridge[key])
            for key in expected
        }
        if actual != expected:
            raise ModuleError(
                "LR-700 settings readback mismatch for "
                f"sensor {input_channel}: expected {expected}, "
                f"read back {actual}",
                "LR700_SETTINGS_VERIFY_FAILED",
                f"sensor {input_channel}",
            )

    @staticmethod
    def _verify_safe_state(
        bridge: Mapping[str, int],
    ) -> None:
        range_index = int(bridge["range_index"])
        expected_excitation = minimum_excitation_index(range_index)
        actual = (
            int(bridge["excitation_index"]),
            int(bridge["excitation_percent"]),
        )
        expected = (
            expected_excitation,
            SAFE_EXCITATION_PERCENT,
        )
        if actual != expected:
            raise ModuleError(
                "LR-700 minimum excitation readback "
                f"mismatch on range {range_index} ({range_label(range_index)}): "
                f"expected {expected}, read back {actual}",
                "LR700_SAFE_STATE_FAILED",
            )

    def _query_measurement(
        self,
        command: str,
        expected_parameter: str,
        range_index: int,
        api: ModuleAPI,
    ) -> float:
        return self._parse_measurement(
            self._query_text(command, api),
            expected_parameter,
            range_index,
            command,
        )

    @classmethod
    def _parse_measurement(
        cls,
        reply: str,
        expected_parameter: str,
        range_index: int,
        command: str,
    ) -> float:
        """把 LR-700 显示值和 K/M/U 前缀转换为 Ohm。

        手册用同一个大写 ``M`` 表示毫欧或兆欧显示。量程 0-2 时解释为 milli，
        量程 9 时解释为 mega；中间量程若出现 M 属于协议歧义并拒绝写入数据。
        """

        try:
            return instrument.parse_measurement(
                reply,
                expected_parameter,
                range_index,
            )
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                "LR-700 measurement response does not "
                f"match the documented format for {command}: {reply!r}",
                "LR700_INVALID_REPLY",
                command,
            ) from exc

    def _query_overloads(
        self,
        api: ModuleAPI,
    ) -> int:
        return self._parse_overloads(
            self._query_text(instrument.GET_OVERLOADS, api)
        )

    @classmethod
    def _parse_overloads(cls, reply: str) -> int:
        try:
            return instrument.parse_overloads(reply)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                "LR-700 GET 7 response does not match "
                f"'### OVERLOADS': {reply!r}",
                "LR700_INVALID_REPLY",
                instrument.GET_OVERLOADS,
            ) from exc

    @staticmethod
    def _status(
        bits: int,
    ) -> tuple[str, str]:
        details = ", ".join(
            name
            for bit, name in OVERLOAD_BITS
            if bits & bit
        )
        if bits == 0:
            return "NORMAL", ""
        if bits & COMMON_MODE_OVERLOAD_MASK:
            details = f"{details}; {COMMON_MODE_OVERLOAD_HINT}"
        # 电压/放大器饱和是 overload；其余标志是显示/测量 overrange。
        if bits & (4 | 8 | 16 | 32):
            return "OVERLOAD", details
        return "OVER_RANGE", details

    @staticmethod
    def _publish_reading_warning(
        slot: int,
        input_channel: int,
        status: str,
        details: str,
        api: ModuleAPI,
    ) -> None:
        warning_context = (
            f"R{slot} / sensor {input_channel}"
        )
        if status == "NORMAL":
            api.warn("LR700_READING_WARNING", None, warning_context)
            return
        api.warn(
            "LR700_READING_WARNING",
            f"LR-700 R{slot} / sensor "
            f"{input_channel}: {status}"
            + (f" ({details})" if details else ""),
            warning_context,
        )

    @classmethod
    def _averaged_system_values(
        cls,
        first: Mapping[str, Mapping[str, Any]],
        second: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float | None, float | None]:
        """计算 dwell 前后主温度(K)和主磁场(Oe)的算术平均；未配置的类型返回 None。"""

        temperature: float | None = None
        first_temperature = cls._optional_primary_snapshot(
            first,
            "temperature",
        )
        if first_temperature is not None:
            second_temperature = cls._same_snapshot(
                second,
                first_temperature[0],
                "temperature",
            )
            cls._require_newer_snapshot(
                first_temperature,
                second_temperature,
            )
            temperature = (
                cls._temperature_kelvin(
                    first_temperature[1],
                    first_temperature[2],
                )
                + cls._temperature_kelvin(
                    second_temperature[1],
                    second_temperature[2],
                )
            ) / 2.0

        field: float | None = None
        first_field = cls._optional_primary_snapshot(
            first,
            "field",
        )
        if first_field is not None:
            second_field = cls._same_snapshot(
                second,
                first_field[0],
                "field",
            )
            cls._require_newer_snapshot(
                first_field,
                second_field,
            )
            field = (
                cls._field_oersted(
                    first_field[1],
                    first_field[2],
                )
                + cls._field_oersted(
                    second_field[1],
                    second_field[2],
                )
            ) / 2.0
        return temperature, field

    @classmethod
    def _optional_primary_snapshot(
        cls,
        system: Mapping[str, Mapping[str, Any]],
        kind: str,
    ) -> tuple[str, float, str, float] | None:
        if not any(
            str(values.get("kind", "")).casefold() == kind
            for values in system.values()
        ):
            return None
        return cls._primary_snapshot(system, kind)

    @staticmethod
    def _primary_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        kind: str,
    ) -> tuple[str, float, str, float]:
        candidates = [
            (str(instrument_id), values)
            for instrument_id, values in system.items()
            if str(
                values.get("kind", "")
            ).casefold() == kind
        ]
        candidates.sort(
            key=lambda item: (
                0
                if str(
                    item[1].get("role", "")
                ).casefold() == "primary"
                else (
                    1
                    if bool(
                        item[1].get(
                            "control_enabled",
                            False,
                        )
                    )
                    else 2
                ),
                item[0].casefold(),
            )
        )
        if not candidates:
            raise ModuleError(
                f"No {kind} instrument is present in the "
                "OpenLab system snapshot",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                kind,
            )
        instrument_id, values = candidates[0]
        return LR700Backend._snapshot_values(
            instrument_id,
            values,
            kind,
        )

    @staticmethod
    def _same_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        instrument_id: str,
        kind: str,
    ) -> tuple[str, float, str, float]:
        values = system.get(instrument_id)
        if values is None:
            raise ModuleError(
                f"{kind.title()} instrument {instrument_id} is "
                "missing from the second system snapshot",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                instrument_id,
            )
        return LR700Backend._snapshot_values(
            instrument_id,
            values,
            kind,
        )

    @staticmethod
    def _snapshot_values(
        instrument_id: str,
        values: Mapping[str, Any],
        kind: str,
    ) -> tuple[str, float, str, float]:
        if not bool(values.get("connected", True)):
            raise ModuleError(
                f"{kind.title()} instrument {instrument_id} is "
                "disconnected",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                instrument_id,
            )
        try:
            current = float(values["current"])
            timestamp = float(values["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModuleError(
                f"{kind.title()} instrument {instrument_id} has no "
                "valid current value or timestamp",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                instrument_id,
            ) from exc
        if (
            not math.isfinite(current)
            or not math.isfinite(timestamp)
        ):
            raise ModuleError(
                f"{kind.title()} instrument {instrument_id} returned "
                "a non-finite value or timestamp",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                instrument_id,
            )
        return (
            instrument_id,
            current,
            str(values.get("unit", "")),
            timestamp,
        )

    @staticmethod
    def _require_newer_snapshot(
        first: tuple[str, float, str, float],
        second: tuple[str, float, str, float],
    ) -> None:
        if second[3] <= first[3]:
            raise ModuleError(
                f"OpenLab system snapshot for {first[0]} "
                "did not advance between settle and dwell",
                "LR700_SYSTEM_SNAPSHOT_NOT_FRESH",
                first[0],
            )

    @staticmethod
    def _temperature_kelvin(
        value: float,
        unit: str,
    ) -> float:
        normalized = unit.strip().casefold()
        if normalized in {"k", "kelvin"}:
            return value
        if normalized in {"mk", "millikelvin"}:
            return value / 1000.0
        if normalized in {
            "c",
            "°c",
            "degc",
            "celsius",
        }:
            return value + 273.15
        raise ModuleError(
            f"Unsupported temperature unit: {unit!r}",
            "LR700_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    @staticmethod
    def _field_oersted(
        value: float,
        unit: str,
    ) -> float:
        normalized = unit.strip().casefold()
        if normalized in {
            "oe",
            "oersted",
            "g",
            "gauss",
        }:
            return value
        if normalized in {"koe", "kilo-oersted"}:
            return value * 1000.0
        if normalized in {"t", "tesla"}:
            return value * 10_000.0
        if normalized in {"mt", "millitesla"}:
            return value * 10.0
        raise ModuleError(
            f"Unsupported magnetic-field unit: {unit!r}",
            "LR700_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    def _close_transport(self) -> None:
        transport = self.transport
        self.transport = None
        self.protocol_signature = ""
        self._last_safe_bridge = None
        self._reset_autorange_session()
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _require_ready(self) -> None:
        if (
            self.transport is None
            or not self.applied_settings
        ):
            raise ModuleError(
                "Apply Settings and confirm the LR-700 "
                "connection before running Measure",
                "LR700_SETTINGS_NOT_APPLIED",
            )

    @staticmethod
    def _require_live_context(
        api: ModuleAPI,
    ) -> None:
        timeout = float(
            api.timeout
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModuleError(
                "Core module operation timeout must be a "
                "positive finite number",
                "LR700_INVALID_SETTINGS",
                "operation_timeout_seconds",
            )
        api.sleep(0)

    @classmethod
    def _normalized_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        require_resource: bool,
        operation_timeout_seconds: float,
    ) -> dict[str, Any]:
        """把文件/UI 输入规范化，并把总扫描时间与核心 IPC 超时绑定。"""

        defaults = default_settings()
        raw = dict(settings)
        common = None
        if "common" in raw:
            if not isinstance(raw["common"], Mapping):
                raise ModuleError("common must be a settings table", "LR700_INVALID_SETTINGS", "common")
            common = {**common_defaults(), **raw["common"]}
            common["current_limit_a"] = cls._number(common["current_limit_a"], 1e-12, 0.03, "common.current_limit_a")
            common["current_a"] = cls._number(common["current_a"], 1e-12, 0.03, "common.current_a")
            lo = cls._integer(common["autorange_min_range"], 1, 8, "common.autorange_min_range")
            hi = cls._integer(common["autorange_max_range"], 2, 9, "common.autorange_max_range")
            if lo >= hi:
                raise ModuleError("Autorange maximum must exceed minimum", "LR700_INVALID_SETTINGS", "common")
            common["range_mode"] = str(common["range_mode"]).strip().casefold()
            if common["range_mode"] not in {"autorange", "fixed"}:
                raise ModuleError("Unknown common range mode", "LR700_INVALID_SETTINGS", "common.range_mode")
            raw_channels = raw.get("channels", {})
            if not isinstance(raw_channels, Mapping):
                raise ModuleError("channels must be a settings table", "LR700_INVALID_SETTINGS", "channels")
            parsed_rows: list[tuple[str, Mapping[str, Any], bool, bool]] = []
            for slot in range(1, 5):
                key = f"r{slot}"
                supplied = raw_channels.get(key, {})
                if not isinstance(supplied, Mapping):
                    raise ModuleError(f"{key} must be a settings table", "LR700_INVALID_SETTINGS", key)
                override = cls._boolean(supplied.get("independent", False), f"{key}.independent")
                enabled = cls._boolean(supplied.get("enabled", slot == 1), f"{key}.enabled")
                parsed_rows.append((key, supplied, override, enabled))
            needs_autorange_trace = (
                common["range_mode"] == "autorange"
                and any(enabled and not override for _k, _s, override, enabled in parsed_rows)
            )
            if needs_autorange_trace:
                try:
                    trace, effective_lo, effective_hi = generate_trace(common["current_limit_a"], lo, hi)
                except ValueError as exc:
                    raise ModuleError(str(exc), "LR700_INVALID_SETTINGS", "common.current_limit_a") from exc
            else:
                trace = [minimum_excitation_index(r) for r in range(10)]
                effective_lo, effective_hi = lo, hi
            resolved = {}
            for slot, (key, supplied, override, enabled) in enumerate(parsed_rows, start=1):
                channel = {
                    **defaults["channels"][key],
                    **common,
                    "input_channel": supplied.get("input_channel", slot),
                    "enabled": enabled,
                    "autorange_min_range": effective_lo,
                    "autorange_max_range": effective_hi,
                }
                if override:
                    channel.update(
                        range_mode="fixed",
                        range_index=supplied.get("range_index", 2),
                        current_a=supplied.get("current_a", 0.003),
                    )
                if channel["range_mode"] == "fixed":
                    r = cls._integer(channel["range_index"], 0, 9, f"{key}.range_index")
                    if enabled:
                        current = cls._number(
                            channel["current_a"], 1e-12, common["current_limit_a"], f"{key}.current_a"
                        )
                        try:
                            e, percent, _actual = fixed_current_settings(r, current)
                        except ValueError as exc:
                            raise ModuleError(str(exc), "LR700_INVALID_SETTINGS", key) from exc
                        channel.update(range_index=r, excitation_index=e, excitation_percent=percent)
                    else:
                        channel.update(
                            range_index=r,
                            excitation_index=minimum_excitation_index(r),
                            excitation_percent=SAFE_EXCITATION_PERCENT,
                        )
                resolved[key] = channel
            enabled_fixed_ranges = [
                int(ch["range_index"])
                for ch in resolved.values()
                if ch["enabled"] and ch["range_mode"] == "fixed"
            ]
            for r_from in enabled_fixed_ranges:
                for r_to in enabled_fixed_ranges:
                    cls._require_safe_range_transition(
                        r_from,
                        r_to,
                        float(common["current_limit_a"]),
                    )
            raw["channels"] = resolved
        result = {
            key: raw.get(key, value)
            for key, value in defaults.items()
            if key != "channels"
        }
        resource = str(result["resource"]).strip()
        if (
            len(resource) > 255
            or "\r" in resource
            or "\n" in resource
        ):
            raise ModuleError(
                "Measurement resource ID must be one line with at "
                "most 255 characters",
                "LR700_INVALID_SETTINGS",
                "resource",
            )
        if require_resource and not resource:
            raise ModuleError(
                "Select a configured measurement instrument resource before Apply "
                "Settings",
                "LR700_INVALID_SETTINGS",
                "resource",
            )
        result["resource"] = resource
        result["switch_settle_seconds"] = cls._number(
            result["switch_settle_seconds"],
            0.1,
            300.0,
            "switch_settle_seconds",
        )
        result["dwell_seconds"] = cls._number(
            result["dwell_seconds"],
            0.1,
            300.0,
            "dwell_seconds",
        )
        result["io_timeout_seconds"] = cls._number(
            result["io_timeout_seconds"],
            0.1,
            30.0,
            "io_timeout_seconds",
        )
        result["retry_attempts"] = cls._integer(
            result["retry_attempts"],
            1,
            5,
            "retry_attempts",
        )

        raw_channels = raw.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raise ModuleError(
                "channels must be a settings table",
                "LR700_INVALID_SETTINGS",
                "channels",
            )
        channels: dict[str, dict[str, Any]] = {}
        enabled_filters: list[int] = []
        input_channels: list[int] = []
        for slot in range(1, 5):
            key = f"r{slot}"
            defaults_for_slot = defaults["channels"][key]
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                raise ModuleError(
                    f"{key} settings must be a table",
                    "LR700_INVALID_SETTINGS",
                    key,
                )
            channel = {
                name: supplied.get(name, value)
                for name, value
                in defaults_for_slot.items()
            }
            channel["input_channel"] = cls._integer(
                channel["input_channel"],
                1,
                16,
                f"{key}.input_channel",
            )
            channel["enabled"] = cls._boolean(
                channel["enabled"],
                f"{key}.enabled",
            )
            range_mode = (
                str(channel.get("range_mode", "fixed")).strip().casefold()
            )
            if range_mode not in {"fixed", "autorange"}:
                raise ModuleError(
                    f"{key}.range_mode must be 'fixed' or 'autorange'",
                    "LR700_INVALID_SETTINGS",
                    f"{key}.range_mode",
                )
            channel["range_mode"] = range_mode
            channel["range_index"] = cls._integer(
                channel["range_index"],
                0,
                9,
                f"{key}.range_index",
            )
            channel["excitation_index"] = (
                cls._integer(
                    channel[
                        "excitation_index"
                    ],
                    0,
                    6,
                    f"{key}.excitation_index",
                )
            )
            channel["excitation_percent"] = (
                cls._integer(
                    channel[
                        "excitation_percent"
                    ],
                    5,
                    100,
                    f"{key}.excitation_percent",
                )
            )
            channel["autorange_min_range"] = cls._integer(
                channel["autorange_min_range"],
                AUTORANGE_MIN_RANGE_INDEX if range_mode == "autorange" else 0,
                AUTORANGE_MAX_RANGE_INDEX - 1,
                f"{key}.autorange_min_range",
            )
            channel["autorange_max_range"] = cls._integer(
                channel["autorange_max_range"],
                (
                    AUTORANGE_MIN_RANGE_INDEX + 1
                    if range_mode == "autorange"
                    else 1
                ),
                AUTORANGE_MAX_RANGE_INDEX,
                f"{key}.autorange_max_range",
            )
            if (
                range_mode == "autorange"
                and channel["autorange_min_range"]
                >= channel["autorange_max_range"]
            ):
                raise ModuleError(
                    f"{key}: autorange upper range "
                    f"({range_label(channel['autorange_max_range'])}) must be "
                    "greater than lower range "
                    f"({range_label(channel['autorange_min_range'])})",
                    "LR700_INVALID_SETTINGS",
                    f"{key}.autorange_max_range",
                )
            if range_mode == "fixed" and not is_valid_range_excitation(
                channel["range_index"],
                channel["excitation_index"],
            ):
                allowed = ", ".join(
                    f"{idx} ({excitation_label(idx)})"
                    for idx in valid_excitation_indices(channel["range_index"])
                )
                raise ModuleError(
                    f"{key}: excitation "
                    f"{excitation_label(channel['excitation_index'])} is not "
                    "allowed on resistance range "
                    f"{range_label(channel['range_index'])} "
                    f"(LR-700 requires 1 nA to 30 mA full-scale current; "
                    f"valid excitations: {allowed})",
                    "LR700_INVALID_SETTINGS",
                    f"{key}.excitation_index",
                )
            channel["filter_index"] = cls._integer(
                channel["filter_index"],
                0,
                2,
                f"{key}.filter_index",
            )
            channels[key] = channel
            input_channels.append(
                int(channel["input_channel"])
            )
            if channel["enabled"]:
                enabled_filters.append(
                    int(channel["filter_index"])
                )
        if not enabled_filters:
            raise ModuleError(
                "Enable at least one LR700 slot (R1-R4)",
                "LR700_INVALID_SETTINGS",
                "channels",
            )
        if len(set(input_channels)) != len(
            input_channels
        ):
            raise ModuleError(
                "R1-R4 must select four different "
                "LR-720-16 physical inputs",
                "LR700_INVALID_SETTINGS",
                "channels",
            )

        filter_seconds = {
            index: seconds
            for index, _label, seconds in FILTERS
        }
        longest_filter = max(
            filter_seconds[index]
            for index in enabled_filters
        )
        required_settle = longest_filter * FILTER_SETTLE_MULTIPLIER
        if (
            float(
                result[
                    "switch_settle_seconds"
                ]
            )
            < required_settle
        ):
            raise ModuleError(
                "Switch settle time must be at least 3x the "
                f"longest enabled digital filter "
                f"(3 x {longest_filter:g} s = {required_settle:g} s)",
                "LR700_INVALID_SETTINGS",
                "switch_settle_seconds",
            )
        if common is not None:
            result["common"] = common
            result["channel_overrides"] = deepcopy(settings.get("channels", {}))
            result["autorange_trace"] = trace
            result["effective_autorange_min_range"] = effective_lo
            result["effective_autorange_max_range"] = effective_hi
            for channel in channels.values():
                channel["autorange_trace"] = trace
        result["channels"] = channels
        cls._validate_measure_duration(
            result,
            operation_timeout_seconds,
        )
        return result

    @staticmethod
    def _slot_measure_attempts(
        channel: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> int:
        if str(channel.get("range_mode", "fixed")).strip().casefold() == "autorange":
            return int(settings.get("retry_attempts", 2))
        return 1

    @staticmethod
    def _io_retry_margin(settings: Mapping[str, Any]) -> float:
        attempts = int(settings["retry_attempts"])
        reconnect_margin = max(
            5.0,
            max(0, attempts - 1)
            * float(instrument.LR700_RECONNECT_DELAY_SECONDS),
        )
        return float(settings["io_timeout_seconds"]) * attempts + reconnect_margin

    @classmethod
    def _estimated_measure_seconds(
        cls,
        settings: Mapping[str, Any],
    ) -> float:
        """给每通道等待和多条 GPIB 命令留出保守预算，不作为精确进度条。"""

        total_attempts = sum(
            cls._slot_measure_attempts(channel, settings)
            for channel in settings["channels"].values()
            if channel["enabled"]
        )
        return (
            total_attempts
            * (
                float(
                    settings[
                        "switch_settle_seconds"
                    ]
                )
                + float(settings["dwell_seconds"])
                + 2.0
            )
            + cls._io_retry_margin(settings)
        )

    @classmethod
    def _validate_measure_duration(
        cls,
        settings: Mapping[str, Any],
        operation_timeout_seconds: float,
    ) -> None:
        max_attempts = max(
            (
                cls._slot_measure_attempts(channel, settings)
                for channel in settings["channels"].values()
                if channel["enabled"]
            ),
            default=1,
        )
        estimate = (
            max_attempts
            * (
                float(settings["switch_settle_seconds"])
                + float(settings["dwell_seconds"])
                + 2.0
            )
            + cls._io_retry_margin(settings)
        )
        timeout = float(operation_timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or estimate > max(0.0, timeout - 2.0)
        ):
            raise ModuleError(
                f"Estimated LR-700 Measure time "
                f"{estimate:.1f} s does not fit the core "
                f"operation timeout {timeout:.1f} s; "
                "disable channels, shorten settle/dwell, "
                "or increase [modules] "
                "operation_timeout_seconds and restart",
                "LR700_MEASURE_TIMEOUT_UNSAFE",
            )

    @staticmethod
    def _integer(
        value: Any,
        minimum: int,
        maximum: int,
        name: str,
    ) -> int:
        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be an integer from "
                f"{minimum} to {maximum}",
                "LR700_INVALID_SETTINGS",
                name,
            )
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be an integer from "
                f"{minimum} to {maximum}",
                "LR700_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            isinstance(value, float)
            and value != result
        ) or not minimum <= result <= maximum:
            raise ModuleError(
                f"{name} must be an integer from "
                f"{minimum} to {maximum}",
                "LR700_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _number(
        value: Any,
        minimum: float,
        maximum: float,
        name: str,
    ) -> float:
        if isinstance(value, bool):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LR700_INVALID_SETTINGS",
                name,
            )
        try:
            result = float(value)
            if name.endswith("_seconds"):
                result = require_precision(result, 1, name)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name}: {exc}; must be from {minimum:g} to "
                f"{maximum:g}",
                "LR700_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            not math.isfinite(result)
            or not minimum <= result <= maximum
        ):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LR700_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _boolean(value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise ModuleError(
                f"{name} must be true or false",
                "LR700_INVALID_SETTINGS",
                name,
            )
        return value


Module = LR700Backend

__all__ = [
    "LR700Backend",
    "Module",
]
