"""Keithley 6517B 的底层 VISA、SCPI 指令与响应解析。"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from typing import Protocol


class Transport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """仅存在于隔离模块 worker 中的 6517B GPIB 会话。"""

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
CLEAR_STATUS = "*CLS"
OUTPUT_QUERY = "OUTP1?"
OUTPUT_ON = "OUTP1 ON"
OUTPUT_OFF = "OUTP1 OFF"
ZERO_CHECK_QUERY = "SYST:ZCH?"
ZERO_CHECK_ON = "SYST:ZCH ON"
ZERO_CHECK_OFF = "SYST:ZCH OFF"
METER_CONNECT_QUERY = "SOUR:VOLT:MCON?"
RESISTIVE_LIMIT_QUERY = "SOUR:CURR:RLIM:STAT?"
COMPLIANCE_QUERY = "SOUR:CURR:LIM:STAT?"
SOURCE_RANGE_QUERY = "SOUR:VOLT:RANG?"
SOURCE_VOLTAGE_QUERY = "SOUR:VOLT?"
VOLTAGE_LIMIT_QUERY = "SOUR:VOLT:LIM?"
VOLTAGE_LIMIT_STATE_QUERY = "SOUR:VOLT:LIM:STAT?"
SENSE_FUNCTION_QUERY = "SENS:FUNC?"
CURRENT_AUTORANGE_QUERY = "SENS:CURR:RANG:AUTO?"
CURRENT_NPLC_QUERY = "SENS:CURR:NPLC?"
DATA_ELEMENTS_QUERY = "FORM:ELEM?"
READ = "READ?"
ERROR_QUERY = "SYST:ERR?"

_NUMBER_PREFIX = re.compile(
    r"^\s*([+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?)"
)


def number(value: object) -> str:
    return f"{float(value):.12g}"


def configuration_commands(
    settings: Mapping[str, object],
    source_range: float,
) -> tuple[str, ...]:
    """FVMI 两线测量的完整绝对设置序列。"""

    return (
        f"SOUR:VOLT:RANG {number(source_range)}",
        f"SOUR:VOLT:LIM {number(settings['voltage_limit'])}",
        "SOUR:VOLT:LIM:STAT ON",
        f"SOUR:VOLT {number(settings['source_voltage'])}",
        # RLIM 会在输出中串入 1 MOhm，固定关闭并由后端读回。
        "SOUR:CURR:RLIM:STAT OFF",
        # METER-CONNECT 把 V-source LO 内部连接至 ammeter LO。
        "SOUR:VOLT:MCON ON",
        "SENS:FUNC 'CURR:DC'",
        "SENS:CURR:RANG:AUTO ON",
        f"SENS:CURR:NPLC {number(settings['nplc'])}",
        "FORM:DATA ASC",
        "FORM:ELEM READ,STAT,VSOUR",
    )


def output_command(enabled: bool) -> str:
    return OUTPUT_ON if enabled else OUTPUT_OFF


def zero_check_command(enabled: bool) -> str:
    return ZERO_CHECK_ON if enabled else ZERO_CHECK_OFF


def validate_identity(identity: str) -> bool:
    normalized = " ".join(identity.upper().replace(",", " ").split())
    return "KEITHLEY" in normalized and "6517B" in normalized


def parse_switch(value: object) -> bool:
    token = str(value).strip().strip('"').upper()
    if token in {"1", "ON"}:
        return True
    if token in {"0", "OFF"}:
        return False
    raise ValueError(f"invalid switch state {value!r}")


def parse_numeric_element(value: str) -> float:
    matched = _NUMBER_PREFIX.match(value)
    if matched is None:
        raise ValueError(f"non-numeric element {value!r}")
    result = float(matched.group(1))
    if not math.isfinite(result):
        raise ValueError(f"non-finite element {value!r}")
    return result


def parse_measurement(reply: str) -> tuple[float, str, float]:
    parts = [item.strip() for item in reply.split(",")]
    if len(parts) != 3:
        raise ValueError(f"expected READ,STAT,VSOUR, received {reply!r}")
    current = parse_numeric_element(parts[0])
    status = parts[1].strip().strip('"').strip("'").upper()
    voltage = parse_numeric_element(parts[2])
    if not status:
        raise ValueError(f"empty status in {reply!r}")
    return current, status, voltage


def parse_error_code(reply: str) -> int:
    matched = re.match(r"\s*([+-]?\d+)", reply)
    if matched is None:
        raise ValueError(f"invalid error response {reply!r}")
    return int(matched.group(1))


def canonical_element(value: object) -> str:
    token = str(value).strip().strip('"').strip("'").upper()
    if token.startswith("READ"):
        return "READ"
    if token.startswith("STAT"):
        return "STAT"
    if token.startswith("VSO"):
        return "VSOUR"
    return token


def clean_token(value: object) -> str:
    return str(value).strip().strip('"').strip("'").upper()


__all__ = [
    "Transport",
    "PyVisaTransport",
    "IDENTIFY",
    "CLEAR_STATUS",
    "OUTPUT_QUERY",
    "OUTPUT_OFF",
    "ZERO_CHECK_QUERY",
    "ZERO_CHECK_ON",
    "METER_CONNECT_QUERY",
    "RESISTIVE_LIMIT_QUERY",
    "COMPLIANCE_QUERY",
    "SOURCE_RANGE_QUERY",
    "SOURCE_VOLTAGE_QUERY",
    "VOLTAGE_LIMIT_QUERY",
    "VOLTAGE_LIMIT_STATE_QUERY",
    "SENSE_FUNCTION_QUERY",
    "CURRENT_AUTORANGE_QUERY",
    "CURRENT_NPLC_QUERY",
    "DATA_ELEMENTS_QUERY",
    "READ",
    "ERROR_QUERY",
    "configuration_commands",
    "output_command",
    "zero_check_command",
    "validate_identity",
    "parse_switch",
    "parse_numeric_element",
    "parse_measurement",
    "parse_error_code",
    "canonical_element",
    "clean_token",
    "number",
]
