"""Keithley Delta 模块的协议常量和无输出默认设置。

默认设置不会产生非零电流。Enable 只发现资源并判断 7001 是否可用；模块不再维护
用户可配置的软件电流或 compliance 上限，但仍会按仪表手册拒绝设备本身无法接受的
命令范围。样品、接线和允许功耗的实验安全边界应在真实仪表上人工配置并核对。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final


MODE_SHARED: Final = "shared_armed"
MODE_INDEPENDENT: Final = "independent_rearm"
ARM_SETTLE_SECONDS: Final = 3.0
# 核心单条 IPC 消息上限为 1 MiB。32,768 个最坏长度的 JSON float 加上事件
# 元数据仍可安全放入一帧；再高会在结果写盘前因消息过大而失败。
MAX_DELTA_COUNT: Final = 32_768

# 6221 用户手册给出的 Delta 和 compliance 绝对仪表边界。
DEVICE_CURRENT_LIMIT_A: Final = 105.0e-3
DEVICE_COMPLIANCE_MIN_V: Final = 0.1
DEVICE_COMPLIANCE_MAX_V: Final = 105.0

# 2182A DCV1 的五个固定量程；None 表示自动量程。
VOLTAGE_RANGES: Final[
    tuple[tuple[str, str, float | None], ...]
] = (
    ("auto", "Auto range", None),
    ("10mv", "10 mV", 10.0e-3),
    ("100mv", "100 mV", 100.0e-3),
    ("1v", "1 V", 1.0),
    ("10v", "10 V", 10.0),
    ("100v", "100 V", 100.0),
)

# 2182A 手册明确要求 Delta 测量使用 Moving filter；Repeating 用于 stepping/
# scanning。模块不提供一个会被仪表忽略或改写的无效选择。
FILTER_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("moving", "Moving (required for Delta)"),
)

# DAT 状态码由本模块解释。代码 2 预留给未来经过真机验证、能够明确识别的 6221
# compliance 事件；不能把任意 SYST:ERR? 文本猜测成 compliance。
STATUS_CODE_NORMAL: Final = 0
STATUS_CODE_OVER_RANGE: Final = 1
STATUS_CODE_COMPLIANCE: Final = 2
STATUS_CODE_INVALID_TRACE: Final = 3


def default_delta_settings() -> dict[str, Any]:
    """返回一套不会输出非零电流的 Delta 设置。"""

    return {
        "high_current": "0",
        "low_current": "0",
        "compliance": "1",
        "delta_delay": "2m",
        "count": 1,
        "voltage_range": "auto",
        # 6221 在 ARM 时会把非整数 PLC 自动改成 1 PLC；界面因此只允许整数。
        "nplc": 1,
        "analog_filter_enabled": False,
        "digital_filter_enabled": False,
        "digital_filter_type": "moving",
        "digital_filter_count": 10,
        "digital_filter_window_percent": 0.01,
    }


def default_settings() -> dict[str, Any]:
    """返回模块首次安装时的完整 desired settings。"""

    delta = default_delta_settings()
    return {
        "resource_6221": "",
        "resource_7001": "",
        "mode": MODE_SHARED,
        "io_timeout_seconds": 3.0,
        "switch_settle_seconds": 0.5,
        "channels": {
            f"ch{index}": {
                "enabled": index == 1,
            }
            for index in range(1, 5)
        },
        "shared": deepcopy(delta),
        "independent": {
            f"ch{index}": deepcopy(delta)
            for index in range(1, 5)
        },
    }


__all__ = [
    "ARM_SETTLE_SECONDS",
    "DEVICE_COMPLIANCE_MAX_V",
    "DEVICE_COMPLIANCE_MIN_V",
    "DEVICE_CURRENT_LIMIT_A",
    "FILTER_TYPES",
    "MAX_DELTA_COUNT",
    "MODE_INDEPENDENT",
    "MODE_SHARED",
    "STATUS_CODE_COMPLIANCE",
    "STATUS_CODE_INVALID_TRACE",
    "STATUS_CODE_NORMAL",
    "STATUS_CODE_OVER_RANGE",
    "VOLTAGE_RANGES",
    "default_delta_settings",
    "default_settings",
]
