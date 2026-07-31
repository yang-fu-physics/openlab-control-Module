"""2614B 模块独立使用的 SI 前缀数值解析。"""

from __future__ import annotations

import math
import re
from typing import Final


_NUMBER = re.compile(
    r"""
    ^\s*
    (?P<number>[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?)
    \s*(?P<prefix>[fpnumkKMGTµμ]?)
    \s*(?P<unit>[A-Za-zΩΩ]*)\s*$
    """,
    re.VERBOSE,
)

PREFIX_FACTORS: Final[dict[str, float]] = {
    "": 1.0,
    "f": 1.0e-15,
    "p": 1.0e-12,
    "n": 1.0e-9,
    "u": 1.0e-6,
    "µ": 1.0e-6,
    "μ": 1.0e-6,
    "m": 1.0e-3,
    "k": 1.0e3,
    "K": 1.0e3,
    "M": 1.0e6,
    "G": 1.0e9,
    "T": 1.0e12,
}

_EXPONENT_PREFIX: Final[dict[int, str]] = {
    -15: "f",
    -12: "p",
    -9: "n",
    -6: "u",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
    12: "T",
}

_UNIT_ALIASES: Final[dict[str, frozenset[str]]] = {
    "A": frozenset({"a", "amp", "amps", "ampere", "amperes"}),
    "V": frozenset({"v", "volt", "volts"}),
    "s": frozenset({"s", "sec", "secs", "second", "seconds"}),
}


def parse_quantity(value: object, *, expected_unit: str = "") -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric quantity")
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("quantity must be finite")
        return result
    if not isinstance(value, str):
        raise ValueError("quantity must be a number or text")
    matched = _NUMBER.fullmatch(value)
    if matched is None:
        raise ValueError("use a number such as 1m, 500u, 1p or 1e-6")
    explicit_unit = matched.group("unit")
    if explicit_unit and expected_unit:
        allowed = _UNIT_ALIASES.get(
            expected_unit,
            frozenset({expected_unit.casefold()}),
        )
        if explicit_unit.casefold() not in allowed:
            raise ValueError(f"expected {expected_unit}, got {explicit_unit}")
    result = (
        float(matched.group("number"))
        * PREFIX_FACTORS[matched.group("prefix")]
    )
    if not math.isfinite(result):
        raise ValueError("quantity must be finite")
    return result


def format_quantity(value: object, *, expected_unit: str = "") -> str:
    number = parse_quantity(value, expected_unit=expected_unit)
    if number == 0:
        return "0"
    exponent = int(math.floor(math.log10(abs(number)) / 3.0) * 3)
    exponent = max(min(exponent, 12), -15)
    scaled = number / (10.0**exponent)
    return f"{scaled:.9g}{_EXPONENT_PREFIX[exponent]}"


__all__ = ["PREFIX_FACTORS", "format_quantity", "parse_quantity"]
