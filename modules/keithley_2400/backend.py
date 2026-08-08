"""Keithley Model 2400 电阻测量模块后端。

模块把 2400 当作单通道 SMU 使用：恒流时读取 V/I，恒压时同样读取 V/I，最后统一
计算 ``R = V / I``。模块不使用仪表的 AUTO OHMS 模式，因为用户需要显式控制源模式；
这样 DAT 中的电压和电流也与实际本次读数一一对应。

所有主动输出都被限制在一次 ``measure`` 调用内部。输出状态、源设置、compliance 和
两线/四线设置必须由仪表读回确认；仅有 ``write`` 成功不视为配置完成。
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
    DEVICE_MAX_CURRENT_A,
    DEVICE_MAX_VOLTAGE_V,
    SENSE_2WIRE,
    SENSE_4WIRE,
    SOURCE_CURRENT,
    SOURCE_VOLTAGE,
    STATUS_CODE_COMPLIANCE,
    STATUS_CODE_INVALID_READING,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVER_RANGE,
    default_settings,
)
from .quantities import parse_quantity
from . import keithley_2400 as instrument


_READING_SENTINEL = 9.0e36
_MEASURE_CLEANUP_RESERVE_SECONDS = 3.0


TransportFactory = Callable[[str, float], instrument.Transport]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleAPI, float], None]


class Keithley2400Backend:
    """2400 的生命周期、读回确认、测量和安全关闭状态机。"""

    columns = {
        "Resistance": "Ohm",
        "Voltage": "V",
        "Current": "A",
        "StatusCode": "",
    }

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = transport_factory or instrument.PyVisaTransport
        self._resource_lister = (
            resource_lister or instrument.PyVisaTransport.list_resources
        )
        self._waiter = waiter or (
            lambda api, seconds: api.sleep(seconds)
        )
        self.transport: instrument.Transport | None = None
        self.desired_settings: dict[str, Any] = default_settings()
        self.applied_settings: dict[str, Any] | None = None
        self.available_resources: tuple[str, ...] = ()
        self.identity = ""
        self.sequence_active = False
        self.last_status = "Idle"
        self.last_resistance: float | None = None
        self.last_voltage: float | None = None
        self.last_current: float | None = None

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 只发现资源，绝不连接或改变 2400。"""

        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_resource=False,
            operation_timeout_seconds=api.timeout,
        )
        try:
            self.available_resources = tuple(
                sorted(set(self._resource_lister()), key=str.casefold)
            )
            api.warn("K2400_RESOURCE_DISCOVERY_FAILED", None)
        except Exception as exc:
            self.available_resources = ()
            api.warn(
                "K2400_RESOURCE_DISCOVERY_FAILED",
                "GPIB resource discovery failed: "
                f"{type(exc).__name__}: {exc}",
            )
        self.applied_settings = None
        self.identity = ""
        self.sequence_active = False
        self.last_status = "Initialized"
        status = self._status()
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """连接、识别、配置并确认输出关闭。

        Apply 不使用 ``*RST``，避免清除操作员在前面板建立的其他现场设置。模块只写
        完成本测量所需的 SCPI 字段，并逐项读回。
        """

        normalized = self._normalized_settings(
            settings,
            require_resource=True,
            operation_timeout_seconds=api.timeout,
        )
        self.desired_settings = deepcopy(normalized)
        self.applied_settings = None
        try:
            if self.transport is not None:
                self._set_output(False, api)
                self._close_transport()
            self._connect(normalized, api)
            self._set_output(False, api)
            self._write(instrument.CLEAR_STATUS, api)
            self._configure(normalized, api)
            self._set_output(False, api)
            self._raise_if_instrument_error(api)
        except Exception as exc:
            cleanup = self._best_effort_output_off()
            self._close_transport_silently()
            if cleanup:
                raise ModuleError(
                    "2400 settings failed and output-off could not be confirmed: "
                    f"{cleanup}",
                    "K2400_SAFE_STATE_UNCONFIRMED",
                    "configure",
                ) from exc
            raise
        self.applied_settings = deepcopy(normalized)
        self.last_status = "Settings applied - output off"
        status = self._status()
        api.status(status)
        return status

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """Run 开始时确认配置仍一致且输出关闭。"""

        settings = self._require_applied()
        self._set_output(False, api)
        self._verify_configuration(settings, api)
        self.sequence_active = True
        self.last_status = "Sequence ready - output off"
        status = self._status()
        api.status(status)
        return status

    def measure(self, slot: int, api: ModuleAPI) -> Mapping[str, Any]:
        """执行一次有严格清理边界的源-等待-读数事务。"""

        del slot
        settings = self._require_ready()
        output_off = bool(
            settings["output_off_between_measurements"]
        )
        voltage = 0.0
        current = 0.0
        compliance = False
        try:
            api.sleep(0)
            self._verify_configuration(settings, api)
            self._set_output(True, api)
            self._waiter(api, float(settings["settle_seconds"]))
            voltage, current = self._read_voltage_current(api)
            compliance = self._read_compliance(settings, api)
            api.sleep(0)
        except Exception as exc:
            cleanup = self._best_effort_output_off()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "2400 measurement was interrupted and output-off could not "
                    f"be confirmed: {cleanup}",
                    "K2400_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise

        # 默认在正式行之前严格关闭。选择行间保持时，仍在每次读取后查询输出，确认
        # 它确实处于有意的 ON 状态；Stop/Error/completed/Disable 始终走关闭路径。
        try:
            if output_off:
                self._set_output(False, api)
            elif not self._query_switch(instrument.OUTPUT_QUERY, api):
                raise ModuleError(
                    "2400 output turned off unexpectedly while row-boundary "
                    "retention was enabled",
                    "K2400_OUTPUT_MISMATCH",
                    instrument.OUTPUT_QUERY,
                )
        except Exception as exc:
            cleanup = self._best_effort_output_off()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "2400 reading completed but a defined output state could "
                    f"not be confirmed: {cleanup}",
                    "K2400_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise
        status_code, issue = self._classify_reading(
            voltage,
            current,
            compliance,
        )
        row: dict[str, Any] = {"StatusCode": status_code}
        if status_code == STATUS_CODE_NORMAL:
            resistance = voltage / current
            row.update(
                {
                    "Resistance": resistance,
                    "Voltage": voltage,
                    "Current": current,
                }
            )
            api.warn("K2400_READING_WARNING", None)
            self.last_resistance = resistance
            self.last_voltage = voltage
            self.last_current = current
            self.last_status = (
                "Normal - output off"
                if output_off
                else "Normal - output retained"
            )
        else:
            api.warn(
                "K2400_READING_WARNING",
                f"Keithley 2400 reading is not valid: {issue}",
            )
            self.last_resistance = None
            self.last_voltage = None
            self.last_current = None
            self.last_status = (
                f"Data warning ({status_code}) - "
                + ("output off" if output_off else "output retained")
            )
        api.status(self._status())
        return row

    def _run_end(
        self,
        reason: str,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """completed、stopped 和 error 都关闭并确认输出。"""

        self.sequence_active = False
        self._set_output(False, api)
        self.last_status = f"Sequence {reason} - output off"
        status = self._status()
        api.status(status)
        return status

    def close(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """Disable/退出时确认输出关闭并释放 VISA session；可重复调用。"""

        self.sequence_active = False
        failure: Exception | None = None
        try:
            if self.transport is not None:
                self._set_output(False, api)
        except Exception as exc:
            failure = exc
        finally:
            try:
                self._close_transport()
            except Exception as exc:
                failure = failure or exc
            self.applied_settings = None
        self.last_status = (
            "Disabled - safe state unconfirmed" if failure else "Disabled"
        )
        status = self._status()
        api.status(status)
        if failure is not None:
            if isinstance(failure, ModuleError):
                raise failure
            raise ModuleError(
                f"2400 shutdown failed: {type(failure).__name__}: {failure}",
                "K2400_SHUTDOWN_FAILED",
            ) from failure
        return status

    def _read_status(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """只读查询当前输出和基本配置，不隐式连接或 Apply。"""

        if self.transport is not None:
            output = self._query_switch(instrument.OUTPUT_QUERY, api)
            source = self._clean_token(
                self._query(instrument.SOURCE_FUNCTION_QUERY, api)
            )
            sense = self._query_switch(instrument.REMOTE_SENSE_QUERY, api)
            self.last_status = (
                f"Connected / {source} / "
                f"{'4-wire' if sense else '2-wire'} / "
                f"output {'on' if output else 'off'}"
            )
        status = self._status()
        api.status(status)
        return status

    def _action(
        self,
        action: str,
        payload: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """处理 Idle 时的资源刷新、只读连接测试和显式 Safe Off。"""

        if action == "refresh_resources":
            try:
                self.available_resources = tuple(
                    sorted(set(self._resource_lister()), key=str.casefold)
                )
            except Exception as exc:
                raise ModuleWarning(
                    "GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "K2400_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            api.warn("K2400_RESOURCE_DISCOVERY_FAILED", None)
        elif action == "test_connection":
            candidate = payload.get("settings", self.desired_settings)
            if not isinstance(candidate, Mapping):
                raise ModuleError(
                    "Test Connection settings must be a mapping",
                    "K2400_INVALID_SETTINGS",
                    "settings",
                )
            settings = self._normalized_settings(
                candidate,
                require_resource=True,
                operation_timeout_seconds=api.timeout,
            )
            self._test_connection(settings, api)
            self.last_status = "Connection test passed (read-only)"
        elif action == "safe_off":
            if self.transport is not None:
                self._set_output(False, api)
            self.last_status = "Output off"
        else:
            raise ModuleError(
                f"Unsupported action: {action}",
                "UNSUPPORTED_ACTION",
                action,
            )
        status = self._status()
        api.status(status)
        return status

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """把四种可选核心通知收敛到模块内部实现。"""

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
                    "K2400_INVALID_ACTION",
                )
            return self._action(str(data.get("name", "")), payload, api)
        return {}

    def _connect(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        resource = str(settings["resource"])
        timeout = float(settings["io_timeout_seconds"])
        try:
            transport = self._transport_factory(resource, timeout)
        except Exception as exc:
            raise ModuleError(
                f"Could not open 2400 at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K2400_CONNECTION_FAILED",
                resource,
            ) from exc
        self.transport = transport
        try:
            identity = self._query(instrument.IDENTIFY, api)
            self._validate_identity(identity)
        except Exception:
            self._close_transport_silently()
            raise
        self.identity = identity.strip()

    def _test_connection(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        resource = str(settings["resource"])
        timeout = float(settings["io_timeout_seconds"])
        # 已有同一会话时只做身份查询，避免 VISA implementation 拒绝第二个独占 session。
        if self.transport is not None and self.applied_settings is not None:
            if str(self.applied_settings["resource"]) == resource:
                self._validate_identity(
                    self._query(instrument.IDENTIFY, api)
                )
                return
        try:
            temporary = self._transport_factory(resource, timeout)
        except Exception as exc:
            raise ModuleError(
                f"Could not open 2400 at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K2400_CONNECTION_FAILED",
                resource,
            ) from exc
        try:
            api.sleep(0)
            self._validate_identity(
                str(temporary.query(instrument.IDENTIFY)).strip()
            )
            api.sleep(0)
        except ModuleError:
            raise
        except Exception as exc:
            raise ModuleError(
                f"2400 identity query failed: {type(exc).__name__}: {exc}",
                "K2400_IO_FAILED",
                instrument.IDENTIFY,
            ) from exc
        finally:
            temporary.close()

    @staticmethod
    def _validate_identity(identity: str) -> None:
        if not instrument.validate_identity(identity):
            raise ModuleError(
                f"Expected Keithley Model 2400, received {identity!r}",
                "K2400_IDENTITY_MISMATCH",
                instrument.IDENTIFY,
            )

    def _configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        # 电阻必须由同一次触发得到的实际 V/I 计算。2400 手册明确规定 CONC OFF 时
        # 只能启用一个测量函数，因此这里固定同时启用电压和电流；否则 FORM:ELEM 虽然
        # 列出两个字段，未启用的那一项也不代表一次有效测量。
        for command in instrument.configuration_commands(settings):
            self._write(command, api)
        self._verify_configuration(settings, api)

    def _verify_configuration(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        mode = str(settings["source_mode"])
        actual_source = self._clean_token(
            self._query(instrument.SOURCE_FUNCTION_QUERY, api)
        )
        expected_source = "CURR" if mode == SOURCE_CURRENT else "VOLT"
        if not actual_source.startswith(expected_source):
            self._settings_mismatch("source_mode", expected_source, actual_source)

        if mode == SOURCE_CURRENT:
            self._expect_number(
                instrument.SOURCE_CURRENT_LEVEL_QUERY,
                float(settings["source_current"]),
                "source_current",
                api,
            )
            self._expect_number(
                instrument.VOLTAGE_COMPLIANCE_QUERY,
                float(settings["voltage_compliance"]),
                "voltage_compliance",
                api,
            )
        else:
            self._expect_number(
                instrument.SOURCE_VOLTAGE_LEVEL_QUERY,
                float(settings["source_voltage"]),
                "source_voltage",
                api,
            )
            self._expect_number(
                instrument.CURRENT_COMPLIANCE_QUERY,
                float(settings["current_compliance"]),
                "current_compliance",
                api,
            )
        if not self._query_switch(instrument.CONCURRENT_QUERY, api):
            self._settings_mismatch(
                "concurrent_measurements",
                True,
                False,
            )
        actual_functions = {
            self._clean_token(item).split(":", 1)[0]
            for item in self._query(
                instrument.SENSE_FUNCTIONS_QUERY, api
            ).split(",")
            if item.strip()
        }
        if actual_functions != {"VOLT", "CURR"}:
            self._settings_mismatch(
                "sense_functions",
                "VOLT,CURR",
                ",".join(sorted(actual_functions)),
            )
        for command, field in (
            (instrument.VOLTAGE_NPLC_QUERY, "voltage_nplc"),
            (instrument.CURRENT_NPLC_QUERY, "current_nplc"),
        ):
            self._expect_number(
                command,
                float(settings["nplc"]),
                field,
                api,
            )
        remote = self._query_switch(instrument.REMOTE_SENSE_QUERY, api)
        expected_remote = settings["sense_mode"] == SENSE_4WIRE
        if remote != expected_remote:
            self._settings_mismatch("sense_mode", expected_remote, remote)
        elements = {
            self._clean_token(item)
            for item in self._query(
                instrument.DATA_ELEMENTS_QUERY, api
            ).split(",")
        }
        if elements != {"VOLT", "CURR"}:
            self._settings_mismatch(
                "data_elements",
                "VOLT,CURR",
                ",".join(sorted(elements)),
            )

    def _read_voltage_current(
        self,
        api: ModuleAPI,
    ) -> tuple[float, float]:
        reply = self._query(instrument.READ, api)
        try:
            return instrument.parse_voltage_current(reply)
        except ValueError as exc:
            raise ModuleError(
                f"2400 {instrument.READ} returned invalid data: {reply!r}",
                "K2400_INVALID_RESPONSE",
                instrument.READ,
            ) from exc

    def _read_compliance(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> bool:
        command = instrument.compliance_query(settings["source_mode"])
        return self._query_switch(command, api)

    @staticmethod
    def _classify_reading(
        voltage: float,
        current: float,
        compliance: bool,
    ) -> tuple[int, str]:
        if compliance:
            return STATUS_CODE_COMPLIANCE, "source reached compliance"
        if (
            not math.isfinite(voltage)
            or not math.isfinite(current)
            or abs(voltage) >= _READING_SENTINEL
            or abs(current) >= _READING_SENTINEL
        ):
            return STATUS_CODE_OVER_RANGE, "instrument returned overrange data"
        if abs(current) <= 1.0e-30:
            return STATUS_CODE_INVALID_READING, "measured current is zero"
        resistance = voltage / current
        if not math.isfinite(resistance) or abs(resistance) >= _READING_SENTINEL:
            return STATUS_CODE_OVER_RANGE, "calculated resistance is overrange"
        return STATUS_CODE_NORMAL, ""

    def _set_output(
        self,
        enabled: bool,
        api: ModuleAPI,
    ) -> None:
        self._write(instrument.output_command(enabled), api)
        actual = self._query_switch(instrument.OUTPUT_QUERY, api)
        if actual != enabled:
            raise ModuleError(
                "2400 output readback did not match the requested state",
                "K2400_OUTPUT_MISMATCH",
                instrument.OUTPUT_QUERY,
            )

    def _best_effort_output_off(self) -> str | None:
        """取消路径绕过 checkpoint 直接请求关闭，避免 Stop 阻止清理命令。"""

        if self.transport is None:
            return None
        try:
            self.transport.write(instrument.OUTPUT_OFF)
            actual = self._parse_switch(
                self.transport.query(instrument.OUTPUT_QUERY),
                instrument.OUTPUT_QUERY,
            )
            if actual:
                return f"{instrument.OUTPUT_QUERY} still reports ON"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def _raise_if_instrument_error(
        self,
        api: ModuleAPI,
    ) -> None:
        reply = self._query(instrument.ERROR_QUERY, api)
        try:
            error_code = instrument.parse_error_code(reply)
        except ValueError as exc:
            raise ModuleError(
                f"2400 returned an invalid error-queue response: {reply!r}",
                "K2400_INVALID_RESPONSE",
                instrument.ERROR_QUERY,
            ) from exc
        if error_code != 0:
            raise ModuleError(
                f"2400 reported an instrument error: {reply}",
                "K2400_INSTRUMENT_ERROR",
                instrument.ERROR_QUERY,
            )

    def _write(self, command: str, api: ModuleAPI) -> None:
        transport = self._require_transport()
        api.sleep(0)
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"2400 write failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K2400_IO_FAILED",
                command,
            ) from exc
        api.sleep(0)

    def _query(self, command: str, api: ModuleAPI) -> str:
        transport = self._require_transport()
        api.sleep(0)
        try:
            reply = str(transport.query(command)).strip()
        except Exception as exc:
            raise ModuleError(
                f"2400 query failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K2400_IO_FAILED",
                command,
            ) from exc
        api.sleep(0)
        if not reply:
            raise ModuleError(
                f"2400 returned an empty response for {command!r}",
                "K2400_INVALID_RESPONSE",
                command,
            )
        return reply

    def _query_switch(
        self,
        command: str,
        api: ModuleAPI,
    ) -> bool:
        return self._parse_switch(self._query(command, api), command)

    @staticmethod
    def _parse_switch(value: object, command: str) -> bool:
        try:
            return instrument.parse_switch(value)
        except ValueError as exc:
            raise ModuleError(
                f"2400 returned invalid switch state {value!r}",
                "K2400_INVALID_RESPONSE",
                command,
            ) from exc

    def _expect_number(
        self,
        command: str,
        expected: float,
        field: str,
        api: ModuleAPI,
    ) -> None:
        reply = self._query(command, api)
        try:
            actual = instrument.parse_number(reply)
        except ValueError as exc:
            raise ModuleError(
                f"2400 returned non-numeric readback {reply!r}",
                "K2400_INVALID_RESPONSE",
                command,
            ) from exc
        tolerance = max(1.0e-12, abs(expected) * 1.0e-6)
        if not math.isfinite(actual) or abs(actual - expected) > tolerance:
            self._settings_mismatch(field, expected, actual)

    @staticmethod
    def _settings_mismatch(field: str, expected: object, actual: object) -> None:
        raise ModuleError(
            f"2400 {field} readback mismatch: expected {expected!r}, "
            f"received {actual!r}",
            "K2400_SETTINGS_MISMATCH",
            field,
        )

    @staticmethod
    def _clean_token(value: object) -> str:
        return instrument.clean_token(value)

    @staticmethod
    def _scpi(value: object) -> str:
        return instrument.number(value)

    def _require_transport(self) -> instrument.Transport:
        if self.transport is None:
            raise ModuleError(
                "Keithley 2400 is not connected; Apply Settings first",
                "K2400_NOT_CONNECTED",
            )
        return self.transport

    def _require_applied(self) -> dict[str, Any]:
        if self.applied_settings is None or self.transport is None:
            raise ModuleError(
                "Keithley 2400 settings have not been applied",
                "K2400_NOT_APPLIED",
            )
        return deepcopy(self.applied_settings)

    def _require_ready(self) -> dict[str, Any]:
        settings = self._require_applied()
        if not self.sequence_active:
            raise ModuleError(
                "Keithley 2400 sequence has not begun",
                "K2400_SEQUENCE_NOT_ACTIVE",
            )
        return settings

    def _close_transport(self) -> None:
        transport = self.transport
        self.transport = None
        self.identity = ""
        if transport is not None:
            transport.close()

    def _close_transport_silently(self) -> None:
        try:
            self._close_transport()
        except Exception:
            pass

    def _status(self) -> dict[str, Any]:
        return {
            "Connection": "Connected" if self.transport is not None else "Disconnected",
            "Resource": self.desired_settings.get("resource") or "Not selected",
            "Identity": self.identity or "Not queried",
            "Applied Settings": "Applied" if self.applied_settings is not None else "Not applied",
            "Sequence": "Running" if self.sequence_active else "Idle",
            "Output": (
                "See Last Status" if self.transport is not None else "Unknown"
            ),
            "Last Status": self.last_status,
            "Last Resistance (Ohm)": (
                self.last_resistance if self.last_resistance is not None else "-"
            ),
            "Last Voltage (V)": (
                self.last_voltage if self.last_voltage is not None else "-"
            ),
            "Last Current (A)": (
                self.last_current if self.last_current is not None else "-"
            ),
            "Available GPIB Resources": list(self.available_resources),
        }

    def _normalized_settings(
        self,
        supplied: Mapping[str, Any],
        *,
        require_resource: bool,
        operation_timeout_seconds: float,
    ) -> dict[str, Any]:
        if not isinstance(supplied, Mapping):
            raise ModuleError(
                "Keithley 2400 settings must be a mapping",
                "K2400_INVALID_SETTINGS",
                "settings",
            )
        merged = default_settings()
        for key in merged:
            if key in supplied:
                merged[key] = supplied[key]

        resource = str(merged["resource"]).strip()
        if require_resource and not resource:
            raise ModuleError(
                "Select or enter a GPIB VISA resource",
                "K2400_INVALID_SETTINGS",
                "resource",
            )
        if "\n" in resource or "\r" in resource:
            raise ModuleError(
                "VISA resource must be a single line",
                "K2400_INVALID_SETTINGS",
                "resource",
            )
        mode = str(merged["source_mode"]).strip().casefold()
        if mode not in {SOURCE_CURRENT, SOURCE_VOLTAGE}:
            raise ModuleError(
                "source_mode must be current or voltage",
                "K2400_INVALID_SETTINGS",
                "source_mode",
            )
        sense = str(merged["sense_mode"]).strip().casefold()
        if sense not in {SENSE_2WIRE, SENSE_4WIRE}:
            raise ModuleError(
                "sense_mode must be 2wire or 4wire",
                "K2400_INVALID_SETTINGS",
                "sense_mode",
            )

        source_current = self._quantity(
            merged["source_current"], "A", "source_current"
        )
        voltage_compliance = self._quantity(
            merged["voltage_compliance"], "V", "voltage_compliance"
        )
        source_voltage = self._quantity(
            merged["source_voltage"], "V", "source_voltage"
        )
        current_compliance = self._quantity(
            merged["current_compliance"], "A", "current_compliance"
        )
        if abs(source_current) > DEVICE_MAX_CURRENT_A:
            self._invalid_range(
                "source_current", -DEVICE_MAX_CURRENT_A, DEVICE_MAX_CURRENT_A
            )
        if abs(source_voltage) > DEVICE_MAX_VOLTAGE_V:
            self._invalid_range(
                "source_voltage", -DEVICE_MAX_VOLTAGE_V, DEVICE_MAX_VOLTAGE_V
            )
        if not 0 < voltage_compliance <= DEVICE_MAX_VOLTAGE_V:
            self._invalid_range(
                "voltage_compliance", 0, DEVICE_MAX_VOLTAGE_V
            )
        if not 0 < current_compliance <= DEVICE_MAX_CURRENT_A:
            self._invalid_range(
                "current_compliance", 0, DEVICE_MAX_CURRENT_A
            )

        io_timeout = self._finite_number(
            merged["io_timeout_seconds"], "io_timeout_seconds"
        )
        nplc = self._finite_number(merged["nplc"], "nplc")
        settle = self._finite_number(
            merged["settle_seconds"], "settle_seconds"
        )
        if not 0.1 <= io_timeout <= 30.0:
            self._invalid_range("io_timeout_seconds", 0.1, 30.0)
        if not 0.01 <= nplc <= 10.0:
            self._invalid_range("nplc", 0.01, 10.0)
        if not 0.0 <= settle <= 3600.0:
            self._invalid_range("settle_seconds", 0.0, 3600.0)
        output_off = merged["output_off_between_measurements"]
        if not isinstance(output_off, bool):
            raise ModuleError(
                "output_off_between_measurements must be true or false",
                "K2400_INVALID_SETTINGS",
                "output_off_between_measurements",
            )
        operation_timeout = self._finite_number(
            operation_timeout_seconds,
            "operation_timeout_seconds",
        )
        # 最坏路径逐项计数：Measure 包含 9 项配置读回、输出 ON/OFF 读回、READ 和
        # compliance；Apply 包含连接识别、完整配置/读回、两次输出关闭和错误队列。
        # 每次 I/O 都按耗尽 VISA timeout 计算，避免放行注定超过核心 deadline 的设置。
        measure_estimate = (
            settle + io_timeout * 15.0 + _MEASURE_CLEANUP_RESERVE_SECONDS
        )
        apply_estimate = io_timeout * 30.0 + _MEASURE_CLEANUP_RESERVE_SECONDS
        estimated = max(measure_estimate, apply_estimate)
        if estimated >= operation_timeout:
            raise ModuleError(
                "Keithley 2400 settle/I/O settings may exceed the core operation "
                f"timeout ({estimated:.3g} s >= {operation_timeout:.3g} s)",
                "K2400_INVALID_SETTINGS",
                "operation_timeout_seconds",
            )
        return {
            "resource": resource,
            "io_timeout_seconds": io_timeout,
            "source_mode": mode,
            "source_current": source_current,
            "voltage_compliance": voltage_compliance,
            "source_voltage": source_voltage,
            "current_compliance": current_compliance,
            "sense_mode": sense,
            "nplc": nplc,
            "settle_seconds": settle,
            "output_off_between_measurements": output_off,
        }

    @staticmethod
    def _quantity(value: object, unit: str, field: str) -> float:
        try:
            return parse_quantity(value, expected_unit=unit)
        except ValueError as exc:
            raise ModuleError(
                f"{field}: {exc}",
                "K2400_INVALID_SETTINGS",
                field,
            ) from exc

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool):
            raise ModuleError(
                f"{field} must be numeric",
                "K2400_INVALID_SETTINGS",
                field,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{field} must be numeric",
                "K2400_INVALID_SETTINGS",
                field,
            ) from exc
        if not math.isfinite(result):
            raise ModuleError(
                f"{field} must be finite",
                "K2400_INVALID_SETTINGS",
                field,
            )
        return result

    @staticmethod
    def _invalid_range(field: str, minimum: float, maximum: float) -> None:
        raise ModuleError(
            f"{field} must be in ({minimum:g}, {maximum:g}] or the signed "
            "equivalent where applicable",
            "K2400_INVALID_SETTINGS",
            field,
        )


Module = Keithley2400Backend

__all__ = ["Keithley2400Backend", "Module"]
