"""Keithley 2614B 双通道恒流/恒压电阻测量后端。

2614B 使用 TSP 而不是传统 SCPI 子系统。模块不调用 ``reset()``，避免清除操作员在
仪表上建立的其他现场设置；只配置 SMU A/B 完成本次测量所需的 source、limit、sense、
NPLC、autorange 和高阻输出关闭模式，并逐项通过 ``print(...)`` 读回。
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from labcontrol.module_api import (
    ModuleError,
    ModuleAPI,
    ModuleWarning,
)

from .constants import (
    CHANNELS,
    DEVICE_HIGH_CURRENT_MAX_VOLTAGE_LIMIT_V,
    DEVICE_HIGH_CURRENT_THRESHOLD_A,
    DEVICE_HIGH_VOLTAGE_MAX_CURRENT_LIMIT_A,
    DEVICE_HIGH_VOLTAGE_THRESHOLD_V,
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
    default_channel_settings,
    default_settings,
)
from .quantities import parse_quantity


_READING_SENTINEL = 9.0e36
_CLEANUP_RESERVE_SECONDS = 4.0
_MEASUREMENT_SPLIT = re.compile(r"[\s,]+")


class InstrumentTransport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


TransportFactory = Callable[[str, float], InstrumentTransport]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleAPI, float], None]


class PyVisaTransport:
    """2614B GPIB/TSP 的有限超时 PyVISA 适配器。"""

    def __init__(self, resource_name: str, timeout_seconds: float) -> None:
        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        try:
            self._instrument = self._manager.open_resource(resource_name)
            self._instrument.timeout = max(1, int(timeout_seconds * 1000))
            self._instrument.read_termination = "\n"
            self._instrument.write_termination = "\n"
        except Exception:
            self._manager.close()
            raise

    @staticmethod
    def list_resources() -> tuple[str, ...]:
        pyvisa = importlib.import_module("pyvisa")
        manager = pyvisa.ResourceManager()
        try:
            resources = tuple(str(item) for item in manager.list_resources())
        finally:
            manager.close()
        return tuple(
            sorted(
                {item for item in resources if item.upper().startswith("GPIB")},
                key=str.casefold,
            )
        )

    def write(self, command: str) -> None:
        self._instrument.write(command)

    def query(self, command: str) -> str:
        return str(self._instrument.query(command))

    def close(self) -> None:
        try:
            self._instrument.close()
        finally:
            self._manager.close()


class Keithley2614BBackend:
    """SMU A/B 并行偏置、顺序读取和统一安全清理状态机。"""

    columns = {
        "R1": "Ohm",
        "Voltage1": "V",
        "Current1": "A",
        "StatusCode1": "",
        "R2": "Ohm",
        "Voltage2": "V",
        "Current2": "A",
        "StatusCode2": "",
    }

    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = transport_factory or PyVisaTransport
        self._resource_lister = resource_lister or PyVisaTransport.list_resources
        self._waiter = waiter or (
            lambda api, seconds: api.sleep(seconds)
        )
        self.transport: InstrumentTransport | None = None
        self.desired_settings: dict[str, Any] = default_settings()
        self.applied_settings: dict[str, Any] | None = None
        self.available_resources: tuple[str, ...] = ()
        self.identity = ""
        self.sequence_active = False
        self.output_states = {"ch1": "Unknown", "ch2": "Unknown"}
        self.last_status = "Idle"
        self.last_channel = "-"
        self.last_resistance: float | None = None
        self.last_voltage: float | None = None
        self.last_current: float | None = None

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 只加载 desired settings 并发现 GPIB 地址。"""

        self.desired_settings = self._normalized_settings(
            default_settings(),
            require_resource=False,
            operation_timeout_seconds=api.timeout,
        )
        try:
            self.available_resources = tuple(
                sorted(set(self._resource_lister()), key=str.casefold)
            )
            api.warn("K2614B_RESOURCE_DISCOVERY_FAILED", None)
        except Exception as exc:
            self.available_resources = ()
            api.warn(
                "K2614B_RESOURCE_DISCOVERY_FAILED",
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
        """关闭两个通道，建立 HIGH_Z 基线，再配置所有 Enabled 通道。"""

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
            self._configure_high_impedance_off(api)
            for key, smu, _number in CHANNELS:
                if normalized["channels"][key]["enabled"]:
                    self._configure_channel(
                        key,
                        smu,
                        normalized["channels"][key],
                        api,
                    )
            self._enter_safe_state(api)
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self._close_transport_silently()
            if cleanup:
                raise ModuleError(
                    "2614B Apply failed and both outputs could not be confirmed "
                    f"OFF: {cleanup}",
                    "K2614B_SAFE_STATE_UNCONFIRMED",
                    "configure",
                ) from exc
            raise
        self.applied_settings = deepcopy(normalized)
        self.last_status = "Settings applied - SMU A/B output off"
        status = self._status()
        api.status(status)
        return status

    def _run_start(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        settings = self._require_applied()
        self._enter_safe_state(api)
        self._verify_high_impedance_off(api)
        for key, smu, _number in self._enabled_channels(settings):
            self._verify_channel(
                key,
                smu,
                settings["channels"][key],
                api,
            )
        self.sequence_active = True
        self.last_status = "Sequence ready - SMU A/B output off"
        status = self._status()
        api.status(status)
        return status

    def measure(self, slot: int, api: ModuleAPI) -> Mapping[str, Any]:
        """同时偏置 Enabled 通道，一次读取 A/B 全部列并只写一行。"""

        del slot
        settings = self._require_ready()
        channels = self._enabled_channels(settings)
        output_off = bool(
            settings["output_off_between_measurements"]
        )
        readings: list[tuple[str, int, float, float, bool]] = []
        try:
            api.sleep(0)
            if output_off:
                self._enter_safe_state(api)
            for key, smu, _number in channels:
                self._verify_channel(
                    key,
                    smu,
                    settings["channels"][key],
                    api,
                    require_output_off=output_off,
                )
            # 打开顺序固定为 A→B。任何一步失败都进入下面的直接双通道关闭路径。
            for key, smu, _number in channels:
                self._set_channel_output(key, smu, True, api)
            self._waiter(api, float(settings["settle_seconds"]))
            for key, smu, number in channels:
                voltage, current, compliance = self._read_channel(smu, api)
                readings.append((key, number, voltage, current, compliance))
                api.sleep(0)
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "2614B measurement was interrupted and both outputs could "
                    f"not be confirmed OFF: {cleanup}",
                    "K2614B_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise

        try:
            if output_off:
                self._enter_safe_state(api)
            else:
                # 行间保持不是“省略安全确认”。每次采样完成仍逐通道查询实际输出，
                # 防止联锁或前面板动作已经关闭某一路却让状态页继续显示 retained。
                for key, smu, _number in channels:
                    if not self._query_bool(
                        f"print({smu}.source.output == {smu}.OUTPUT_ON)",
                        api,
                    ):
                        raise ModuleError(
                            f"2614B SMU {key.upper()} output turned off "
                            "unexpectedly while row-boundary retention was enabled",
                            "K2614B_OUTPUT_MISMATCH",
                            key,
                        )
                    self.output_states[key] = "On"
        except Exception as exc:
            cleanup = self._best_effort_safe_state()
            self.sequence_active = False
            if cleanup:
                raise ModuleError(
                    "2614B readings completed but a defined output state could "
                    f"not be confirmed: {cleanup}",
                    "K2614B_SAFE_STATE_UNCONFIRMED",
                    "measure",
                ) from exc
            raise

        row: dict[str, Any] = {}
        for key, number, voltage, current, compliance in readings:
            status_code, issue = self._classify_reading(
                voltage,
                current,
                compliance,
            )
            row[f"StatusCode{number}"] = status_code
            if status_code == STATUS_CODE_NORMAL:
                resistance = voltage / current
                row.update(
                    {
                        f"R{number}": resistance,
                        f"Voltage{number}": voltage,
                        f"Current{number}": current,
                    }
                )
                api.warn("K2614B_READING_WARNING", None, key)
                self.last_resistance = resistance
                self.last_voltage = voltage
                self.last_current = current
                self.last_status = (
                    f"{key.upper()} normal - "
                    + ("outputs off" if output_off else "outputs retained")
                )
            else:
                api.warn(
                    "K2614B_READING_WARNING",
                    f"Keithley 2614B {key.upper()} reading is not valid: {issue}",
                    key,
                )
                self.last_resistance = None
                self.last_voltage = None
                self.last_current = None
                self.last_status = (
                    f"{key.upper()} data warning ({status_code}) - "
                    + ("outputs off" if output_off else "outputs retained")
                )
            self.last_channel = str(number)
            api.status(self._status())
        return row

    def _run_end(
        self,
        reason: str,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        self.sequence_active = False
        self._enter_safe_state(api)
        self.last_status = f"Sequence {reason} - SMU A/B output off"
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
            "Disabled - output state unconfirmed" if failure else "Disabled"
        )
        status = self._status()
        api.status(status)
        if failure is not None:
            if isinstance(failure, ModuleError):
                raise failure
            raise ModuleError(
                f"2614B shutdown failed: {type(failure).__name__}: {failure}",
                "K2614B_SHUTDOWN_FAILED",
            ) from failure
        return status

    def _read_status(
        self,
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        if self.transport is not None:
            states: list[str] = []
            for key, smu, _number in CHANNELS:
                enabled = self._query_bool(
                    f"print({smu}.source.output == {smu}.OUTPUT_ON)",
                    api,
                )
                self.output_states[key] = "On" if enabled else "Off"
                states.append(f"{smu.upper()} {'on' if enabled else 'off'}")
            self.last_status = "Connected / " + " / ".join(states)
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
                    "K2614B_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            api.warn("K2614B_RESOURCE_DISCOVERY_FAILED", None)
        elif action == "test_connection":
            candidate = payload.get("settings", self.desired_settings)
            if not isinstance(candidate, Mapping):
                raise ModuleError(
                    "Test Connection settings must be a mapping",
                    "K2614B_INVALID_SETTINGS",
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
            self.last_status = "SMU A/B output off"
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
                    "K2614B_INVALID_ACTION",
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
                f"Could not open 2614B at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K2614B_CONNECTION_FAILED",
                resource,
            ) from exc
        try:
            identity = self._query("*IDN?", api)
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
                self._validate_identity(self._query("*IDN?", api))
                return
        try:
            temporary = self._transport_factory(resource, timeout)
        except Exception as exc:
            raise ModuleError(
                f"Could not open 2614B at {resource}: "
                f"{type(exc).__name__}: {exc}",
                "K2614B_CONNECTION_FAILED",
                resource,
            ) from exc
        try:
            api.sleep(0)
            self._validate_identity(str(temporary.query("*IDN?")).strip())
            api.sleep(0)
        except ModuleError:
            raise
        except Exception as exc:
            raise ModuleError(
                f"2614B identity query failed: {type(exc).__name__}: {exc}",
                "K2614B_IO_FAILED",
                "*IDN?",
            ) from exc
        finally:
            temporary.close()

    @staticmethod
    def _validate_identity(identity: str) -> None:
        normalized = " ".join(identity.upper().replace(",", " ").split())
        if "KEITHLEY" not in normalized or "2614B" not in normalized:
            raise ModuleError(
                f"Expected Keithley Model 2614B, received {identity!r}",
                "K2614B_IDENTITY_MISMATCH",
                "*IDN?",
            )

    def _configure_high_impedance_off(
        self,
        api: ModuleAPI,
    ) -> None:
        for key, smu, _number in CHANNELS:
            self._write(
                f"{smu}.source.offmode = {smu}.OUTPUT_HIGH_Z",
                api,
            )
            high_z = self._query_bool(
                f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)",
                api,
            )
            if not high_z:
                self._settings_mismatch(f"{key}.offmode", "HIGH_Z", "other")

    def _verify_high_impedance_off(
        self,
        api: ModuleAPI,
    ) -> None:
        for key, smu, _number in CHANNELS:
            if not self._query_bool(
                f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)",
                api,
            ):
                self._settings_mismatch(f"{key}.offmode", "HIGH_Z", "other")

    def _configure_channel(
        self,
        key: str,
        smu: str,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> None:
        self._set_channel_output(key, smu, False, api)
        self._write(
            f"{smu}.source.offmode = {smu}.OUTPUT_HIGH_Z",
            api,
        )
        mode = str(settings["source_mode"])
        if mode == SOURCE_CURRENT:
            self._write(
                f"{smu}.source.func = {smu}.OUTPUT_DCAMPS",
                api,
            )
            self._write(
                f"{smu}.source.autorangei = {smu}.AUTORANGE_ON",
                api,
            )
            self._write(f"{smu}.source.leveli = 0", api)
            self._write(
                f"{smu}.source.limitv = {self._tsp(settings['voltage_limit'])}",
                api,
            )
        else:
            self._write(
                f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS",
                api,
            )
            self._write(
                f"{smu}.source.autorangev = {smu}.AUTORANGE_ON",
                api,
            )
            self._write(f"{smu}.source.levelv = 0", api)
            self._write(
                f"{smu}.source.limiti = {self._tsp(settings['current_limit'])}",
                api,
            )
        self._write(
            f"{smu}.measure.autorangev = {smu}.AUTORANGE_ON",
            api,
        )
        self._write(
            f"{smu}.measure.autorangei = {smu}.AUTORANGE_ON",
            api,
        )
        self._write(
            f"{smu}.measure.nplc = {self._tsp(settings['nplc'])}",
            api,
        )
        self._write(
            f"{smu}.sense = {smu}.SENSE_REMOTE"
            if settings["sense_mode"] == SENSE_4WIRE
            else f"{smu}.sense = {smu}.SENSE_LOCAL",
            api,
        )
        if mode == SOURCE_CURRENT:
            self._write(
                f"{smu}.source.leveli = {self._tsp(settings['source_current'])}",
                api,
            )
        else:
            self._write(
                f"{smu}.source.levelv = {self._tsp(settings['source_voltage'])}",
                api,
            )
        self._verify_channel(key, smu, settings, api)

    def _verify_channel(
        self,
        key: str,
        smu: str,
        settings: Mapping[str, Any],
        api: ModuleAPI,
        *,
        require_output_off: bool = True,
    ) -> None:
        output_on = self._query_bool(
            f"print({smu}.source.output == {smu}.OUTPUT_ON)",
            api,
        )
        self.output_states[key] = "On" if output_on else "Off"
        if require_output_off and output_on:
            raise ModuleError(
                f"{smu.upper()} output is ON before measurement",
                "K2614B_OUTPUT_MISMATCH",
                key,
            )
        if not self._query_bool(
            f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)",
            api,
        ):
            self._settings_mismatch(f"{key}.offmode", "HIGH_Z", "other")
        mode = str(settings["source_mode"])
        expected_function = (
            f"{smu}.OUTPUT_DCAMPS"
            if mode == SOURCE_CURRENT
            else f"{smu}.OUTPUT_DCVOLTS"
        )
        if not self._query_bool(
            f"print({smu}.source.func == {expected_function})",
            api,
        ):
            self._settings_mismatch(f"{key}.source_mode", mode, "other")
        if mode == SOURCE_CURRENT:
            self._expect_number(
                f"print({smu}.source.leveli)",
                float(settings["source_current"]),
                f"{key}.source_current",
                api,
            )
            self._expect_number(
                f"print({smu}.source.limitv)",
                float(settings["voltage_limit"]),
                f"{key}.voltage_limit",
                api,
            )
            source_autorange = "autorangei"
        else:
            self._expect_number(
                f"print({smu}.source.levelv)",
                float(settings["source_voltage"]),
                f"{key}.source_voltage",
                api,
            )
            self._expect_number(
                f"print({smu}.source.limiti)",
                float(settings["current_limit"]),
                f"{key}.current_limit",
                api,
            )
            source_autorange = "autorangev"
        if not self._query_bool(
            f"print({smu}.source.{source_autorange} == {smu}.AUTORANGE_ON)",
            api,
        ):
            self._settings_mismatch(
                f"{key}.{source_autorange}", "AUTORANGE_ON", "other"
            )
        for measure_range in ("autorangev", "autorangei"):
            if not self._query_bool(
                f"print({smu}.measure.{measure_range} == {smu}.AUTORANGE_ON)",
                api,
            ):
                self._settings_mismatch(
                    f"{key}.measure.{measure_range}",
                    "AUTORANGE_ON",
                    "other",
                )
        self._expect_number(
            f"print({smu}.measure.nplc)",
            float(settings["nplc"]),
            f"{key}.nplc",
            api,
        )
        remote = self._query_bool(
            f"print({smu}.sense == {smu}.SENSE_REMOTE)",
            api,
        )
        expected_remote = settings["sense_mode"] == SENSE_4WIRE
        if remote != expected_remote:
            self._settings_mismatch(
                f"{key}.sense_mode", expected_remote, remote
            )

    def _read_channel(
        self,
        smu: str,
        api: ModuleAPI,
    ) -> tuple[float, float, bool]:
        # 两个测量函数处于同一条 TSP 请求，减少主机往返；返回顺序固定为 V、I、limit。
        reply = self._query(
            f"print({smu}.measure.v(), {smu}.measure.i(), "
            f"{smu}.source.compliance)",
            api,
        )
        parts = [item for item in _MEASUREMENT_SPLIT.split(reply.strip()) if item]
        if len(parts) != 3:
            raise ModuleError(
                f"{smu.upper()} returned {reply!r}; expected voltage, current, "
                "compliance",
                "K2614B_INVALID_RESPONSE",
                smu,
            )
        try:
            voltage = float(parts[0])
            current = float(parts[1])
        except ValueError as exc:
            raise ModuleError(
                f"{smu.upper()} returned non-numeric V/I data: {reply!r}",
                "K2614B_INVALID_RESPONSE",
                smu,
            ) from exc
        compliance = self._parse_bool(parts[2], smu)
        return voltage, current, compliance

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

    def _set_channel_output(
        self,
        key: str,
        smu: str,
        enabled: bool,
        api: ModuleAPI,
    ) -> None:
        self._write(
            f"{smu}.source.output = "
            f"{smu}.{'OUTPUT_ON' if enabled else 'OUTPUT_OFF'}",
            api,
        )
        actual = self._query_bool(
            f"print({smu}.source.output == {smu}.OUTPUT_ON)",
            api,
        )
        self.output_states[key] = "On" if actual else "Off"
        if actual != enabled:
            raise ModuleError(
                f"{smu.upper()} output readback mismatch; check the physical "
                "interlock for high-voltage ranges",
                "K2614B_OUTPUT_MISMATCH",
                key,
            )

    def _enter_safe_state(self, api: ModuleAPI) -> None:
        failures: list[str] = []
        for key, smu, _number in CHANNELS:
            try:
                self._set_channel_output(key, smu, False, api)
            except Exception as exc:
                failures.append(f"{smu}: {type(exc).__name__}: {exc}")
        if failures:
            raise ModuleError(
                "2614B output-off could not be confirmed: " + "; ".join(failures),
                "K2614B_SAFE_STATE_UNCONFIRMED",
            )

    def _best_effort_safe_state(self) -> str | None:
        """取消时不经过 checkpoint，始终尝试 A/B 两个输出。"""

        if self.transport is None:
            return None
        failures: list[str] = []
        for key, smu, _number in CHANNELS:
            try:
                self.transport.write(
                    f"{smu}.source.output = {smu}.OUTPUT_OFF"
                )
                reply = self.transport.query(
                    f"print({smu}.source.output == {smu}.OUTPUT_ON)"
                )
                enabled = self._parse_bool(reply, smu)
                self.output_states[key] = "On" if enabled else "Off"
                if enabled:
                    failures.append(f"{smu} still reports output ON")
            except Exception as exc:
                failures.append(f"{smu}: {type(exc).__name__}: {exc}")
        return "; ".join(failures) or None

    def _write(self, command: str, api: ModuleAPI) -> None:
        transport = self._require_transport()
        api.sleep(0)
        try:
            transport.write(command)
        except Exception as exc:
            raise ModuleError(
                f"2614B write failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K2614B_IO_FAILED",
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
                f"2614B query failed for {command!r}: "
                f"{type(exc).__name__}: {exc}",
                "K2614B_IO_FAILED",
                command,
            ) from exc
        api.sleep(0)
        if not reply:
            raise ModuleError(
                f"2614B returned an empty response for {command!r}",
                "K2614B_INVALID_RESPONSE",
                command,
            )
        return reply

    def _query_bool(
        self,
        command: str,
        api: ModuleAPI,
    ) -> bool:
        return self._parse_bool(self._query(command, api), command)

    @staticmethod
    def _parse_bool(value: object, api: str) -> bool:
        token = str(value).strip().strip('"').casefold()
        if token in {"true", "1", "on"}:
            return True
        if token in {"false", "0", "off"}:
            return False
        raise ModuleError(
            f"2614B returned invalid boolean {value!r}",
            "K2614B_INVALID_RESPONSE",
            api,
        )

    def _expect_number(
        self,
        command: str,
        expected: float,
        field: str,
        api: ModuleAPI,
    ) -> None:
        reply = self._query(command, api)
        try:
            actual = float(reply)
        except ValueError as exc:
            raise ModuleError(
                f"2614B returned non-numeric readback {reply!r}",
                "K2614B_INVALID_RESPONSE",
                command,
            ) from exc
        tolerance = max(1.0e-12, abs(expected) * 1.0e-6)
        if not math.isfinite(actual) or abs(actual - expected) > tolerance:
            self._settings_mismatch(field, expected, actual)

    @staticmethod
    def _settings_mismatch(field: str, expected: object, actual: object) -> None:
        raise ModuleError(
            f"2614B {field} readback mismatch: expected {expected!r}, "
            f"received {actual!r}",
            "K2614B_SETTINGS_MISMATCH",
            field,
        )

    @staticmethod
    def _tsp(value: object) -> str:
        return f"{float(value):.12g}"

    def _require_transport(self) -> InstrumentTransport:
        if self.transport is None:
            raise ModuleError(
                "Keithley 2614B is not connected; Apply Settings first",
                "K2614B_NOT_CONNECTED",
            )
        return self.transport

    def _require_applied(self) -> dict[str, Any]:
        if self.applied_settings is None or self.transport is None:
            raise ModuleError(
                "Keithley 2614B settings have not been applied",
                "K2614B_NOT_APPLIED",
            )
        return deepcopy(self.applied_settings)

    def _require_ready(self) -> dict[str, Any]:
        settings = self._require_applied()
        if not self.sequence_active:
            raise ModuleError(
                "Keithley 2614B sequence has not begun",
                "K2614B_SEQUENCE_NOT_ACTIVE",
            )
        return settings

    @staticmethod
    def _enabled_channels(
        settings: Mapping[str, Any],
    ) -> list[tuple[str, str, int]]:
        return [
            (key, smu, number)
            for key, smu, number in CHANNELS
            if settings["channels"][key]["enabled"]
        ]

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
            "SMU A Output": self.output_states["ch1"],
            "SMU B Output": self.output_states["ch2"],
            "Last Channel": self.last_channel,
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
                "Keithley 2614B settings must be a mapping",
                "K2614B_INVALID_SETTINGS",
                "settings",
            )
        defaults = default_settings()
        resource = str(supplied.get("resource", defaults["resource"])).strip()
        if require_resource and not resource:
            raise ModuleError(
                "Select or enter a GPIB VISA resource",
                "K2614B_INVALID_SETTINGS",
                "resource",
            )
        if "\n" in resource or "\r" in resource:
            raise ModuleError(
                "VISA resource must be a single line",
                "K2614B_INVALID_SETTINGS",
                "resource",
            )
        io_timeout = self._finite_number(
            supplied.get("io_timeout_seconds", defaults["io_timeout_seconds"]),
            "io_timeout_seconds",
        )
        settle = self._finite_number(
            supplied.get("settle_seconds", defaults["settle_seconds"]),
            "settle_seconds",
        )
        if not 0.1 <= io_timeout <= 30.0:
            self._invalid_range("io_timeout_seconds", 0.1, 30.0)
        if not 0.0 <= settle <= 3600.0:
            self._invalid_range("settle_seconds", 0.0, 3600.0)
        output_off = supplied.get(
            "output_off_between_measurements",
            defaults["output_off_between_measurements"],
        )
        if not isinstance(output_off, bool):
            raise ModuleError(
                "output_off_between_measurements must be true or false",
                "K2614B_INVALID_SETTINGS",
                "output_off_between_measurements",
            )

        raw_channels = supplied.get("channels", defaults["channels"])
        if not isinstance(raw_channels, Mapping):
            raise ModuleError(
                "channels must be a mapping",
                "K2614B_INVALID_SETTINGS",
                "channels",
            )
        channels: dict[str, dict[str, Any]] = {}
        enabled_count = 0
        for key, _smu, _number in CHANNELS:
            base = default_channel_settings()
            raw = raw_channels.get(key, defaults["channels"][key])
            if not isinstance(raw, Mapping):
                raise ModuleError(
                    f"{key} must be a mapping",
                    "K2614B_INVALID_SETTINGS",
                    key,
                )
            for field in base:
                if field in raw:
                    base[field] = raw[field]
            enabled = base["enabled"]
            if not isinstance(enabled, bool):
                raise ModuleError(
                    f"{key}.enabled must be true or false",
                    "K2614B_INVALID_SETTINGS",
                    f"{key}.enabled",
                )
            enabled_count += int(enabled)
            mode = str(base["source_mode"]).strip().casefold()
            if mode not in {SOURCE_CURRENT, SOURCE_VOLTAGE}:
                raise ModuleError(
                    f"{key}.source_mode must be current or voltage",
                    "K2614B_INVALID_SETTINGS",
                    f"{key}.source_mode",
                )
            sense = str(base["sense_mode"]).strip().casefold()
            if sense not in {SENSE_2WIRE, SENSE_4WIRE}:
                raise ModuleError(
                    f"{key}.sense_mode must be 2wire or 4wire",
                    "K2614B_INVALID_SETTINGS",
                    f"{key}.sense_mode",
                )
            source_current = self._quantity(
                base["source_current"], "A", f"{key}.source_current"
            )
            voltage_limit = self._quantity(
                base["voltage_limit"], "V", f"{key}.voltage_limit"
            )
            source_voltage = self._quantity(
                base["source_voltage"], "V", f"{key}.source_voltage"
            )
            current_limit = self._quantity(
                base["current_limit"], "A", f"{key}.current_limit"
            )
            nplc = self._finite_number(base["nplc"], f"{key}.nplc")
            if abs(source_current) > DEVICE_MAX_CURRENT_A:
                self._invalid_range(
                    f"{key}.source_current",
                    -DEVICE_MAX_CURRENT_A,
                    DEVICE_MAX_CURRENT_A,
                )
            if abs(source_voltage) > DEVICE_MAX_VOLTAGE_V:
                self._invalid_range(
                    f"{key}.source_voltage",
                    -DEVICE_MAX_VOLTAGE_V,
                    DEVICE_MAX_VOLTAGE_V,
                )
            if not 0 < voltage_limit <= 200.0:
                self._invalid_range(f"{key}.voltage_limit", 0.0, 200.0)
            if not 0 < current_limit <= 1.5:
                self._invalid_range(f"{key}.current_limit", 0.0, 1.5)
            if (
                abs(source_current) > DEVICE_HIGH_CURRENT_THRESHOLD_A
                and voltage_limit
                > DEVICE_HIGH_CURRENT_MAX_VOLTAGE_LIMIT_V
            ):
                raise ModuleError(
                    f"{key}.voltage_limit must be <= 20 V when source current "
                    "exceeds 100 mA",
                    "K2614B_INVALID_SETTINGS",
                    f"{key}.voltage_limit",
                )
            if (
                abs(source_voltage) > DEVICE_HIGH_VOLTAGE_THRESHOLD_V
                and current_limit > DEVICE_HIGH_VOLTAGE_MAX_CURRENT_LIMIT_A
            ):
                raise ModuleError(
                    f"{key}.current_limit must be <= 100 mA when source voltage "
                    "exceeds 20 V",
                    "K2614B_INVALID_SETTINGS",
                    f"{key}.current_limit",
                )
            if not 0.001 <= nplc <= 25.0:
                self._invalid_range(f"{key}.nplc", 0.001, 25.0)
            channels[key] = {
                "enabled": enabled,
                "source_mode": mode,
                "source_current": source_current,
                "voltage_limit": voltage_limit,
                "source_voltage": source_voltage,
                "current_limit": current_limit,
                "sense_mode": sense,
                "nplc": nplc,
            }
        if enabled_count == 0:
            raise ModuleError(
                "Enable at least one 2614B channel",
                "K2614B_INVALID_SETTINGS",
                "channels",
            )
        operation_timeout = self._finite_number(
            operation_timeout_seconds, "operation_timeout_seconds"
        )
        # 固定成本包括 A/B 双通道安全关闭与 HIGH_Z 读回；每个 Enabled 通道另有
        # 完整配置读回、输出切换和测量。按所有 TSP 往返都耗尽 VISA timeout 估算，
        # 两通道默认 2 s timeout 时仍在核心 120 s deadline 内（118 s 上界）。
        measure_estimate = (
            settle
            + io_timeout * (8.0 + 13.0 * enabled_count)
            + _CLEANUP_RESERVE_SECONDS
        )
        apply_estimate = (
            io_timeout * (13.0 + 22.0 * enabled_count)
            + _CLEANUP_RESERVE_SECONDS
        )
        estimated = max(measure_estimate, apply_estimate)
        if estimated >= operation_timeout:
            raise ModuleError(
                "Keithley 2614B channel/I/O settings may exceed the core "
                f"operation timeout ({estimated:.3g} s >= "
                f"{operation_timeout:.3g} s)",
                "K2614B_INVALID_SETTINGS",
                "operation_timeout_seconds",
            )
        return {
            "resource": resource,
            "io_timeout_seconds": io_timeout,
            "settle_seconds": settle,
            "output_off_between_measurements": output_off,
            "channels": channels,
        }

    @staticmethod
    def _quantity(value: object, unit: str, field: str) -> float:
        try:
            return parse_quantity(value, expected_unit=unit)
        except ValueError as exc:
            raise ModuleError(
                f"{field}: {exc}",
                "K2614B_INVALID_SETTINGS",
                field,
            ) from exc

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool):
            raise ModuleError(
                f"{field} must be numeric",
                "K2614B_INVALID_SETTINGS",
                field,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{field} must be numeric",
                "K2614B_INVALID_SETTINGS",
                field,
            ) from exc
        if not math.isfinite(result):
            raise ModuleError(
                f"{field} must be finite",
                "K2614B_INVALID_SETTINGS",
                field,
            )
        return result

    @staticmethod
    def _invalid_range(field: str, minimum: float, maximum: float) -> None:
        raise ModuleError(
            f"{field} must be between {minimum:g} and {maximum:g}",
            "K2614B_INVALID_SETTINGS",
            field,
        )


Module = Keithley2614BBackend

__all__ = ["Keithley2614BBackend", "Module", "PyVisaTransport"]
