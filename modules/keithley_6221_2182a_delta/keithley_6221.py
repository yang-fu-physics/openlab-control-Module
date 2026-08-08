"""Keithley 6221 的 VISA 会话、Delta 命令与通用响应解析。"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from typing import Protocol


class Transport(Protocol):
    def write(self, command: str) -> None: ...

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """支持为单次长查询临时扩大 timeout 的 PyVISA 会话。"""

    def __init__(self, resource: str, timeout_seconds: float) -> None:
        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        try:
            self._instrument = self._manager.open_resource(resource)
            self._instrument.timeout = max(1, int(float(timeout_seconds) * 1000))
            self._instrument.write_termination = "\n"
            self._instrument.read_termination = "\n"
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

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str:
        if timeout_seconds is None:
            return str(self._instrument.query(command))
        original = self._instrument.timeout
        self._instrument.timeout = max(1, int(float(timeout_seconds) * 1000))
        try:
            return str(self._instrument.query(command))
        finally:
            self._instrument.timeout = original

    def close(self) -> None:
        try:
            self._instrument.close()
        finally:
            self._manager.close()


IDENTIFY = "*IDN?"
NANOVOLTMETER_PRESENT_QUERY = "SOUR:DELT:NVPRESENT?"
ARM = "SOUR:DELT:ARM"
ARM_QUERY = "SOUR:DELT:ARM?"
TRIGGER = "INIT:IMM"
COMPLETE_QUERY = "*OPC?"
TRACE_QUERY = "TRAC:DATA?"
ABORT = "SOUR:SWE:ABOR"
CLEAR = "SOUR:CLE"
OUTPUT_QUERY = "OUTP?"
CURRENT_QUERY = "SOUR:CURR?"
ERROR_QUERY = "SYST:ERR?"
SERIAL_ENTER_QUERY = "SYST:COMM:SER:ENT?"
COMPLIANCE_QUERY = "SOUR:CURR:COMP?"
HIGH_CURRENT_QUERY = "SOUR:DELT:HIGH?"
LOW_CURRENT_QUERY = "SOUR:DELT:LOW?"
DELAY_QUERY = "SOUR:DELT:DEL?"
COUNT_QUERY = "SOUR:DELT:COUN?"
COLD_SWITCH_QUERY = "SOUR:DELT:CSW?"
COMPLIANCE_ABORT_QUERY = "SOUR:DELT:CAB?"


def number(value: object) -> str:
    return f"{float(value):.12g}"


def validate_identity(identity: str) -> bool:
    normalized = identity.upper()
    return "KEITHLEY" in normalized and "6221" in normalized


def configuration_commands(settings: Mapping[str, object]) -> tuple[str, ...]:
    """生成一次完整 Delta 配置；所有命令均为绝对设置。"""

    count = int(settings["count"])
    return (
        "UNIT V",
        "FORM:DATA ASC",
        "FORM:ELEM READ",
        "SENS:AVER:STAT OFF",
        f"SOUR:CURR:COMP {number(settings['compliance'])}",
        f"SOUR:DELT:HIGH {number(settings['high_current'])}",
        f"SOUR:DELT:LOW {number(settings['low_current'])}",
        f"SOUR:DELT:DEL {number(settings['delta_delay'])}",
        f"SOUR:DELT:COUN {count}",
        "SOUR:SWE:COUN 1",
        "SOUR:DELT:CSW ON",
        "SOUR:DELT:CAB ON",
        f"TRAC:POIN {count}",
    )


def serial_send(command: str) -> str:
    escaped = command.replace('"', '""')
    return f'SYST:COMM:SER:SEND "{escaped}"'


def parse_switch(reply: str) -> bool:
    normalized = reply.strip().upper()
    if normalized in {"1", "+1", "ON"}:
        return True
    if normalized in {"0", "+0", "OFF"}:
        return False
    raise ValueError(f"invalid switch response {reply!r}")


def parse_number(reply: str) -> float:
    token = reply.strip().split(",", 1)[0].strip()
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite numeric response {reply!r}")
    return value


def parse_error_code(reply: str) -> int:
    matched = re.match(r"^\s*([+-]?\d+)\s*(?:,|$)", reply)
    if matched is None:
        raise ValueError(f"invalid error-queue response {reply!r}")
    return int(matched.group(1))


__all__ = [
    "Transport",
    "PyVisaTransport",
    "IDENTIFY",
    "NANOVOLTMETER_PRESENT_QUERY",
    "ARM",
    "ARM_QUERY",
    "TRIGGER",
    "COMPLETE_QUERY",
    "TRACE_QUERY",
    "ABORT",
    "CLEAR",
    "OUTPUT_QUERY",
    "CURRENT_QUERY",
    "ERROR_QUERY",
    "SERIAL_ENTER_QUERY",
    "COMPLIANCE_QUERY",
    "HIGH_CURRENT_QUERY",
    "LOW_CURRENT_QUERY",
    "DELAY_QUERY",
    "COUNT_QUERY",
    "COLD_SWITCH_QUERY",
    "COMPLIANCE_ABORT_QUERY",
    "configuration_commands",
    "serial_send",
    "parse_switch",
    "parse_number",
    "parse_error_code",
]
