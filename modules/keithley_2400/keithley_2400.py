"""Keithley 2400 的底层 VISA、SCPI 指令和响应解析。

本文件只描述“如何与 2400 说话”，不决定 SEQ 生命周期、通道调度、Warning/Error
级别或失败后的重试策略。``backend.py`` 负责这些流程，并通过这里导出的绝对命令与
纯解析函数执行协议。
"""

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
    """仅在模块 worker 内创建的有限超时 GPIB 会话。"""

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
OUTPUT_QUERY = "OUTP?"
OUTPUT_ON = "OUTP ON"
OUTPUT_OFF = "OUTP OFF"
SOURCE_FUNCTION_QUERY = "SOUR:FUNC?"
SOURCE_CURRENT_LEVEL_QUERY = "SOUR:CURR:LEV?"
SOURCE_VOLTAGE_LEVEL_QUERY = "SOUR:VOLT:LEV?"
VOLTAGE_COMPLIANCE_QUERY = "SENS:VOLT:PROT?"
CURRENT_COMPLIANCE_QUERY = "SENS:CURR:PROT?"
CONCURRENT_QUERY = "SENS:FUNC:CONC?"
SENSE_FUNCTIONS_QUERY = "SENS:FUNC:ON?"
VOLTAGE_NPLC_QUERY = "SENS:VOLT:NPLC?"
CURRENT_NPLC_QUERY = "SENS:CURR:NPLC?"
REMOTE_SENSE_QUERY = "SYST:RSEN?"
DATA_ELEMENTS_QUERY = "FORM:ELEM?"
READ = "READ?"
VOLTAGE_TRIP_QUERY = "SENS:VOLT:PROT:TRIP?"
CURRENT_TRIP_QUERY = "SENS:CURR:PROT:TRIP?"
ERROR_QUERY = "SYST:ERR?"


def number(value: object) -> str:
    """生成不依赖区域设置的有限精度 SCPI 数值。"""

    return f"{float(value):.12g}"


def configuration_commands(settings: Mapping[str, object]) -> tuple[str, ...]:
    """生成完整的绝对配置命令，不使用会累积或依赖前态的相对操作。"""

    commands = [
        "SENS:FUNC:CONC ON",
        "SENS:FUNC:OFF:ALL",
        "SENS:FUNC:ON 'VOLT:DC','CURR:DC'",
    ]
    if settings["source_mode"] == SOURCE_CURRENT:
        commands.extend(
            (
                "SOUR:FUNC CURR",
                "SOUR:CURR:MODE FIX",
                "SOUR:CURR:RANG:AUTO ON",
                f"SOUR:CURR:LEV {number(settings['source_current'])}",
                f"SENS:VOLT:PROT {number(settings['voltage_compliance'])}",
                "SENS:VOLT:RANG:AUTO ON",
            )
        )
    else:
        commands.extend(
            (
                "SOUR:FUNC VOLT",
                "SOUR:VOLT:MODE FIX",
                "SOUR:VOLT:RANG:AUTO ON",
                f"SOUR:VOLT:LEV {number(settings['source_voltage'])}",
                f"SENS:CURR:PROT {number(settings['current_compliance'])}",
                "SENS:CURR:RANG:AUTO ON",
            )
        )
    commands.extend(
        (
            f"SENS:VOLT:NPLC {number(settings['nplc'])}",
            f"SENS:CURR:NPLC {number(settings['nplc'])}",
            "SYST:RSEN ON" if settings["sense_mode"] == SENSE_4WIRE else "SYST:RSEN OFF",
            "FORM:DATA ASC",
            "FORM:ELEM VOLT,CURR",
        )
    )
    return tuple(commands)


def compliance_query(source_mode: object) -> str:
    return (
        VOLTAGE_TRIP_QUERY
        if source_mode == SOURCE_CURRENT
        else CURRENT_TRIP_QUERY
    )


def output_command(enabled: bool) -> str:
    return OUTPUT_ON if enabled else OUTPUT_OFF


def validate_identity(identity: str) -> bool:
    normalized = " ".join(identity.upper().replace(",", " ").split())
    return "KEITHLEY" in normalized and bool(
        re.search(r"\bMODEL\s*2400\b|\b2400\b", normalized)
    )


def clean_token(value: object) -> str:
    return str(value).strip().strip('"').strip("'").upper()


def parse_switch(value: object) -> bool:
    token = str(value).strip().strip('"').upper()
    if token in {"1", "ON"}:
        return True
    if token in {"0", "OFF"}:
        return False
    raise ValueError(f"invalid switch state {value!r}")


def parse_voltage_current(reply: str) -> tuple[float, float]:
    parts = [item.strip() for item in reply.split(",") if item.strip()]
    if len(parts) != 2:
        raise ValueError(f"expected VOLT,CURR, received {reply!r}")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(f"non-numeric VOLT,CURR response {reply!r}") from exc


def parse_error_code(reply: str) -> int:
    matched = re.match(r"\s*([+-]?\d+)", reply)
    if matched is None:
        raise ValueError(f"invalid error-queue response {reply!r}")
    return int(matched.group(1))


def parse_number(reply: str) -> float:
    value = float(reply)
    if not math.isfinite(value):
        raise ValueError(f"non-finite numeric response {reply!r}")
    return value


__all__ = [
    "Transport",
    "PyVisaTransport",
    "IDENTIFY",
    "CLEAR_STATUS",
    "OUTPUT_QUERY",
    "OUTPUT_OFF",
    "SOURCE_FUNCTION_QUERY",
    "SOURCE_CURRENT_LEVEL_QUERY",
    "SOURCE_VOLTAGE_LEVEL_QUERY",
    "VOLTAGE_COMPLIANCE_QUERY",
    "CURRENT_COMPLIANCE_QUERY",
    "CONCURRENT_QUERY",
    "SENSE_FUNCTIONS_QUERY",
    "VOLTAGE_NPLC_QUERY",
    "CURRENT_NPLC_QUERY",
    "REMOTE_SENSE_QUERY",
    "DATA_ELEMENTS_QUERY",
    "READ",
    "ERROR_QUERY",
    "configuration_commands",
    "compliance_query",
    "output_command",
    "validate_identity",
    "clean_token",
    "parse_switch",
    "parse_voltage_current",
    "parse_error_code",
    "parse_number",
]
