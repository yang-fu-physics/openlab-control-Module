from __future__ import annotations

import importlib
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from labcontrol.measurement.api import (
    ModuleBackend,
    ModuleError,
    ModuleOperationContext,
    ModuleWarning,
)

from .constants import (
    CURRENT_EXCITATIONS,
    STATUS_BITS,
    default_settings,
)


class InstrumentTransport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


TransportFactory = Callable[
    [str, float],
    InstrumentTransport,
]
ResourceLister = Callable[[], tuple[str, ...]]
Waiter = Callable[[ModuleOperationContext, float], None]


class PyVisaTransport:
    """Small lazy PyVISA adapter kept entirely in the module worker."""

    def __init__(
        self,
        resource_name: str,
        timeout_seconds: float,
    ) -> None:
        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        try:
            self._instrument = self._manager.open_resource(
                resource_name
            )
            self._instrument.timeout = max(
                1,
                int(timeout_seconds * 1000),
            )
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
            resources = tuple(
                str(item)
                for item in manager.list_resources()
            )
        finally:
            manager.close()
        return tuple(
            sorted(
                {
                    item
                    for item in resources
                    if item.upper().startswith("GPIB")
                },
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


class LakeShore372ABackend(ModuleBackend):
    def __init__(
        self,
        transport_factory: TransportFactory | None = None,
        resource_lister: ResourceLister | None = None,
        waiter: Waiter | None = None,
    ) -> None:
        self._transport_factory = (
            transport_factory or PyVisaTransport
        )
        self._resource_lister = (
            resource_lister
            or PyVisaTransport.list_resources
        )
        self._waiter = (
            waiter
            or (
                lambda context, seconds:
                context.interruptible_sleep(seconds)
            )
        )
        self.transport: InstrumentTransport | None = None
        self.desired_settings = default_settings()
        self.applied_settings: dict[str, Any] = {}
        self.identity = ""
        self.sequence_active = False
        self.last_values: dict[str, Any] = {}
        self.available_resources: tuple[str, ...] = ()

    def initialize(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        self._require_live_context(context)
        self.desired_settings = self._normalized_settings(
            settings,
            require_resource=False,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
        )
        discovery_message = ""
        try:
            self.available_resources = (
                self._resource_lister()
            )
        except Exception as exc:
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
            "Identity": "Not queried",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Excitation": "Shunted",
            "Available GPIB Resources": list(
                self.available_resources
            ),
            "Resource Discovery": (
                discovery_message or "Completed"
            ),
            "Last Channel": "-",
            "Last Resistance (Ohm)": "-",
            "Last Phase (deg)": "-",
            "Last Current (A)": "-",
            "Last Status": "-",
        }
        context.update_status(status)
        return status

    def apply_settings(
        self,
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        desired = self._normalized_settings(
            settings,
            require_resource=True,
            operation_timeout_seconds=(
                context.operation_timeout_seconds
            ),
        )
        self.desired_settings = deepcopy(desired)
        self._connect(
            desired["resource"],
            float(desired["io_timeout_seconds"]),
        )
        try:
            self._write(
                f"FREQ 0,{desired['frequency_index']}",
                context,
            )
            self._expect_integers(
                "FREQ? 0",
                (int(desired["frequency_index"]),),
                context,
            )
            for slot in range(1, 5):
                channel = desired["channels"][
                    f"r{slot}"
                ]
                self._configure_channel(
                    channel,
                    enabled=bool(channel["enabled"]),
                    shunted=True,
                    context=context,
                )
        except Exception:
            self._best_effort_shunt_settings(desired)
            self._close_transport()
            raise
        self.applied_settings = deepcopy(desired)
        status = {
            "Connection": "Connected",
            "Resource": desired["resource"],
            "Identity": self.identity,
            "Applied Settings": "Applied; excitation shunted",
            "Excitation": "Shunted",
            "Estimated Measure Time (s)": (
                self._estimated_measure_seconds(desired)
            ),
        }
        context.update_status(status)
        return status

    def begin_sequence(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        self._require_ready()
        self.sequence_active = True
        status = {
            "Sequence": "Running",
            "Excitation": "Shunted",
        }
        context.update_status(status)
        return status

    def measure(
        self,
        context: ModuleOperationContext,
    ) -> None:
        self._require_ready()
        settings = self.applied_settings
        self._validate_measure_duration(
            settings,
            context.operation_timeout_seconds,
        )
        for slot in range(1, 5):
            channel = settings["channels"][f"r{slot}"]
            if not channel["enabled"]:
                continue
            context.checkpoint()
            self._measure_channel(
                slot,
                channel,
                settings,
                context,
            )

    def _measure_channel(
        self,
        slot: int,
        channel: Mapping[str, Any],
        settings: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> None:
        input_channel = int(channel["input_channel"])
        failure: Exception | None = None
        try:
            self._configure_channel(
                channel,
                enabled=True,
                shunted=True,
                context=context,
            )
            self._switch_channel(
                input_channel,
                context,
            )
            self._set_shunt(
                channel,
                shunted=False,
                context=context,
            )
            context.update_status({
                "Excitation": (
                    f"Active on input {input_channel}"
                ),
                "Last Channel": f"R{slot} / input {input_channel}",
            })
            self._waiter(
                context,
                float(settings["pause_seconds"]),
            )
            first = context.sample_system()
            self._waiter(
                context,
                float(settings["dwell_seconds"]),
            )
            second = context.sample_system()
            temperature, field = (
                self._averaged_system_values(
                    first,
                    second,
                )
            )
            resistance = self._query_float(
                f"RDGR? {input_channel}",
                context,
            )
            quadrature = self._query_float(
                f"QRDG? {input_channel}",
                context,
            )
            power = self._query_float(
                f"RDGPWR? {input_channel}",
                context,
            )
            status_bits = self._query_status(
                input_channel,
                context,
            )
            phase = math.degrees(
                math.atan2(
                    quadrature,
                    resistance,
                )
            )
            current = self._excitation_current(
                channel,
                resistance,
                quadrature,
                power,
            )
            status, details = self._status(
                status_bits
            )
            self._publish_reading_warning(
                slot,
                input_channel,
                status,
                details,
                context,
            )
            row = {
                "TemperatureAverage": temperature,
                "FieldAverage": field,
                f"R{slot}": resistance,
                f"Phase{slot}": phase,
                f"Current{slot}": current,
                f"Status{slot}": status,
            }
            context.emit_row(row)
            self.last_values = {
                "slot": slot,
                "input_channel": input_channel,
                "resistance": resistance,
                "phase": phase,
                "current": current,
                "status": status,
            }
            context.update_status({
                "Last Channel": (
                    f"R{slot} / input {input_channel}"
                ),
                "Last Resistance (Ohm)": resistance,
                "Last Phase (deg)": phase,
                "Last Current (A)": current,
                "Last Status": (
                    status
                    if not details
                    else f"{status}: {details}"
                ),
            })
        except Exception as exc:
            failure = exc
            raise
        finally:
            should_shunt = (
                bool(settings["shunt_after_read"])
                or failure is not None
            )
            if should_shunt:
                cleanup_error = (
                    self._best_effort_shunt_channel(
                        channel
                    )
                )
                context.update_status({
                    "Excitation": "Shunted",
                })
                if cleanup_error is not None:
                    raise ModuleError(
                        "Could not confirm excitation shunt "
                        f"for input {input_channel}: "
                        f"{cleanup_error}",
                        "LS372_SHUNT_FAILED",
                        f"input {input_channel}",
                    ) from failure

    def end_sequence(
        self,
        reason: str,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        try:
            self._shunt_all(context)
        except Exception:
            context.update_status({
                "Sequence": reason.title(),
                "Excitation": "Shunt unconfirmed",
            })
            raise
        finally:
            self.sequence_active = False
        status = {
            "Sequence": reason.title(),
            "Excitation": "Shunted",
        }
        context.update_status(status)
        return status

    def abort(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        errors = self._best_effort_shunt_settings(
            self.applied_settings
        )
        self._close_transport()
        self.sequence_active = False
        self.applied_settings = {}
        status = {
            "Connection": "Disconnected",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Excitation": (
                "Shunted"
                if not errors
                else "Shunt unconfirmed"
            ),
        }
        context.update_status(status)
        if errors:
            raise ModuleError(
                "One or more 372A inputs could not be "
                "shunted before disconnect: "
                + "; ".join(errors),
                "LS372_SHUNT_FAILED",
            )
        return status

    def read_status(
        self,
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        if self.transport is None:
            return {
                "Connection": "Disconnected",
                "Sequence": (
                    "Running"
                    if self.sequence_active
                    else "Idle"
                ),
            }
        identity = self._query_text("*IDN?", context)
        self._validate_identity(identity)
        self.identity = identity
        return {
            "Connection": "Connected",
            "Resource": (
                self.applied_settings.get("resource")
                or self.desired_settings.get("resource")
                or "Unknown"
            ),
            "Identity": identity,
            "Sequence": (
                "Running"
                if self.sequence_active
                else "Idle"
            ),
        }

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        if action == "refresh_resources":
            try:
                self.available_resources = (
                    self._resource_lister()
                )
            except Exception as exc:
                raise ModuleWarning(
                    f"GPIB resource discovery failed: "
                    f"{type(exc).__name__}: {exc}",
                    "LS372_RESOURCE_DISCOVERY_FAILED",
                ) from exc
            status = {
                "Available GPIB Resources": list(
                    self.available_resources
                ),
                "Resource Discovery": "Completed",
                "Last Action": (
                    f"Found {len(self.available_resources)} "
                    "GPIB resource(s)"
                ),
            }
        elif action == "test_connection":
            supplied = payload.get("settings")
            source = (
                supplied
                if isinstance(supplied, Mapping)
                else self.desired_settings
            )
            settings = self._normalized_settings(
                source,
                require_resource=True,
                operation_timeout_seconds=(
                    context.operation_timeout_seconds
                ),
            )
            self._connect(
                settings["resource"],
                float(settings["io_timeout_seconds"]),
            )
            status = {
                "Connection": "Connected",
                "Resource": settings["resource"],
                "Identity": self.identity,
                "Applied Settings": "Not applied",
                "Last Action": "Connection test passed",
            }
        else:
            return (
                super().manual_action(
                    action,
                    payload,
                    context,
                )
                or {}
            )
        context.update_status(status)
        return status

    @staticmethod
    def _require_live_context(
        context: ModuleOperationContext,
    ) -> None:
        if (
            not callable(
                getattr(context, "sample_system", None)
            )
            or not callable(
                getattr(
                    context,
                    "interruptible_sleep",
                    None,
                )
            )
        ):
            raise ModuleError(
                "This module requires the live, interruptible "
                "Measurement Module context added after "
                "OpenLab Control 0.11.0 Beta 2 or newer",
                "LS372_CORE_API_TOO_OLD",
            )

    def _connect(
        self,
        resource: str,
        timeout_seconds: float,
    ) -> None:
        self._close_transport()
        transport: InstrumentTransport | None = None
        try:
            transport = self._transport_factory(
                resource,
                timeout_seconds,
            )
            identity = str(
                transport.query("*IDN?")
            ).strip()
            self._validate_identity(identity)
        except Exception as exc:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass
            raise ModuleError(
                f"Could not connect to {resource}: "
                f"{type(exc).__name__}: {exc}",
                "LS372_CONNECTION_FAILED",
                resource,
            ) from exc
        self.transport = transport
        self.identity = identity

    @staticmethod
    def _validate_identity(identity: str) -> None:
        compact = re.sub(
            r"[\s_-]+",
            "",
            identity,
        ).upper()
        if "MODEL372" not in compact:
            raise ModuleError(
                f"Expected Lake Shore Model 372, received "
                f"{identity!r}",
                "LS372_IDENTITY_MISMATCH",
            )

    def _write(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> None:
        self._call_with_retry(
            command,
            lambda transport: transport.write(command),
            context,
        )

    def _query_text(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> str:
        result = self._call_with_retry(
            command,
            lambda transport: transport.query(command),
            context,
        )
        text = str(result).strip()
        if not text:
            raise ModuleError(
                f"Model 372 returned an empty reply to "
                f"{command}",
                "LS372_INVALID_REPLY",
                command,
            )
        return text

    def _call_with_retry(
        self,
        command: str,
        operation: Callable[
            [InstrumentTransport],
            Any,
        ],
        context: ModuleOperationContext,
    ) -> Any:
        settings = (
            self.applied_settings
            or self.desired_settings
        )
        attempts = int(
            settings.get("retry_attempts", 1)
        )
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            transport = self.transport
            if transport is None:
                last_error = RuntimeError(
                    "Model 372 transport is disconnected"
                )
                if attempt >= attempts:
                    break
                try:
                    self._reopen_transport(settings)
                except Exception as reopen_error:
                    last_error = reopen_error
                continue
            try:
                result = operation(transport)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                context.warning(
                    f"Model 372 I/O failed; retrying "
                    f"({attempt}/{attempts}): "
                    f"{type(exc).__name__}: {exc}",
                    "LS372_IO_RETRY",
                    command,
                )
                self._waiter(context, 0.2)
                try:
                    self._reopen_transport(settings)
                except Exception as reopen_error:
                    last_error = reopen_error
                continue
            context.resolve_warning(
                "LS372_IO_RETRY",
                command,
            )
            return result
        context.resolve_warning(
            "LS372_IO_RETRY",
            command,
        )
        assert last_error is not None
        raise ModuleError(
            f"Model 372 I/O failed after {attempts} "
            f"attempt(s) for {command}: "
            f"{type(last_error).__name__}: {last_error}",
            "LS372_COMMUNICATION_FAILED",
            command,
        ) from last_error

    def _reopen_transport(
        self,
        settings: Mapping[str, Any],
    ) -> None:
        resource = str(settings["resource"])
        timeout = float(settings["io_timeout_seconds"])
        self._close_transport()
        transport = self._transport_factory(
            resource,
            timeout,
        )
        try:
            identity = str(
                transport.query("*IDN?")
            ).strip()
            self._validate_identity(identity)
        except Exception:
            transport.close()
            raise
        self.transport = transport
        self.identity = identity

    def _configure_channel(
        self,
        channel: Mapping[str, Any],
        *,
        enabled: bool,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        settings = (
            self.applied_settings
            or self.desired_settings
        )
        input_channel = int(channel["input_channel"])
        self._write(
            "FILTER "
            f"{input_channel},"
            f"{1 if settings['filter_enabled'] else 0},"
            f"{settings['filter_settle_seconds']},"
            f"{settings['filter_window_percent']}",
            context,
        )
        self._expect_integers(
            f"FILTER? {input_channel}",
            (
                1 if settings["filter_enabled"] else 0,
                int(settings["filter_settle_seconds"]),
                int(settings["filter_window_percent"]),
            ),
            context,
        )
        self._write(
            "INSET "
            f"{input_channel},"
            f"{1 if enabled else 0},"
            f"{settings['dwell_seconds']},"
            f"{settings['pause_seconds']},"
            "0,2",
            context,
        )
        self._expect_integers(
            f"INSET? {input_channel}",
            (
                1 if enabled else 0,
                int(settings["dwell_seconds"]),
                int(settings["pause_seconds"]),
                0,
                2,
            ),
            context,
        )
        self._write(
            self._intype_command(
                channel,
                shunted=shunted,
            ),
            context,
        )
        self._verify_intype(
            channel,
            shunted=shunted,
            context=context,
        )

    def _switch_channel(
        self,
        input_channel: int,
        context: ModuleOperationContext,
    ) -> None:
        self._write(
            f"SCAN {input_channel},0",
            context,
        )
        self._expect_integers(
            "SCAN?",
            (input_channel, 0),
            context,
        )

    def _set_shunt(
        self,
        channel: Mapping[str, Any],
        *,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        self._write(
            self._intype_command(
                channel,
                shunted=shunted,
            ),
            context,
        )
        self._verify_intype(
            channel,
            shunted=shunted,
            context=context,
        )

    def _verify_intype(
        self,
        channel: Mapping[str, Any],
        *,
        shunted: bool,
        context: ModuleOperationContext,
    ) -> None:
        self._expect_integers(
            f"INTYPE? {channel['input_channel']}",
            self._intype_values(
                channel,
                shunted=shunted,
            ),
            context,
        )

    def _expect_integers(
        self,
        command: str,
        expected: tuple[int, ...],
        context: ModuleOperationContext,
    ) -> None:
        reply = self._query_text(command, context)
        try:
            actual = tuple(
                int(part.strip(), 10)
                for part in reply.split(",")
            )
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned an invalid settings "
                f"reply to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if actual != expected:
            raise ModuleError(
                f"Model 372 settings readback mismatch for "
                f"{command}: expected {expected}, "
                f"read back {actual}",
                "LS372_SETTINGS_VERIFY_FAILED",
                command,
            )

    def _query_float(
        self,
        command: str,
        context: ModuleOperationContext,
    ) -> float:
        reply = self._query_text(command, context)
        try:
            value = float(reply)
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned a non-numeric reply "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if not math.isfinite(value):
            raise ModuleError(
                f"Model 372 returned a non-finite reply "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            )
        return value

    def _query_status(
        self,
        input_channel: int,
        context: ModuleOperationContext,
    ) -> int:
        command = f"RDGST? {input_channel}"
        reply = self._query_text(command, context)
        try:
            value = int(reply, 10)
        except ValueError as exc:
            raise ModuleError(
                f"Model 372 returned an invalid status "
                f"to {command}: {reply!r}",
                "LS372_INVALID_REPLY",
                command,
            ) from exc
        if not 0 <= value <= 255:
            raise ModuleError(
                f"Model 372 returned an out-of-range status "
                f"to {command}: {value}",
                "LS372_INVALID_REPLY",
                command,
            )
        return value

    @staticmethod
    def _status(
        status_bits: int,
    ) -> tuple[str, str]:
        details = "|".join(
            label
            for bit, label in STATUS_BITS
            if status_bits & bit
        )
        if status_bits & 1:
            return "OVER_COMPLIANCE", details
        if status_bits:
            return "OVER_RANGE", details
        return "NORMAL", ""

    @staticmethod
    def _publish_reading_warning(
        slot: int,
        input_channel: int,
        status: str,
        details: str,
        context: ModuleOperationContext,
    ) -> None:
        warning_context = (
            f"R{slot}/input {input_channel}"
        )
        if status == "OVER_COMPLIANCE":
            context.resolve_warning(
                "LS372_OVER_RANGE",
                warning_context,
            )
            context.warning(
                f"R{slot} input {input_channel} exceeded "
                f"current-source compliance ({details})",
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
        elif status == "OVER_RANGE":
            context.resolve_warning(
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
            context.warning(
                f"R{slot} input {input_channel} is outside "
                f"the valid measurement range ({details})",
                "LS372_OVER_RANGE",
                warning_context,
            )
        else:
            context.resolve_warning(
                "LS372_OVER_COMPLIANCE",
                warning_context,
            )
            context.resolve_warning(
                "LS372_OVER_RANGE",
                warning_context,
            )

    @staticmethod
    def _excitation_current(
        channel: Mapping[str, Any],
        resistance: float,
        quadrature: float,
        power: float,
    ) -> float:
        if channel["excitation_mode"] == "current":
            values = {
                index: current
                for index, _label, current
                in CURRENT_EXCITATIONS
            }
            return values[
                int(channel["excitation_range"])
            ]
        del quadrature
        dissipative_resistance = abs(resistance)
        if dissipative_resistance <= 0:
            raise ModuleError(
                "Cannot estimate voltage-mode current from "
                "zero resistance",
                "LS372_CURRENT_CALCULATION_FAILED",
            )
        current = math.sqrt(
            abs(power) / dissipative_resistance
        )
        if not math.isfinite(current):
            raise ModuleError(
                "Calculated excitation current is not finite",
                "LS372_CURRENT_CALCULATION_FAILED",
            )
        return current

    @classmethod
    def _averaged_system_values(
        cls,
        first: Mapping[str, Mapping[str, Any]],
        second: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float, float]:
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
            if str(values.get("kind", "")).casefold()
            == kind
        ]
        candidates.sort(
            key=lambda item: (
                0
                if str(
                    item[1].get("role", "")
                ).casefold()
                == "primary"
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
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                kind,
            )
        device_id, values = candidates[0]
        return LakeShore372ABackend._snapshot_values(
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
                f"{kind.title()} device {device_id} is missing "
                "from the second system snapshot",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        return LakeShore372ABackend._snapshot_values(
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
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            )
        try:
            current = float(values["current"])
            timestamp = float(values["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModuleError(
                f"{kind.title()} device {device_id} has no "
                "valid current value or timestamp",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
                device_id,
            ) from exc
        if (
            not math.isfinite(current)
            or not math.isfinite(timestamp)
        ):
            raise ModuleError(
                f"{kind.title()} device {device_id} returned "
                "a non-finite value or timestamp",
                "LS372_SYSTEM_SNAPSHOT_UNAVAILABLE",
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
                "did not advance between pause and dwell "
                "samples",
                "LS372_SYSTEM_SNAPSHOT_NOT_FRESH",
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
            "LS372_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    @staticmethod
    def _field_oersted(
        value: float,
        unit: str,
    ) -> float:
        normalized = unit.strip().casefold()
        if normalized in {"oe", "oersted", "g", "gauss"}:
            return value
        if normalized in {"koe", "kilo-oersted"}:
            return value * 1000.0
        if normalized in {"t", "tesla"}:
            return value * 10_000.0
        if normalized in {"mt", "millitesla"}:
            return value * 10.0
        raise ModuleError(
            f"Unsupported magnetic-field unit: {unit!r}",
            "LS372_SYSTEM_UNIT_UNSUPPORTED",
            unit,
        )

    def _shunt_all(
        self,
        context: ModuleOperationContext,
    ) -> None:
        if not self.applied_settings:
            return
        errors: list[str] = []
        for slot in range(1, 5):
            channel = self.applied_settings[
                "channels"
            ][f"r{slot}"]
            if not channel["enabled"]:
                continue
            try:
                self._set_shunt(
                    channel,
                    shunted=True,
                    context=context,
                )
            except Exception as exc:
                errors.append(
                    f"input {channel['input_channel']}: "
                    f"{type(exc).__name__}: {exc}"
                )
        if errors:
            raise ModuleError(
                "Could not shunt all configured Model 372 "
                "inputs: "
                + "; ".join(errors),
                "LS372_SHUNT_FAILED",
            )

    def _best_effort_shunt_channel(
        self,
        channel: Mapping[str, Any],
    ) -> str | None:
        transport = self.transport
        if transport is None:
            return "transport is disconnected"
        try:
            transport.write(
                self._intype_command(
                    channel,
                    shunted=True,
                )
            )
            reply = str(
                transport.query(
                    f"INTYPE? {channel['input_channel']}"
                )
            ).strip()
            actual = tuple(
                int(part.strip(), 10)
                for part in reply.split(",")
            )
            expected = self._intype_values(
                channel,
                shunted=True,
            )
            if actual != expected:
                return (
                    f"shunt readback mismatch: expected "
                    f"{expected}, read back {actual}"
                )
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def _best_effort_shunt_settings(
        self,
        settings: Mapping[str, Any],
    ) -> list[str]:
        if not settings:
            return []
        errors: list[str] = []
        channels = settings.get("channels", {})
        if not isinstance(channels, Mapping):
            return []
        for slot in range(1, 5):
            channel = channels.get(f"r{slot}")
            if (
                not isinstance(channel, Mapping)
                or not channel.get("enabled")
            ):
                continue
            if self.transport is None:
                errors.append(
                    f"input {channel.get('input_channel')}: "
                    "transport disconnected; shunt not confirmed"
                )
                continue
            error = self._best_effort_shunt_channel(
                channel
            )
            if error is not None:
                errors.append(
                    f"input {channel.get('input_channel')}: "
                    f"{error}"
                )
        return errors

    @staticmethod
    def _intype_command(
        channel: Mapping[str, Any],
        *,
        shunted: bool,
    ) -> str:
        mode = (
            1
            if channel["excitation_mode"] == "current"
            else 0
        )
        return (
            "INTYPE "
            f"{channel['input_channel']},"
            f"{mode},"
            f"{channel['excitation_range']},"
            f"{channel['autorange']},"
            f"{channel['resistance_range']},"
            f"{1 if shunted else 0},"
            "2"
        )

    @staticmethod
    def _intype_values(
        channel: Mapping[str, Any],
        *,
        shunted: bool,
    ) -> tuple[int, ...]:
        return (
            (
                1
                if channel["excitation_mode"] == "current"
                else 0
            ),
            int(channel["excitation_range"]),
            int(channel["autorange"]),
            int(channel["resistance_range"]),
            1 if shunted else 0,
            2,
        )

    def _close_transport(self) -> None:
        transport = self.transport
        self.transport = None
        self.identity = ""
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
                "Apply Settings and confirm the Model 372 "
                "connection before running Measure",
                "LS372_SETTINGS_NOT_APPLIED",
            )

    @classmethod
    def _normalized_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        require_resource: bool,
        operation_timeout_seconds: float,
    ) -> dict[str, Any]:
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
                "GPIB resource must be one line with at most "
                "255 characters",
                "LS372_INVALID_SETTINGS",
                "resource",
            )
        if require_resource and not resource:
            raise ModuleError(
                "Select a GPIB resource before Apply Settings",
                "LS372_INVALID_SETTINGS",
                "resource",
            )
        if (
            resource
            and not resource.upper().startswith("GPIB")
        ):
            raise ModuleError(
                "Lake Shore 372A resource must be a GPIB "
                "VISA resource",
                "LS372_INVALID_SETTINGS",
                resource,
            )
        result["resource"] = resource
        result["frequency_index"] = cls._integer(
            result["frequency_index"],
            1,
            5,
            "frequency_index",
        )
        result["pause_seconds"] = cls._integer(
            result["pause_seconds"],
            3,
            200,
            "pause_seconds",
        )
        result["dwell_seconds"] = cls._integer(
            result["dwell_seconds"],
            1,
            200,
            "dwell_seconds",
        )
        result["filter_enabled"] = cls._boolean(
            result["filter_enabled"],
            "filter_enabled",
        )
        result["filter_settle_seconds"] = (
            cls._integer(
                result["filter_settle_seconds"],
                1,
                200,
                "filter_settle_seconds",
            )
        )
        result["filter_window_percent"] = (
            cls._integer(
                result["filter_window_percent"],
                1,
                80,
                "filter_window_percent",
            )
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
        result["shunt_after_read"] = cls._boolean(
            result["shunt_after_read"],
            "shunt_after_read",
        )

        raw_channels = raw.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raise ModuleError(
                "channels must be a settings table",
                "LS372_INVALID_SETTINGS",
                "channels",
            )
        channels: dict[str, dict[str, Any]] = {}
        physical_channels: list[int] = []
        for slot in range(1, 5):
            key = f"r{slot}"
            default_channel = defaults["channels"][key]
            supplied = raw_channels.get(key, {})
            if not isinstance(supplied, Mapping):
                raise ModuleError(
                    f"{key} settings must be a table",
                    "LS372_INVALID_SETTINGS",
                    key,
                )
            channel = {
                name: supplied.get(name, value)
                for name, value
                in default_channel.items()
            }
            channel["enabled"] = cls._boolean(
                channel["enabled"],
                f"{key}.enabled",
            )
            channel["input_channel"] = cls._integer(
                channel["input_channel"],
                1,
                16,
                f"{key}.input_channel",
            )
            mode = str(
                channel["excitation_mode"]
            ).strip().casefold()
            if mode not in {"current", "voltage"}:
                raise ModuleError(
                    f"{key}.excitation_mode must be current "
                    "or voltage",
                    "LS372_INVALID_SETTINGS",
                    key,
                )
            channel["excitation_mode"] = mode
            maximum_excitation = (
                22 if mode == "current" else 12
            )
            channel["excitation_range"] = (
                cls._integer(
                    channel["excitation_range"],
                    1,
                    maximum_excitation,
                    f"{key}.excitation_range",
                )
            )
            channel["autorange"] = cls._integer(
                channel["autorange"],
                0,
                1,
                f"{key}.autorange",
            )
            channel["resistance_range"] = (
                cls._integer(
                    channel["resistance_range"],
                    1,
                    22,
                    f"{key}.resistance_range",
                )
            )
            channels[key] = channel
            physical_channels.append(
                channel["input_channel"]
            )
        if len(set(physical_channels)) != 4:
            raise ModuleError(
                "R1-R4 physical input selections must be "
                "unique",
                "LS372_INVALID_SETTINGS",
                "channels",
            )
        if not any(
            channel["enabled"]
            for channel in channels.values()
        ):
            raise ModuleError(
                "Enable at least one R1-R4 channel",
                "LS372_INVALID_SETTINGS",
                "channels",
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
        enabled = sum(
            bool(channel["enabled"])
            for channel in settings[
                "channels"
            ].values()
        )
        return (
            enabled
            * (
                float(settings["pause_seconds"])
                + float(settings["dwell_seconds"])
                + 1.0
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
        estimate = cls._estimated_measure_seconds(
            settings
        )
        timeout = float(operation_timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or estimate > max(0.0, timeout - 2.0)
        ):
            raise ModuleError(
                f"Estimated Measure time {estimate:.1f} s "
                f"does not fit the core operation timeout "
                f"{timeout:.1f} s; shorten pause/dwell or "
                "increase [modules] "
                "operation_timeout_seconds and restart",
                "LS372_MEASURE_TIMEOUT_UNSAFE",
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
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            isinstance(value, float)
            and value != result
        ) or not minimum <= result <= maximum:
            raise ModuleError(
                f"{name} must be an integer from {minimum} "
                f"to {maximum}",
                "LS372_INVALID_SETTINGS",
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
                "LS372_INVALID_SETTINGS",
                name,
            )
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LS372_INVALID_SETTINGS",
                name,
            ) from exc
        if (
            not math.isfinite(result)
            or not minimum <= result <= maximum
        ):
            raise ModuleError(
                f"{name} must be from {minimum:g} to "
                f"{maximum:g}",
                "LS372_INVALID_SETTINGS",
                name,
            )
        return result

    @staticmethod
    def _boolean(
        value: Any,
        name: str,
    ) -> bool:
        if not isinstance(value, bool):
            raise ModuleError(
                f"{name} must be true or false",
                "LS372_INVALID_SETTINGS",
                name,
            )
        return value


__all__ = [
    "InstrumentTransport",
    "LakeShore372ABackend",
    "PyVisaTransport",
]
