"""Keithley 6517B 恒压两线高电阻测量后端。

本模块固定使用手册定义的 FVMI 接线。关键安全前置条件不是“命令已经发送”，而是：

1. V-source 处于 standby；
2. zero check 为 ON；
3. ``SOUR:VOLT:MCON?`` 明确返回 ON；
4. V-source range、hardware voltage limit 和源值全部与设置一致。

只有这些条件均读回成功，Measure 才允许进入 operate。任何异常路径都会先直接请求
standby 和 zero-check，再把控制权交还核心。
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
    SOURCE_RANGES,
    STATUS_CODE_COMPLIANCE,
    STATUS_CODE_INVALID_READING,
    STATUS_CODE_NORMAL,
    STATUS_CODE_OVER_RANGE,
    default_settings,
)
from .quantities import parse_quantity
from . import keithley_6517b as instrument


_READING_SENTINEL = 9.0e36
_CLEANUP_RESERVE_SECONDS = 4.0
TransportFactory = Callable[[str, float], instrument.Transport]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleAPI, float], None]


class Keithley6517BBackend:
    """6517B 的连接、METER-CONNECT、测量和高压安全状态机。"""

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
        self.last_meter_connect = "Unknown"
        self.last_resistive_limit = "Unknown"
        self.last_zero_check = "Unknown"
        self.last_output = "Unknown"
        self.last_resistance: float | None = None
        self.last_voltage: float | None = None
        self.last_current: float | None = None

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 只发现 GPIB；不连接、不改变 METER-CONNECT 或 V-source。"""

        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_resource=False,
            operation_timeout_seconds=api.timeout,
        )
        try:
            self.available_resources = tuple(
                sorted(set(self._resource_lister()), key=str.casefold)
            )
            api.warn("K6517B_RESOURCE_DISCOVERY_FAILED", None)
        except Exception as exc:
            self.available_resources = ()
            api.warn(
                "K6517B_RESOURCE_DISCOVERY_FAILED",
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
        """在 standby/zero-check 条件下配置并读回全部关键设置。"""

        normalized = self._normalized_settings(
            settings,
            require_resource=True,
            operation_timeout_seconds=api.timeout,
        )
        self.desired_settings = deepcopy(normalized)
        self.applied_settings = None
        try:
            if self.transport is not None:
                self._enter_safe_state(api)
                self._close_transport()
            self._connect(normalized, api)
            self._enter_safe_state(api)
            self._write(instrument.CLEAR_STATUS, api)
            self._configure(normalized, api)
            self._enter_safe_state(api)
            self._raise_if_instrument_error(api)
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self._close_transport_silently()
            if cleanup:
                raise ModuleError(
                    "6517B Apply failed and standby/zero-check could not be "
                    f"confirmed: {cleanup}",
                    "K6517B_SAFE_STATE_UNCONFIRMED",
                    "configure",
                ) from exc
            raise
        self.applied_settings = deepcopy(normalized)
        self.last_status = "Settings applied - standby / zero check on"
        status = self._status()
        api.status(status)
        return status

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        settings = self._require_applied()
        self._enter_safe_state(api)
        self._verify_configuration(settings, api)
        self.sequence_active = True
        self.last_status = "Sequence ready - standby / zero check on"
        status = self._status()
        api.status(status)
        return status

    def measure(self, slot: int, api: ModuleAPI) -> Mapping[str, Any]:
        """关闭 zero check、短时 operate、读数，然后严格恢复安全状态。"""

        del slot
        settings = self._require_ready()
        output_off = bool(
            settings["output_off_between_measurements"]
        )
        current = 0.0
        voltage = 0.0
        reading_status = ""
        compliance = False
        try:
            api.sleep(0)
            self._verify_configuration(settings, api)
            if output_off and not self._query_switch(
                instrument.ZERO_CHECK_QUERY, api
            ):
                raise ModuleError(
                    "6517B zero check must be ON before a measurement",
                    "K6517B_ZERO_CHECK_MISMATCH",
                    instrument.ZERO_CHECK_QUERY,
                )
            self._set_zero_check(False, api)
            # METER-CONNECT 在真正打开高压的最后时刻再次验证，防止前面板在 Verify 后改动。
            if not self._query_switch(instrument.METER_CONNECT_QUERY, api):
                raise ModuleError(
                    "6517B METER-CONNECT is OFF; V-source LO is not connected "
                    "to Ammeter LO",
                    "K6517B_METER_CONNECT_REQUIRED",
                    instrument.METER_CONNECT_QUERY,
                )
            if self._query_switch(instrument.RESISTIVE_LIMIT_QUERY, api):
                raise ModuleError(
                    "6517B resistive current limit is ON; the internal 1 MOhm "
                    "series resistor would be included in V/I",
                    "K6517B_RESISTIVE_LIMIT_MISMATCH",
                    instrument.RESISTIVE_LIMIT_QUERY,
                )
            self._set_output(True, api)
            self._waiter(api, float(settings["settle_seconds"]))
            current, reading_status, voltage = self._read_measurement(api)
            compliance = self._query_switch(instrument.COMPLIANCE_QUERY, api)
            api.sleep(0)
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "6517B measurement was interrupted and standby/zero-check "
                    f"could not be confirmed: {cleanup}",
                    "K6517B_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise

        try:
            if output_off:
                self._enter_safe_state(api)
            else:
                if not self._query_switch(instrument.OUTPUT_QUERY, api):
                    raise ModuleError(
                        "6517B V-source unexpectedly entered standby while "
                        "row-boundary retention was enabled",
                        "K6517B_OUTPUT_MISMATCH",
                        instrument.OUTPUT_QUERY,
                    )
                if self._query_switch(instrument.ZERO_CHECK_QUERY, api):
                    raise ModuleError(
                        "6517B zero check unexpectedly turned ON while "
                        "row-boundary retention was enabled",
                        "K6517B_ZERO_CHECK_MISMATCH",
                        instrument.ZERO_CHECK_QUERY,
                    )
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "6517B reading completed but a defined output state could "
                    f"not be confirmed: {cleanup}",
                    "K6517B_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise

        status_code, issue = self._classify_reading(
            current,
            voltage,
            reading_status,
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
            api.warn("K6517B_READING_WARNING", None)
            self.last_resistance = resistance
            self.last_voltage = voltage
            self.last_current = current
            self.last_status = (
                "Normal - standby / zero check on"
                if output_off
                else "Normal - output retained / zero check off"
            )
        else:
            api.warn(
                "K6517B_READING_WARNING",
                f"Keithley 6517B reading is not valid: {issue}",
            )
            self.last_resistance = None
            self.last_voltage = None
            self.last_current = None
            self.last_status = (
                f"Data warning ({status_code}) - "
                + (
                    "standby / zero check on"
                    if output_off
                    else "output retained / zero check off"
                )
            )
        api.status(self._status())
        return row

    def _run_end(
        self,
        reason: str,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        self.sequence_active = False
        self._enter_safe_state(api)
        self.last_status = f"Sequence {reason} - standby / zero check on"
        status = self._status()
        api.status(status)
        return status

    def close(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        self.sequence_active = False
        failure: Exception | None = None
        try:
            if self.transport is not None:
                self._enter_safe_state(api)
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
                f"6517B shutdown failed: {type(failure).__name__}: {failure}",
                "K6517B_SHUTDOWN_FAILED",
            ) from failure
        return status

    def _read_status(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """只读实际 V-source、zero-check 和 METER-CONNECT 状态。"""

        if self.transport is not None:
            output = self._query_switch(instrument.OUTPUT_QUERY, api)
            zero_check = self._query_switch(instrument.ZERO_CHECK_QUERY, api)
            meter_connect = self._query_switch(
                instrument.METER_CONNECT_QUERY, api
            )
            resistive_limit = self._query_switch(
                instrument.RESISTIVE_LIMIT_QUERY, api
            )
            self.last_resistive_limit = "On" if resistive_limit else "Off"
            self._record_safety_state(output, zero_check, meter_connect)
            self.last_status = (
                f"Connected / output {'operate' if output else 'standby'} / "
                f"zero check {'on' if zero_check else 'off'} / "
                f"METER-CONNECT {'on' if meter_connect else 'off'} / "
                f"resistive limit {'on' if resistive_limit else 'off'}"
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
        if action == "refresh_resources":
            try:
                self.available_resources = tuple(
                    sorted(set(self._resource_lister()), key=str.casefold)
                )
            except Exception as exc:
                raise ModuleWarning(
                    "GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "K6517B_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            api.warn("K6517B_RESOURCE_DISCOVERY_FAILED", None)
        elif action == "test_connection":
            candidate = payload.get("settings", self.desired_settings)
            if not isinstance(candidate, Mapping):
                raise ModuleError(
                    "Test Connection settings must be a mapping",
                    "K6517B_INVALID_SETTINGS",
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
                self._enter_safe_state(api)
            self.last_status = "Standby / zero check on"
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
                    "K6517B_INVALID_ACTION",
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
            self.transport = self._transport_factory(resource, timeout)
        except Exception as exc:
            raise ModuleError(
                f"Could not open 6517B at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K6517B_CONNECTION_FAILED",
                resource,
            ) from exc
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
                f"Could not open 6517B at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K6517B_CONNECTION_FAILED",
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
                f"6517B identity query failed: {type(exc).__name__}: {exc}",
                "K6517B_IO_FAILED",
                instrument.IDENTIFY,
            ) from exc
        finally:
            temporary.close()

    @staticmethod
    def _validate_identity(identity: str) -> None:
        if not instrument.validate_identity(identity):
            raise ModuleError(
                f"Expected Keithley Model 6517B, received {identity!r}",
                "K6517B_IDENTITY_MISMATCH",
                instrument.IDENTIFY,
            )

    def _configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        range_value = SOURCE_RANGES[str(settings["source_range"])]
        for command in instrument.configuration_commands(settings, range_value):
            self._write(command, api)
        self._verify_configuration(settings, api)

    def _verify_configuration(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        range_value = SOURCE_RANGES[str(settings["source_range"])]
        self._expect_number(
            instrument.SOURCE_RANGE_QUERY, range_value, "source_range", api
        )
        self._expect_number(
            instrument.SOURCE_VOLTAGE_QUERY,
            float(settings["source_voltage"]),
            "source_voltage",
            api,
        )
        self._expect_number(
            instrument.VOLTAGE_LIMIT_QUERY,
            float(settings["voltage_limit"]),
            "voltage_limit",
            api,
        )
        if not self._query_switch(instrument.VOLTAGE_LIMIT_STATE_QUERY, api):
            self._settings_mismatch("voltage_limit_state", True, False)
        resistive_limit = self._query_switch(
            instrument.RESISTIVE_LIMIT_QUERY, api
        )
        self.last_resistive_limit = "On" if resistive_limit else "Off"
        if resistive_limit:
            self._settings_mismatch("resistive_current_limit", False, True)
        meter_connect = self._query_switch(
            instrument.METER_CONNECT_QUERY, api
        )
        self.last_meter_connect = "On" if meter_connect else "Off"
        if not meter_connect:
            raise ModuleError(
                "6517B METER-CONNECT readback is OFF; V-source LO is not "
                "connected to Ammeter LO",
                "K6517B_METER_CONNECT_REQUIRED",
                instrument.METER_CONNECT_QUERY,
            )
        function = self._clean_token(
            self._query(instrument.SENSE_FUNCTION_QUERY, api)
        )
        if "CURR" not in function:
            self._settings_mismatch("sense_function", "CURR", function)
        if not self._query_switch(instrument.CURRENT_AUTORANGE_QUERY, api):
            self._settings_mismatch("current_autorange", True, False)
        self._expect_number(
            instrument.CURRENT_NPLC_QUERY,
            float(settings["nplc"]),
            "nplc",
            api,
        )
        elements = {
            self._canonical_element(item)
            for item in self._query(
                instrument.DATA_ELEMENTS_QUERY, api
            ).split(",")
        }
        if elements != {"READ", "STAT", "VSOUR"}:
            self._settings_mismatch(
                "data_elements",
                "READ,STAT,VSOUR",
                ",".join(sorted(elements)),
            )

    def _read_measurement(
        self,
        api: ModuleAPI,
    ) -> tuple[float, str, float]:
        reply = self._query(instrument.READ, api)
        try:
            return instrument.parse_measurement(reply)
        except ValueError as exc:
            raise ModuleError(
                f"6517B {instrument.READ} returned invalid data: {reply!r}",
                "K6517B_INVALID_RESPONSE",
                instrument.READ,
            ) from exc

    @staticmethod
    def _classify_reading(
        current: float,
        voltage: float,
        reading_status: str,
        compliance: bool,
    ) -> tuple[int, str]:
        if compliance:
            return STATUS_CODE_COMPLIANCE, "V-source reached current compliance"
        status = reading_status.strip().upper()
        if status.startswith(("O", "U")):
            return STATUS_CODE_OVER_RANGE, f"instrument status is {reading_status}"
        if not status.startswith("N"):
            return (
                STATUS_CODE_INVALID_READING,
                f"instrument status is {reading_status}",
            )
        if (
            not math.isfinite(current)
            or not math.isfinite(voltage)
            or abs(current) >= _READING_SENTINEL
            or abs(voltage) >= _READING_SENTINEL
        ):
            return STATUS_CODE_OVER_RANGE, "instrument returned overrange data"
        if abs(current) <= 1.0e-30:
            return STATUS_CODE_INVALID_READING, "measured current is zero"
        resistance = voltage / current
        if not math.isfinite(resistance) or abs(resistance) >= _READING_SENTINEL:
            return STATUS_CODE_OVER_RANGE, "calculated resistance is overrange"
        return STATUS_CODE_NORMAL, ""

    def _enter_safe_state(self, api: ModuleAPI) -> None:
        """无论第一步是否失败，都继续尝试第二个安全动作。"""

        failures: list[str] = []
        try:
            self._set_output(False, api)
        except Exception as exc:
            failures.append(f"standby: {type(exc).__name__}: {exc}")
        try:
            self._set_zero_check(True, api)
        except Exception as exc:
            failures.append(f"zero check: {type(exc).__name__}: {exc}")
        if failures:
            raise ModuleError(
                "6517B safe state could not be confirmed: " + "; ".join(failures),
                "K6517B_SAFE_STATE_UNCONFIRMED",
            )

    def _set_output(
        self,
        enabled: bool,
        api: ModuleAPI,
    ) -> None:
        self._write(instrument.output_command(enabled), api)
        actual = self._query_switch(instrument.OUTPUT_QUERY, api)
        self.last_output = "Operate" if actual else "Standby"
        if actual != enabled:
            raise ModuleError(
                "6517B V-source output readback mismatch",
                "K6517B_OUTPUT_MISMATCH",
                instrument.OUTPUT_QUERY,
            )

    def _set_zero_check(
        self,
        enabled: bool,
        api: ModuleAPI,
    ) -> None:
        self._write(instrument.zero_check_command(enabled), api)
        actual = self._query_switch(instrument.ZERO_CHECK_QUERY, api)
        self.last_zero_check = "On" if actual else "Off"
        if actual != enabled:
            raise ModuleError(
                "6517B zero-check readback mismatch",
                "K6517B_ZERO_CHECK_MISMATCH",
                instrument.ZERO_CHECK_QUERY,
            )

    def _best_effort_safe_state(self) -> str | None:
        """Stop/异常路径直接使用 transport，避免取消 checkpoint 阻止高压关闭。"""

        if self.transport is None:
            return None
        failures: list[str] = []
        try:
            self.transport.write(instrument.OUTPUT_OFF)
            output = self._parse_switch(
                self.transport.query(instrument.OUTPUT_QUERY),
                instrument.OUTPUT_QUERY,
            )
            self.last_output = "Operate" if output else "Standby"
            if output:
                failures.append(
                    f"{instrument.OUTPUT_QUERY} still reports operate"
                )
        except Exception as exc:
            failures.append(f"standby: {type(exc).__name__}: {exc}")
        try:
            self.transport.write(instrument.ZERO_CHECK_ON)
            zero = self._parse_switch(
                self.transport.query(instrument.ZERO_CHECK_QUERY),
                instrument.ZERO_CHECK_QUERY,
            )
            self.last_zero_check = "On" if zero else "Off"
            if not zero:
                failures.append(
                    f"{instrument.ZERO_CHECK_QUERY} still reports OFF"
                )
        except Exception as exc:
            failures.append(f"zero check: {type(exc).__name__}: {exc}")
        return "; ".join(failures) or None

    def _record_safety_state(
        self,
        output: bool,
        zero_check: bool,
        meter_connect: bool,
    ) -> None:
        self.last_output = "Operate" if output else "Standby"
        self.last_zero_check = "On" if zero_check else "Off"
        self.last_meter_connect = "On" if meter_connect else "Off"

    def _raise_if_instrument_error(
        self,
        api: ModuleAPI,
    ) -> None:
        reply = self._query(instrument.ERROR_QUERY, api)
        try:
            error_code = instrument.parse_error_code(reply)
        except ValueError as exc:
            raise ModuleError(
                f"6517B returned an invalid error response: {reply!r}",
                "K6517B_INVALID_RESPONSE",
                instrument.ERROR_QUERY,
            ) from exc
        if error_code != 0:
            raise ModuleError(
                f"6517B reported an instrument error: {reply}",
                "K6517B_INSTRUMENT_ERROR",
                instrument.ERROR_QUERY,
            )

    def _write(self, command: str, api: ModuleAPI) -> None:
        transport = self._require_transport()
        api.sleep(0)
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"6517B write failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K6517B_IO_FAILED",
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
                f"6517B query failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K6517B_IO_FAILED",
                command,
            ) from exc
        api.sleep(0)
        if not reply:
            raise ModuleError(
                f"6517B returned an empty response for {command!r}",
                "K6517B_INVALID_RESPONSE",
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
                f"6517B returned invalid switch state {value!r}",
                "K6517B_INVALID_RESPONSE",
                command,
            ) from exc

    @staticmethod
    def _numeric_element(value: str, label: str) -> float:
        try:
            return instrument.parse_numeric_element(value)
        except ValueError as exc:
            raise ModuleError(
                f"6517B returned a non-numeric {label}: {value!r}",
                "K6517B_INVALID_RESPONSE",
                instrument.READ,
            ) from exc

    def _expect_number(
        self,
        command: str,
        expected: float,
        field: str,
        api: ModuleAPI,
    ) -> None:
        actual = self._numeric_element(self._query(command, api), command)
        tolerance = max(1.0e-12, abs(expected) * 1.0e-6)
        if not math.isfinite(actual) or abs(actual - expected) > tolerance:
            self._settings_mismatch(field, expected, actual)

    @staticmethod
    def _canonical_element(value: object) -> str:
        return instrument.canonical_element(value)

    @staticmethod
    def _clean_token(value: object) -> str:
        return instrument.clean_token(value)

    @staticmethod
    def _settings_mismatch(field: str, expected: object, actual: object) -> None:
        raise ModuleError(
            f"6517B {field} readback mismatch: expected {expected!r}, "
            f"received {actual!r}",
            "K6517B_SETTINGS_MISMATCH",
            field,
        )

    @staticmethod
    def _scpi(value: object) -> str:
        return instrument.number(value)

    def _require_transport(self) -> instrument.Transport:
        if self.transport is None:
            raise ModuleError(
                "Keithley 6517B is not connected; Apply Settings first",
                "K6517B_NOT_CONNECTED",
            )
        return self.transport

    def _require_applied(self) -> dict[str, Any]:
        if self.applied_settings is None or self.transport is None:
            raise ModuleError(
                "Keithley 6517B settings have not been applied",
                "K6517B_NOT_APPLIED",
            )
        return deepcopy(self.applied_settings)

    def _require_ready(self) -> dict[str, Any]:
        settings = self._require_applied()
        if not self.sequence_active:
            raise ModuleError(
                "Keithley 6517B sequence has not begun",
                "K6517B_SEQUENCE_NOT_ACTIVE",
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
            "V-source Output": self.last_output,
            "Zero Check": self.last_zero_check,
            "METER-CONNECT": self.last_meter_connect,
            "Resistive Limit": self.last_resistive_limit,
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
                "Keithley 6517B settings must be a mapping",
                "K6517B_INVALID_SETTINGS",
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
                "K6517B_INVALID_SETTINGS",
                "resource",
            )
        if "\n" in resource or "\r" in resource:
            raise ModuleError(
                "VISA resource must be a single line",
                "K6517B_INVALID_SETTINGS",
                "resource",
            )
        range_key = str(merged["source_range"]).strip().casefold()
        if range_key not in SOURCE_RANGES:
            raise ModuleError(
                "source_range must be 100v or 1000v",
                "K6517B_INVALID_SETTINGS",
                "source_range",
            )
        range_value = SOURCE_RANGES[range_key]
        source_voltage = self._quantity(
            merged["source_voltage"], "V", "source_voltage"
        )
        voltage_limit = self._quantity(
            merged["voltage_limit"], "V", "voltage_limit"
        )
        if abs(source_voltage) > range_value:
            raise ModuleError(
                f"source_voltage exceeds the selected {range_value:g} V range",
                "K6517B_INVALID_SETTINGS",
                "source_voltage",
            )
        if not 0 < voltage_limit <= range_value:
            raise ModuleError(
                f"voltage_limit must be > 0 and <= {range_value:g} V",
                "K6517B_INVALID_SETTINGS",
                "voltage_limit",
            )
        if abs(source_voltage) > voltage_limit:
            raise ModuleError(
                "voltage_limit must be at least abs(source_voltage)",
                "K6517B_INVALID_SETTINGS",
                "voltage_limit",
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
                "K6517B_INVALID_SETTINGS",
                "output_off_between_measurements",
            )
        operation_timeout = self._finite_number(
            operation_timeout_seconds, "operation_timeout_seconds"
        )
        # Measure 最坏包含 10 项配置读回、最后时刻 METER-CONNECT 查询、operate/
        # standby、zero-check、READ 和 compliance；Apply 还包含身份、完整配置和
        # 错误队列。逐项按 VISA timeout 计入核心 lifecycle budget。
        measure_estimate = settle + io_timeout * 23.0 + _CLEANUP_RESERVE_SECONDS
        apply_estimate = io_timeout * 32.0 + _CLEANUP_RESERVE_SECONDS
        estimated = max(measure_estimate, apply_estimate)
        if estimated >= operation_timeout:
            raise ModuleError(
                "Keithley 6517B settle/I/O settings may exceed the core "
                f"operation timeout ({estimated:.3g} s >= "
                f"{operation_timeout:.3g} s)",
                "K6517B_INVALID_SETTINGS",
                "operation_timeout_seconds",
            )
        return {
            "resource": resource,
            "io_timeout_seconds": io_timeout,
            "source_range": range_key,
            "source_voltage": source_voltage,
            "voltage_limit": voltage_limit,
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
                "K6517B_INVALID_SETTINGS",
                field,
            ) from exc

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool):
            raise ModuleError(
                f"{field} must be numeric",
                "K6517B_INVALID_SETTINGS",
                field,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{field} must be numeric",
                "K6517B_INVALID_SETTINGS",
                field,
            ) from exc
        if not math.isfinite(result):
            raise ModuleError(
                f"{field} must be finite",
                "K6517B_INVALID_SETTINGS",
                field,
            )
        return result

    @staticmethod
    def _invalid_range(field: str, minimum: float, maximum: float) -> None:
        raise ModuleError(
            f"{field} must be between {minimum:g} and {maximum:g}",
            "K6517B_INVALID_SETTINGS",
            field,
        )


Module = Keithley6517BBackend

__all__ = ["Keithley6517BBackend", "Module"]
