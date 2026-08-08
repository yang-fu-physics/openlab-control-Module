"""由 6221 RS-232 转发控制的 Keithley 2182A 命令与读数解析。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping


IDENTIFY = "*IDN?"
RANGE_SETTING = "VOLT:RANG"
NPLC_SETTING = "VOLT:NPLC"
RANGE_AUTO_QUERY = "VOLT:RANG:AUTO?"
RANGE_QUERY = "VOLT:RANG?"
NPLC_QUERY = "VOLT:NPLC?"
ANALOG_FILTER_QUERY = "VOLT:LPAS?"
DIGITAL_FILTER_QUERY = "VOLT:DFIL:STAT?"

_TRACE_TOKEN = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*[vV])?$"
)


def validate_identity(identity: str) -> bool:
    normalized = identity.upper()
    return "KEITHLEY" in normalized and "2182A" in normalized


def number(value: object) -> str:
    return f"{float(value):.12g}"


def range_commands(voltage_range: str, ranges: Mapping[str, float | None]) -> tuple[str, ...]:
    if voltage_range == "auto":
        return ("VOLT:RANG:AUTO ON",)
    value = ranges[voltage_range]
    if value is None:
        raise ValueError(f"fixed range {voltage_range!r} has no value")
    return ("VOLT:RANG:AUTO OFF", f"VOLT:RANG {number(value)}")


def filter_commands(settings: Mapping[str, object]) -> tuple[str, ...]:
    analog = "ON" if bool(settings["analog_filter_enabled"]) else "OFF"
    digital = "ON" if bool(settings["digital_filter_enabled"]) else "OFF"
    return (
        f"VOLT:LPAS {analog}",
        "VOLT:DFIL:TCON MOV",
        f"VOLT:DFIL:COUN {int(settings['digital_filter_count'])}",
        f"VOLT:DFIL:WIND {number(settings['digital_filter_window_percent'])}",
        f"VOLT:DFIL:STAT {digital}",
    )


def nplc_command(value: object) -> str:
    return f"VOLT:NPLC {int(value)}"


def parse_trace(reply: str, expected_count: int) -> tuple[tuple[float, ...], tuple[str, ...], bool, bool]:
    """返回数值、问题说明、格式错误标志和量程溢出标志。"""

    stripped = reply.strip()
    tokens = re.split(r"[,;\r\n]+", stripped) if stripped else []
    values: list[float] = []
    issues: list[str] = []
    invalid = False
    over_range = False
    for index, token in enumerate(tokens, start=1):
        item = token.strip()
        if _TRACE_TOKEN.fullmatch(item) is None:
            invalid = True
            issues.append(f"sample {index} is not numeric ({item!r})")
            continue
        try:
            value = float(re.sub(r"[vV]\s*$", "", item).strip())
        except ValueError:
            invalid = True
            issues.append(f"sample {index} cannot be parsed")
            continue
        if not math.isfinite(value):
            invalid = True
            issues.append(f"sample {index} is not finite")
            continue
        values.append(value)
        if abs(value) > 120.0:
            over_range = True
            issues.append(f"sample {index} exceeds the 2182A range")
    if len(tokens) != expected_count:
        invalid = True
        issues.append(f"expected {expected_count} samples, received {len(tokens)}")
    if len(values) != len(tokens):
        invalid = True
        issues.append(f"only {len(values)} samples were numeric")
    return tuple(values), tuple(dict.fromkeys(issues)), invalid, over_range


__all__ = [
    "IDENTIFY",
    "RANGE_SETTING",
    "NPLC_SETTING",
    "RANGE_AUTO_QUERY",
    "RANGE_QUERY",
    "NPLC_QUERY",
    "ANALOG_FILTER_QUERY",
    "DIGITAL_FILTER_QUERY",
    "validate_identity",
    "range_commands",
    "filter_commands",
    "nplc_command",
    "parse_trace",
]
