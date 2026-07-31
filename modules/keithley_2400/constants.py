"""Keithley 2400 模块共用常量和无输出默认设置。

这里的数值只描述 Model 2400 本身能够接受的绝对命令范围，不是样品安全上限。
2400 的实际连续功率还受 22 W 工作边界约束，仪表会按当前量程和 compliance 限制
输出；模块不会伪造一个未经真实接线验证的样品功率限制。
"""

from __future__ import annotations

from typing import Any, Final


SOURCE_CURRENT: Final = "current"
SOURCE_VOLTAGE: Final = "voltage"
SENSE_2WIRE: Final = "2wire"
SENSE_4WIRE: Final = "4wire"

# Series 2400 用户手册给出的 Model 2400 实际最大输出幅度。
DEVICE_MAX_CURRENT_A: Final = 1.05
DEVICE_MAX_VOLTAGE_V: Final = 210.0
DEVICE_MAX_CONTINUOUS_POWER_W: Final = 22.0

# DAT 状态码只表示本行数据质量；框架 Warning/Error 仍使用独立事件。
STATUS_CODE_NORMAL: Final = 0
STATUS_CODE_OVER_RANGE: Final = 1
STATUS_CODE_COMPLIANCE: Final = 2
STATUS_CODE_INVALID_READING: Final = 3


def default_settings() -> dict[str, Any]:
    """返回首次安装时的安全设置。

    两种源模式分别保存自己的源值与 compliance，切换模式不会把 1 mA 误解释成
    1 mV。两个源值都默认为零；Enable 和加载设置不会把这些值写入仪表。
    """

    return {
        "resource": "",
        "io_timeout_seconds": 3.0,
        "source_mode": SOURCE_CURRENT,
        "source_current": "0",
        "voltage_compliance": "10",
        "source_voltage": "0",
        "current_compliance": "1m",
        "sense_mode": SENSE_2WIRE,
        "nplc": 1.0,
        "settle_seconds": 0.2,
        "output_off_between_measurements": True,
    }


__all__ = [
    "DEVICE_MAX_CONTINUOUS_POWER_W",
    "DEVICE_MAX_CURRENT_A",
    "DEVICE_MAX_VOLTAGE_V",
    "SENSE_2WIRE",
    "SENSE_4WIRE",
    "SOURCE_CURRENT",
    "SOURCE_VOLTAGE",
    "STATUS_CODE_COMPLIANCE",
    "STATUS_CODE_INVALID_READING",
    "STATUS_CODE_NORMAL",
    "STATUS_CODE_OVER_RANGE",
    "default_settings",
]
