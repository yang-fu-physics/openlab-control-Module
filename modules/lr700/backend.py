"""Linear Research LR-700 + LR-720-16 Measurement Module 后端。

实现依据用户提供的 LR-700 v1.3 手册：

- ``SELECT S=nn`` 选择 LR-720-16 的 1-16 号传感器；
- ``RANGE / EXCITATION / VAREXC / FILTER`` 设置当前桥参数；
- ``GET 0 / GET 1`` 分别读取 R 与 X；
- ``GET 6`` 读回量程、激励、滤波、模式和传感器号；
- ``GET 7`` 读取 8 位 ``OVERLOADS`` 状态。

LR-700 没有真正的 excitation-off 命令。软件能确认的最低状态是 20 uV 满量程激励的
5%，即 1 uV；切换通道、Stop、Error、Disable 和退出时都回到该状态。任何无法读回
确认最低激励的情况都是 Error，不能仅凭 worker 已退出宣称仪表安全。

本模块尚未连接真实 LR-700、LR-720-16、GPIB 控制器和实际传感器验证，因此版本保持
Beta。自动化测试验证的是协议状态机和异常边界，不是硬件安全认证。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from labcontrol.module_api import (
    ModuleError,
    ModuleAPI,
    ModuleWarning,
)

from .constants import (
    FILTERS,
    OVERLOAD_BITS,
    SAFE_EXCITATION_INDEX,
    SAFE_EXCITATION_PERCENT,
    STATUS_CODE_INVALID_READING,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVERLOAD,
    STATUS_CODE_OVER_RANGE,
    default_settings,
)
from . import lr700 as instrument


TransportFactory = Callable[
    [str, float],
    instrument.Transport,
]
ResourceLister = Callable[[], tuple[str, ...]]
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

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or instrument.PyVisaTransport
        )
        self._resource_lister = (
            resource_lister
            or instrument.PyVisaTransport.list_resources
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
        self.available_resources: tuple[str, ...] = ()

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 只枚举地址，不连接、不写 LR-700。"""

        self._require_live_context(api)
        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_resource=False,
            operation_timeout_seconds=(
                api.timeout
            ),
        )
        discovery_message = ""
        try:
            self.available_resources = (
                self._resource_lister()
            )
        except Exception as exc:
            # 没安装厂商 VISA 时仍允许模块窗口打开并手动填写地址；Apply 会再次严格
            # 连接并把失败报告为 Error。
            self.available_resources = ()
            discovery_message = (
                f"{type(exc).__name__}: {exc}"
            )
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
            "Available GPIB Resources": list(
                self.available_resources
            ),
            "Resource Discovery": (
                discovery_message or "Completed"
            ),
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
        self._connect(
            desired["resource"],
            float(desired["io_timeout_seconds"]),
        )
        try:
            self._set_safe_state(api)
        except Exception as failure:
            cleanup_error = (
                self._best_effort_safe_state()
            )
            self._close_transport()
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
            "Excitation Safety": self._safe_state_text(),
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
            self._set_safe_state(api)
        except Exception as failure:
            cleanup_error = (
                self._best_effort_safe_state()
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
            "Excitation Safety": self._safe_state_text(),
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
            self._set_safe_state(api)
            self._configure_sensor(
                input_channel,
                channel,
                api,
            )
            api.status({
                "Excitation Safety": (
                    "Measurement excitation active on "
                    f"R{slot} / sensor {input_channel}"
                ),
                "Last Slot / Sensor": (
                    f"R{slot} / S{input_channel}"
                ),
            })
            self._waiter(
                api,
                float(
                    settings["switch_settle_seconds"]
                ),
            )
            first = api.devices()
            self._waiter(
                api,
                float(settings["dwell_seconds"]),
            )
            second = api.devices()
            temperature, field = (
                self._averaged_system_values(
                    first,
                    second,
                )
            )

            # 在读取数值前后都核对当前传感器和设置。LR-700 未被 LocalLockout，
            # 操作者仍可使用前面板；两次读回可避免把中途改变后的值写入错误通道。
            before = self._query_bridge_settings(
                api
            )
            self._verify_channel_readback(
                input_channel,
                channel,
                before,
            )
            range_index = int(before["range_index"])
            data_failure: ModuleError | None = None
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
                overload_bits = self._query_overloads(
                    api
                )
            except ModuleError as exc:
                # 仅把已经收到但无法解析的 R/X/OVERLOAD 读数视为数据问题。GET 6
                # 设置读回、通信耗尽和安全状态失败仍是系统问题，必须终止 SEQ。
                if exc.code != "LR700_INVALID_REPLY":
                    raise
                data_failure = exc
            after = self._query_bridge_settings(
                api
            )
            self._verify_channel_readback(
                input_channel,
                channel,
                after,
            )

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
                row = {
                    "TemperatureAverage": temperature,
                    "FieldAverage": field,
                    "StatusCode": (
                        STATUS_CODE_INVALID_READING
                    ),
                }
                self.last_values = {
                    "slot": slot,
                    "input_channel": input_channel,
                    "status": "ERROR",
                }
                api.status({
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
                "TemperatureAverage": temperature,
                "FieldAverage": field,
                "StatusCode": status_code,
            }
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
                self._best_effort_safe_state()
            )
            api.status({
                "Excitation Safety": (
                    self._safe_state_text()
                    if cleanup_error is None
                    else "Minimum excitation unconfirmed"
                ),
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

        error = self._best_effort_safe_state()
        self.sequence_active = False
        if error is not None:
            api.status({
                "Sequence": reason.title(),
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
            "Excitation Safety": self._safe_state_text(),
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
            self._best_effort_safe_state()
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
            "Current Sensor": (
                f"S{bridge['sensor']}"
                if bridge["sensor"]
                else "Direct / sensor 0"
            ),
            "Current Range Index": (
                bridge["range_index"]
            ),
            "Current Excitation Index": (
                bridge["excitation_index"]
            ),
            "Current Excitation (%)": (
                bridge["excitation_percent"]
            ),
            "Current Filter Index": (
                bridge["filter_index"]
            ),
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
        """处理资源刷新和只读连接测试；两者都不会保存或 Apply 设置。"""

        if action == "refresh_resources":
            try:
                resources = self._resource_lister()
            except Exception as exc:
                raise ModuleWarning(
                    "GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "LR700_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            self.available_resources = resources
            status = {
                "Available GPIB Resources": list(
                    resources
                ),
                "Resource Discovery": "Completed",
                "Last Action": "GPIB resources refreshed",
            }
            api.status(status)
            return status

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
            try:
                transport = self._transport_factory(
                    desired["resource"],
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
                    f"{desired['resource']}: "
                    f"{type(exc).__name__}: {exc}",
                    "LR700_CONNECTION_FAILED",
                    desired["resource"],
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
        resource: str,
        timeout_seconds: float,
    ) -> None:
        """打开 VISA 并用只读 GET 6 验证 LR-700 协议结构。

        老式 LR-700 不提供 ``*IDN?``。相比仅测试“能读到任意字符串”，严格解析
        ``#R,#E,###%,#F,#M,#L,##S`` 是目前可用的协议身份门槛。
        """

        self._close_transport()
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
                f"Could not verify LR-700 protocol at "
                f"{resource}: {type(exc).__name__}: {exc}",
                "LR700_CONNECTION_FAILED",
                resource,
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
                    self._reopen_transport(settings)
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
                    self._reopen_transport(settings)
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
    ) -> None:
        """重建同一 GPIB session，并只读验证 GET 6 协议结构。"""

        resource = str(settings["resource"])
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
    ) -> None:
        """发送一套完整的当前传感器绝对设置，并用 GET 6 逐字段读回。"""

        commands = instrument.configuration_commands(
            input_channel,
            channel,
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

    def _set_safe_state(
        self,
        api: ModuleAPI,
    ) -> None:
        """通过普通受控 I/O 设置并读回最低激励。"""

        for command in instrument.safe_state_commands():
            self._write_absolute(command, api)
        bridge = self._query_bridge_settings(api)
        self._verify_safe_state(bridge)
        api.status({
            "Excitation Safety": self._safe_state_text(),
        })

    def _best_effort_safe_state(self) -> str | None:
        """在取消/异常路径直接设置最低激励，返回未确认原因而不吞掉原异常。

        先使用当前 session；失败后关闭并有界地重开一次，再重发绝对安全命令。成功
        后保持连接，供仍 Enabled 的模块继续使用。此方法不无限重试，也不把进程回收
        等同于仪表安全。
        """

        settings = (
            self.applied_settings
            or self.desired_settings
        )
        if not settings.get("resource"):
            return "no GPIB resource is configured"
        last_error: Exception | None = None
        for attempt in range(2):
            transport = self.transport
            if transport is None:
                try:
                    transport = self._transport_factory(
                        str(settings["resource"]),
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
                for command in (
                    instrument.safe_state_commands()
                ):
                    transport.write(command)
                bridge = (
                    self._parse_bridge_settings(
                        str(
                            transport.query(instrument.GET_SETTINGS)
                        ).strip()
                    )
                )
                self._verify_safe_state(bridge)
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
    def _safe_state_text() -> str:
        return (
            "Minimum confirmed: 20 uV x 5% "
            "(1 uV full scale; LR-700 has no Off command)"
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
    def _verify_channel_readback(
        input_channel: int,
        channel: Mapping[str, Any],
        bridge: Mapping[str, int],
    ) -> None:
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
        actual = (
            int(bridge["excitation_index"]),
            int(bridge["excitation_percent"]),
        )
        expected = (
            SAFE_EXCITATION_INDEX,
            SAFE_EXCITATION_PERCENT,
        )
        if actual != expected:
            raise ModuleError(
                "LR-700 minimum excitation readback "
                f"mismatch: expected {expected}, "
                f"read back {actual}",
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
    ) -> tuple[float, float]:
        """计算 dwell 前后主温度(K)和主磁场(Oe)的算术平均。"""

        first_temperature = cls._primary_snapshot(
            first,
            "temperature",
        )
        second_temperature = cls._same_snapshot(
            second,
            first_temperature[0],
            "temperature",
        )
        first_field = cls._primary_snapshot(
            first,
            "field",
        )
        second_field = cls._same_snapshot(
            second,
            first_field[0],
            "field",
        )
        cls._require_newer_snapshot(
            first_temperature,
            second_temperature,
        )
        cls._require_newer_snapshot(
            first_field,
            second_field,
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

    @staticmethod
    def _primary_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        kind: str,
    ) -> tuple[str, float, str, float]:
        candidates = [
            (str(device_id), values)
            for device_id, values in system.items()
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
                f"No {kind} device is present in the "
                "OpenLab system snapshot",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                kind,
            )
        device_id, values = candidates[0]
        return LR700Backend._snapshot_values(
            device_id,
            values,
            kind,
        )

    @staticmethod
    def _same_snapshot(
        system: Mapping[str, Mapping[str, Any]],
        device_id: str,
        kind: str,
    ) -> tuple[str, float, str, float]:
        values = system.get(device_id)
        if values is None:
            raise ModuleError(
                f"{kind.title()} device {device_id} is "
                "missing from the second system snapshot",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        return LR700Backend._snapshot_values(
            device_id,
            values,
            kind,
        )

    @staticmethod
    def _snapshot_values(
        device_id: str,
        values: Mapping[str, Any],
        kind: str,
    ) -> tuple[str, float, str, float]:
        if not bool(values.get("connected", True)):
            raise ModuleError(
                f"{kind.title()} device {device_id} is "
                "disconnected",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        try:
            current = float(values["current"])
            timestamp = float(values["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModuleError(
                f"{kind.title()} device {device_id} has no "
                "valid current value or timestamp",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            ) from exc
        if (
            not math.isfinite(current)
            or not math.isfinite(timestamp)
        ):
            raise ModuleError(
                f"{kind.title()} device {device_id} returned "
                "a non-finite value or timestamp",
                "LR700_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        return (
            device_id,
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
                "GPIB resource must be one line with at "
                "most 255 characters",
                "LR700_INVALID_SETTINGS",
                "resource",
            )
        if require_resource and not resource:
            raise ModuleError(
                "Select a GPIB resource before Apply "
                "Settings",
                "LR700_INVALID_SETTINGS",
                "resource",
            )
        if (
            resource
            and not resource.upper().startswith("GPIB")
        ):
            raise ModuleError(
                "LR-700 resource must be a GPIB VISA "
                "resource",
                "LR700_INVALID_SETTINGS",
                resource,
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
        required_settle = max(
            filter_seconds[index]
            for index in enabled_filters
        )
        if (
            float(
                result[
                    "switch_settle_seconds"
                ]
            )
            < required_settle
        ):
            raise ModuleError(
                "Switch settle time must be at least the "
                f"longest enabled digital filter "
                f"({required_settle:g} s)",
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
    def _estimated_measure_seconds(
        settings: Mapping[str, Any],
    ) -> float:
        """给每通道等待和多条 GPIB 命令留出保守预算，不作为精确进度条。"""

        enabled = sum(
            bool(channel["enabled"])
            for channel in settings[
                "channels"
            ].values()
        )
        return (
            enabled
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
        estimate = (
            float(settings["switch_settle_seconds"])
            + float(settings["dwell_seconds"])
            + 2.0
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
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
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
