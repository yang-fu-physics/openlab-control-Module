"""LR-700/LR-720-16 的底层 VISA、命令构造与响应解析。

这里集中保存手册协议细节。模块生命周期、通道轮转、查询重试、数据异常降级和
最低激励恢复仍由 ``backend.py`` 决定。
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from typing import Protocol

from .constants import SAFE_EXCITATION_INDEX, SAFE_EXCITATION_PERCENT


class Transport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """只在模块 worker 中持有的 PyVISA GPIB 会话。"""

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


GET_RESISTANCE = "GET 0"
GET_REACTANCE = "GET 1"
GET_SETTINGS = "GET 6"
GET_OVERLOADS = "GET 7"

_SETTINGS_RE = re.compile(
    r"^\s*(\d)R\s*,\s*(\d)E\s*,\s*(\d{1,3})%\s*,"
    r"\s*(\d)F(?:\([^)]*\))?\s*,\s*(\d)M\s*,"
    r"\s*(\d)L\s*,\s*(\d{1,2})S\s*$",
    re.IGNORECASE,
)
_MEASUREMENT_RE = re.compile(
    r"^\s*([+-]?)\s*(\d+(?:\.\d*)?|\.\d+)\s*"
    r"([KMU]?)\s*OHM\s*(R|X|DR|DX|RSET|XSET)\s*$",
    re.IGNORECASE,
)
_OVERLOAD_RE = re.compile(r"^\s*(\d{1,3})\s+OVERLOADS\s*$", re.IGNORECASE)


def variable_excitation_commands(percent: object) -> tuple[str, ...]:
    value = int(percent)
    if value == 100:
        return ("VAREXC 0",)
    return (f"VAREXC ={value:02d}", "VAREXC 1")


def configuration_commands(
    input_channel: object,
    channel: Mapping[str, object],
) -> tuple[str, ...]:
    """生成当前传感器的完整绝对配置，不依赖桥内旧状态。"""

    return (
        "AUTORANGE 0",
        "MODE 0",
        f"SELECT S={int(input_channel):02d}",
        f"RANGE {int(channel['range_index'])}",
        f"EXCITATION {int(channel['excitation_index'])}",
        *variable_excitation_commands(channel["excitation_percent"]),
        f"FILTER {int(channel['filter_index'])}",
    )


def safe_state_commands() -> tuple[str, ...]:
    """LR-700 没有 Off；返回可读回确认的最低激励绝对命令。"""

    return (
        "AUTORANGE 0",
        f"EXCITATION {SAFE_EXCITATION_INDEX}",
        f"VAREXC ={SAFE_EXCITATION_PERCENT:02d}",
        "VAREXC 1",
    )


def parse_bridge_settings(reply: str) -> dict[str, int]:
    match = _SETTINGS_RE.fullmatch(reply)
    if match is None:
        raise ValueError(f"undocumented GET 6 response {reply!r}")
    (
        range_index,
        excitation_index,
        excitation_percent,
        filter_index,
        mode,
        local_lockout,
        sensor,
    ) = (int(value, 10) for value in match.groups())
    if not (
        0 <= range_index <= 9
        and 0 <= excitation_index <= 6
        and 0 <= excitation_percent <= 100
        and 0 <= filter_index <= 3
        and 0 <= mode <= 1
        and 0 <= local_lockout <= 1
        and 0 <= sensor <= 99
    ):
        raise ValueError(f"out-of-range GET 6 field in {reply!r}")
    return {
        "range_index": range_index,
        "excitation_index": excitation_index,
        "excitation_percent": excitation_percent,
        "filter_index": filter_index,
        "mode": mode,
        "local_lockout": local_lockout,
        "sensor": sensor,
    }


def parse_measurement(
    reply: str,
    expected_parameter: str,
    range_index: int,
) -> float:
    """把桥显示值及 K/M/U 前缀转换为 Ohm。"""

    match = _MEASUREMENT_RE.fullmatch(reply)
    if match is None:
        raise ValueError(f"undocumented measurement response {reply!r}")
    sign, number, multiplier, parameter = match.groups()
    if parameter.upper() != expected_parameter.upper():
        raise ValueError(
            f"response parameter {parameter!r} does not match "
            f"{expected_parameter!r}"
        )
    value = float(number)
    if sign == "-":
        value = -value
    prefix = multiplier.upper()
    if prefix == "":
        factor = 1.0
    elif prefix == "U":
        factor = 1.0e-6
    elif prefix == "K":
        factor = 1.0e3
    elif prefix == "M" and range_index <= 2:
        factor = 1.0e-3
    elif prefix == "M" and range_index == 9:
        factor = 1.0e6
    elif prefix == "M":
        raise ValueError(f"ambiguous M multiplier on range {range_index}")
    else:  # pragma: no cover - 正则已经排除其他前缀
        raise AssertionError(prefix)
    result = value * factor
    if not math.isfinite(result):
        raise ValueError(f"non-finite measurement response {reply!r}")
    return result


def parse_overloads(reply: str) -> int:
    match = _OVERLOAD_RE.fullmatch(reply)
    if match is None:
        raise ValueError(f"undocumented GET 7 response {reply!r}")
    bits = int(match.group(1), 10)
    if not 0 <= bits <= 255:
        raise ValueError(f"overload word outside 0-255: {bits}")
    return bits


__all__ = [
    "Transport",
    "PyVisaTransport",
    "GET_RESISTANCE",
    "GET_REACTANCE",
    "GET_SETTINGS",
    "GET_OVERLOADS",
    "configuration_commands",
    "variable_excitation_commands",
    "safe_state_commands",
    "parse_bridge_settings",
    "parse_measurement",
    "parse_overloads",
]
