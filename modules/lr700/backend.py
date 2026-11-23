"""Linear Research LR-700 + LR-720-16 Measurement Module 后端。

实现依据用户提供的 LR-700 v1.3 手册及真实 LR-700/LR-720 实测核对结果：

- ``SELECT S=nn`` 选择 LR-720-16 的 1-16 号传感器；
- ``RANGE / EXCITATION / VAREXC / FILTER / AUTORANGE`` 设置固定或自动量程桥参数；
- ``GET 0 / GET 1`` 分别读取 R 与 X；
- ``GET 6`` 读回量程、激励、滤波、模式和传感器号；
- ``GET 7`` 读取 8 位 ``OVERLOADS`` 状态（含共模过载排查提示）。

LR-700 没有真正的 excitation-off 命令。软件能确认的最低状态是“当前电阻量程允许的
最低激励档 × 5%”（0-7 档为 20 uV，200 kOhm 为 200 uV，2 MOhm 为 2 mV）；
切换通道、Stop、Error、Disable 和退出时都回到并读回确认该状态。任何无法读回确认
最低激励的情况都是 Error，不能仅凭 worker 已退出宣称仪表安全。
"""

from __future__ import annotations

import math
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
    default_settings,
    excitation_current_a,
    excitation_label,
    format_current,
    is_valid_range_excitation,
    minimum_excitation_index,
    range_label,
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

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or instrument.PyVisaTransport
        )
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
        """连接并确认 LR-700 协议，然后把桥置于最低可确认激励。

        LR-700 的量程、滤波和激励是全局状态，不能同时为多个传感器各保存一套。
        因此 Apply 表示“接受并验证四槽位扫描参数”，Measure 才在切换每个物理输入后
        发送该槽位的参数。Apply 不会把 R1 的工作激励长期留在样品上。
        """

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
                    self._set_safe_state(api)
                except Exception as failure:
                    cleanup_error = self._best_effort_safe_state(api)
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
            safe_bridge = self._set_safe_state(api)
        except Exception as failure:
            cleanup_error = (
                self._best_effort_safe_state(api)
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
        status = {
            "Connection": "Connected",
            "Resource": desired["resource"],
            "Protocol": self.protocol_signature,
            "Applied Settings": (
                "Applied; per-slot settings are sent "
                "during Measure"
            ),
            "Excitation Safety": self._safe_state_text(safe_bridge),
            **self._bridge_status_fields(safe_bridge),
            "Estimated Measure Time (s)": (
                self._estimated_measure_seconds(desired)
            ),
        }
        api.status(status)
        return status

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """开始 Run 前重新确认最低激励，防止 Idle 期间前面板改变输出。"""

        self._require_ready()
        try:
            safe_bridge = self._set_safe_state(api)
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
            "Excitation Safety": self._safe_state_text(safe_bridge),
            **self._bridge_status_fields(safe_bridge),
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

    def _measure_sensor(
        self,
        slot: int,
        input_channel: int,
        channel: Mapping[str, Any],
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """执行一个逻辑槽位的完整事务，并在 finally 中确认最低激励。"""

        failure: Exception | None = None
        try:
            safe_bridge = self._set_safe_state(api)
            configured_bridge = self._configure_sensor(
                input_channel,
                channel,
                api,
                current_range_index=int(safe_bridge["range_index"]),
            )
            api.status({
                **self._bridge_status_fields(configured_bridge),
                "Excitation Safety": (
                    "Measurement excitation active on "
                    f"R{slot} / sensor {input_channel}"
                ),
                "Last Slot / Sensor": (
                    f"R{slot} / S{input_channel}"
                ),
            })

            # 在读取数值前后都核对当前传感器和设置。LR-700 未被 LocalLockout，
            # 操作者仍可使用前面板；两次读回可避免把中途改变后的值写入错误通道。
            # 自动量程模式下把等待拆为 1/4 与结束两次 GET 6 核对：不一致则直接
            # 重等一整轮；若进入 dwell 或读数瞬间又发生换挡，同样重等并刷新温场快照。
            is_autorange = (
                str(channel.get("range_mode", "fixed")).strip().casefold()
                == "autorange"
            )
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
                raise ModuleError(
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
        except Exception as exc:
            failure = exc
            raise
        finally:
            # Stop 会让普通 api checkpoint 立即取消，因此清理路径直接使用带
            # VISA timeout 的底层 transport，不依赖协作上下文。
            cleanup_error = (
                self._best_effort_safe_state(api)
            )
            if cleanup_error is None and self._last_safe_bridge is not None:
                api.status({
                    **self._bridge_status_fields(self._last_safe_bridge),
                    "Excitation Safety": self._safe_state_text(
                        self._last_safe_bridge
                    ),
                })
            else:
                api.status({
                    **self._cleared_bridge_status_fields("Unknown"),
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
                "Excitation Safety": "No instrument connection was opened or changed",
            }
            api.status(status)
            return status
        error = self._best_effort_safe_state(api)
        self.sequence_active = False
        if error is not None:
            api.status({
                "Sequence": reason.title(),
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
            self._best_effort_safe_state(api)
            if had_applied_connection
            else None
        )
        self._close_transport()
        self.sequence_active = False
        self.applied_settings = {}
        status = {
            "Connection": "Disconnected",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
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
        """发送一次绝对设置命令；写超时后不自动重放。

        GPIB 写异常无法判断仪表是否已经执行命令。即使命令本身是绝对值，盲目重放也
        会掩盖链路故障，因此先关闭 session、报告 Error，再由 finally 使用独立的
        最低激励恢复路径处理安全状态。
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
                self._waiter(api, 0.2)
                try:
                    self._reopen_transport(settings, api)
                except Exception as exc:
                    last_error = exc
        api.warn("LR700_IO_RETRY", None, command)
        assert last_error is not None
        raise ModuleError(
            f"LR-700 read failed after {attempts} "
            f"attempt(s) for {command}: "
            f"{type(last_error).__name__}: {last_error}",
            "LR700_COMMUNICATION_FAILED",
            command,
        ) from last_error

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

    def _configure_sensor(
        self,
        input_channel: int,
        channel: Mapping[str, Any],
        api: ModuleAPI,
        *,
        current_range_index: int | None = None,
    ) -> dict[str, int]:
        """发送一套完整的当前传感器绝对设置，并用 GET 6 逐字段读回。"""

        commands = instrument.configuration_commands(
            input_channel,
            channel,
            current_range_index=current_range_index,
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
    ) -> dict[str, int]:
        """通过普通受控 I/O 按当前电阻量程设置并读回最低激励（× 5%）。"""

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
        self._last_safe_bridge = dict(bridge)
        api.status({
            **self._bridge_status_fields(bridge),
            "Excitation Safety": self._safe_state_text(bridge),
        })
        return bridge

    def _best_effort_safe_state(
        self,
        api: ModuleAPI,
    ) -> str | None:
        """在取消/异常路径直接按当前电阻量程设置最低激励，返回未确认原因而不吞掉原异常。

        先使用当前 session；失败后关闭并有界地重开一次，再重发绝对安全命令。成功
        后保持连接，供仍 Enabled 的模块继续使用。此方法不无限重试，也不把进程回收
        等同于仪表安全。
        """

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
        return self._parse_bridge_settings(
            self._query_text(instrument.GET_SETTINGS, api)
        )

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

    @staticmethod
    def _verify_channel_readback(
        input_channel: int,
        channel: Mapping[str, Any],
        bridge: Mapping[str, int],
    ) -> None:
        range_mode = (
            str(channel.get("range_mode", "fixed")).strip().casefold()
        )
        if range_mode == "autorange":
            expected_common = {
                "filter_index": int(channel["filter_index"]),
                "excitation_percent": 100,
                "mode": 0,
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
            if (
                actual_common != expected_common
                or not min_range <= actual_range <= max_range
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
            "mode": 0,
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
                0,
                8,
                f"{key}.autorange_min_range",
            )
            channel["autorange_max_range"] = cls._integer(
                channel["autorange_max_range"],
                1,
                9,
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
            + float(settings["io_timeout_seconds"])
            * int(settings["retry_attempts"])
            + 5.0
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
            + float(settings["io_timeout_seconds"])
            * int(settings["retry_attempts"])
            + 5.0
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
