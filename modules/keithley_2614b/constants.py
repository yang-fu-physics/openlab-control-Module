"""Keithley 2614B 双通道模块的协议常量与零输出默认设置。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final


SOURCE_CURRENT: Final = "current"
SOURCE_VOLTAGE: Final = "voltage"
SENSE_2WIRE: Final = "2wire"
SENSE_4WIRE: Final = "4wire"

CHANNELS: Final[tuple[tuple[str, str, int], ...]] = (
    ("ch1", "smua", 1),
    ("ch2", "smub", 2),
)

# 2600B 用户手册对 2611B/2612B/2614B 给出的实际 source capability。
DEVICE_MAX_CURRENT_A: Final = 1.515
DEVICE_MAX_VOLTAGE_V: Final = 202.0
DEVICE_HIGH_CURRENT_THRESHOLD_A: Final = 0.1
DEVICE_HIGH_VOLTAGE_THRESHOLD_V: Final = 20.0
DEVICE_HIGH_CURRENT_MAX_VOLTAGE_LIMIT_V: Final = 20.0
DEVICE_HIGH_VOLTAGE_MAX_CURRENT_LIMIT_A: Final = 0.1
DEVICE_MAX_POWER_W: Final = 30.603

STATUS_CODE_NORMAL: Final = 0
STATUS_CODE_OVER_RANGE: Final = 1
STATUS_CODE_COMPLIANCE: Final = 2
STATUS_CODE_INVALID_READING: Final = 3


def default_channel_settings() -> dict[str, Any]:
    """一个 SMU 通道的零输出配置。"""

    return {
        "enabled": False,
        "source_mode": SOURCE_CURRENT,
        "source_current": "0",
        "voltage_limit": "10",
        "source_voltage": "0",
        "current_limit": "1m",
        "sense_mode": SENSE_2WIRE,
        "nplc": 1.0,
    }


def default_settings() -> dict[str, Any]:
    """默认只启用 CH1，两个通道的非零源值都为零。"""

    channel = default_channel_settings()
    channels = {
        "ch1": deepcopy(channel),
        "ch2": deepcopy(channel),
    }
    channels["ch1"]["enabled"] = True
    return {
        "resource": "",
        "io_timeout_seconds": 2.0,
        "settle_seconds": 0.2,
        "output_off_between_measurements": True,
        "channels": channels,
    }


__all__ = [
    "CHANNELS",
    "DEVICE_HIGH_CURRENT_MAX_VOLTAGE_LIMIT_V",
    "DEVICE_HIGH_CURRENT_THRESHOLD_A",
    "DEVICE_HIGH_VOLTAGE_MAX_CURRENT_LIMIT_A",
    "DEVICE_HIGH_VOLTAGE_THRESHOLD_V",
    "DEVICE_MAX_CURRENT_A",
    "DEVICE_MAX_POWER_W",
    "DEVICE_MAX_VOLTAGE_V",
    "SENSE_2WIRE",
    "SENSE_4WIRE",
    "SOURCE_CURRENT",
    "SOURCE_VOLTAGE",
    "STATUS_CODE_COMPLIANCE",
    "STATUS_CODE_INVALID_READING",
    "STATUS_CODE_NORMAL",
    "STATUS_CODE_OVER_RANGE",
    "default_channel_settings",
    "default_settings",
]
