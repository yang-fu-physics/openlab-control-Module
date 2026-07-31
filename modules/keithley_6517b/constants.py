"""Keithley 6517B 模块的协议枚举、状态码和安全默认值。"""

from __future__ import annotations

from typing import Any, Final


SOURCE_RANGE_100_V: Final = "100v"
SOURCE_RANGE_1000_V: Final = "1000v"
SOURCE_RANGES: Final[dict[str, float]] = {
    SOURCE_RANGE_100_V: 100.0,
    SOURCE_RANGE_1000_V: 1000.0,
}

STATUS_CODE_NORMAL: Final = 0
STATUS_CODE_OVER_RANGE: Final = 1
STATUS_CODE_COMPLIANCE: Final = 2
STATUS_CODE_INVALID_READING: Final = 3


def default_settings() -> dict[str, Any]:
    """返回无高压输出的默认设置。

    METER-CONNECT 不暴露为设置项：本模块的 FVMI 接线固定要求它为 ON，并在每次输出前
    读回验证。默认 V-source 为 0 V，Enable 不连接，Apply 结束保持 standby。
    """

    return {
        "resource": "",
        "io_timeout_seconds": 3.0,
        "source_range": SOURCE_RANGE_100_V,
        "source_voltage": "0",
        "voltage_limit": "100",
        "nplc": 1.0,
        "settle_seconds": 1.0,
        "output_off_between_measurements": True,
    }


__all__ = [
    "SOURCE_RANGE_1000_V",
    "SOURCE_RANGE_100_V",
    "SOURCE_RANGES",
    "STATUS_CODE_COMPLIANCE",
    "STATUS_CODE_INVALID_READING",
    "STATUS_CODE_NORMAL",
    "STATUS_CODE_OVER_RANGE",
    "default_settings",
]
