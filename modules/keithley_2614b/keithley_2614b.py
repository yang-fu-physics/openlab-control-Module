"""Keithley 2614B 的底层 VISA、TSP 指令与响应解析。"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from typing import Protocol

from .constants import SENSE_4WIRE, SOURCE_CURRENT


class Transport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
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


IDENTIFY = "*IDN?"
_MEASUREMENT_SPLIT = re.compile(r"[\s,]+")


def number(value: object) -> str:
    return f"{float(value):.12g}"


def validate_identity(identity: str) -> bool:
    normalized = " ".join(identity.upper().replace(",", " ").split())
    return "KEITHLEY" in normalized and "2614B" in normalized


def high_impedance_command(smu: str) -> str:
    return f"{smu}.source.offmode = {smu}.OUTPUT_HIGH_Z"


def high_impedance_query(smu: str) -> str:
    return f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)"


def output_command(smu: str, enabled: bool) -> str:
    state = "OUTPUT_ON" if enabled else "OUTPUT_OFF"
    return f"{smu}.source.output = {smu}.{state}"


def output_query(smu: str) -> str:
    return f"print({smu}.source.output == {smu}.OUTPUT_ON)"


def configuration_commands(
    smu: str,
    settings: Mapping[str, object],
) -> tuple[str, ...]:
    """返回单个 SMU 通道的完整绝对 TSP 配置。"""

    commands = [high_impedance_command(smu)]
    if settings["source_mode"] == SOURCE_CURRENT:
        commands.extend(
            (
                f"{smu}.source.func = {smu}.OUTPUT_DCAMPS",
                f"{smu}.source.autorangei = {smu}.AUTORANGE_ON",
                f"{smu}.source.leveli = 0",
                f"{smu}.source.limitv = {number(settings['voltage_limit'])}",
            )
        )
    else:
        commands.extend(
            (
                f"{smu}.source.func = {smu}.OUTPUT_DCVOLTS",
                f"{smu}.source.autorangev = {smu}.AUTORANGE_ON",
                f"{smu}.source.levelv = 0",
                f"{smu}.source.limiti = {number(settings['current_limit'])}",
            )
        )
    commands.extend(
        (
            f"{smu}.measure.autorangev = {smu}.AUTORANGE_ON",
            f"{smu}.measure.autorangei = {smu}.AUTORANGE_ON",
            f"{smu}.measure.nplc = {number(settings['nplc'])}",
            (
                f"{smu}.sense = {smu}.SENSE_REMOTE"
                if settings["sense_mode"] == SENSE_4WIRE
                else f"{smu}.sense = {smu}.SENSE_LOCAL"
            ),
            (
                f"{smu}.source.leveli = {number(settings['source_current'])}"
                if settings["source_mode"] == SOURCE_CURRENT
                else f"{smu}.source.levelv = {number(settings['source_voltage'])}"
            ),
        )
    )
    return tuple(commands)


def source_function_query(smu: str, current_mode: bool) -> str:
    expected = "OUTPUT_DCAMPS" if current_mode else "OUTPUT_DCVOLTS"
    return f"print({smu}.source.func == {smu}.{expected})"


def source_level_query(smu: str, current_mode: bool) -> str:
    field = "leveli" if current_mode else "levelv"
    return f"print({smu}.source.{field})"


def source_limit_query(smu: str, current_mode: bool) -> str:
    field = "limitv" if current_mode else "limiti"
    return f"print({smu}.source.{field})"


def source_autorange_query(smu: str, current_mode: bool) -> str:
    field = "autorangei" if current_mode else "autorangev"
    return f"print({smu}.source.{field} == {smu}.AUTORANGE_ON)"


def measure_autorange_query(smu: str, quantity: str) -> str:
    if quantity not in {"v", "i"}:
        raise ValueError(f"unsupported 2614B quantity: {quantity}")
    return f"print({smu}.measure.autorange{quantity} == {smu}.AUTORANGE_ON)"


def nplc_query(smu: str) -> str:
    return f"print({smu}.measure.nplc)"


def remote_sense_query(smu: str) -> str:
    return f"print({smu}.sense == {smu}.SENSE_REMOTE)"


def measurement_query(smu: str) -> str:
    return (
        f"print({smu}.measure.v(), {smu}.measure.i(), "
        f"{smu}.source.compliance)"
    )


def parse_bool(value: object) -> bool:
    token = str(value).strip().strip('"').casefold()
    if token in {"true", "1", "on"}:
        return True
    if token in {"false", "0", "off"}:
        return False
    raise ValueError(f"invalid boolean {value!r}")


def parse_measurement(reply: str) -> tuple[float, float, bool]:
    parts = [item for item in _MEASUREMENT_SPLIT.split(reply.strip()) if item]
    if len(parts) != 3:
        raise ValueError(f"expected voltage,current,compliance; received {reply!r}")
    try:
        voltage = float(parts[0])
        current = float(parts[1])
    except ValueError as exc:
        raise ValueError(f"non-numeric V/I response {reply!r}") from exc
    if not math.isfinite(voltage) or not math.isfinite(current):
        raise ValueError(f"non-finite V/I response {reply!r}")
    return voltage, current, parse_bool(parts[2])


def parse_number(reply: str) -> float:
    value = float(reply)
    if not math.isfinite(value):
        raise ValueError(f"non-finite numeric response {reply!r}")
    return value


__all__ = [
    "Transport",
    "PyVisaTransport",
    "IDENTIFY",
    "number",
    "validate_identity",
    "high_impedance_command",
    "high_impedance_query",
    "output_command",
    "output_query",
    "configuration_commands",
    "source_function_query",
    "source_level_query",
    "source_limit_query",
    "source_autorange_query",
    "measure_autorange_query",
    "nplc_query",
    "remote_sense_query",
    "measurement_query",
    "parse_bool",
    "parse_measurement",
    "parse_number",
]
