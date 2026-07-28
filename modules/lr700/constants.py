"""Linear Research LR-700 与 LR-720-16 的协议枚举和默认设置。

这里的整数不是界面列表位置，而是 LR-700 手册规定的命令参数。协议表一旦写错，
界面仍可能看似正常但会把错误量程或激励发给真实仪表，因此后端和测试都直接复用
这些元组，不在其他文件重新维护第二份映射。
"""

from __future__ import annotations

from typing import Any


# RANGE <0-9>：索引、界面文字、对应满量程电阻（Ohm）。
RESISTANCE_RANGES: tuple[
    tuple[int, str, float],
    ...,
] = (
    (0, "2 mOhm", 2.0e-3),
    (1, "20 mOhm", 20.0e-3),
    (2, "200 mOhm", 200.0e-3),
    (3, "2 Ohm", 2.0),
    (4, "20 Ohm", 20.0),
    (5, "200 Ohm", 200.0),
    (6, "2 kOhm", 2.0e3),
    (7, "20 kOhm", 20.0e3),
    (8, "200 kOhm", 200.0e3),
    (9, "2 MOhm", 2.0e6),
)

# EXCITATION <0-6>：索引、界面文字、100% 时的满量程激励电压（V）。
EXCITATIONS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (0, "20 uV", 20.0e-6),
    (1, "60 uV", 60.0e-6),
    (2, "200 uV", 200.0e-6),
    (3, "600 uV", 600.0e-6),
    (4, "2 mV", 2.0e-3),
    (5, "6 mV", 6.0e-3),
    (6, "20 mV", 20.0e-3),
)

# 只开放手册内置的三种数字滤波器。用户自定义 FILTER 3 还带第二个时间索引，
# 其 GET 6 回读是可变字符串；首个真机版本先不引入这一额外歧义。
FILTERS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (0, "Off", 0.0),
    (1, "1 s", 1.0),
    (2, "10 s", 10.0),
)

# GET 7 的 8 位 OVERLOADS 状态字。名称保留手册语义，DAT 中再归一化为
# NORMAL / OVER_RANGE / OVERLOAD，完整位名称同时出现在 Status 和 Warning。
OVERLOAD_BITS: tuple[tuple[int, str], ...] = (
    (1, "+dX overrange"),
    (2, "-dX overrange"),
    (4, "Common-mode overload +"),
    (8, "Common-mode overload -"),
    (16, "I-HIGH voltage overload"),
    (32, "Tuned-amplifier input overload"),
    (64, "R overrange"),
    (128, "dR overrange"),
)

# LR-700 没有 excitation-off 命令。手册允许的最低可确认状态是 20 uV 的 5%。
SAFE_EXCITATION_INDEX = 0
SAFE_EXCITATION_PERCENT = 5


def default_channel(slot: int) -> dict[str, Any]:
    """返回一个 R1-R4 逻辑槽位的独立默认设置字典。

    每个槽位可选择 LR-720-16 的一个物理输入。默认只启用 R1，并把 R1-R4 映射到
    物理输入 1-4；最低激励和 200 Ohm 都只是保守初值，接入样品前必须人工确认。
    """

    return {
        "input_channel": slot,
        "enabled": slot == 1,
        "range_index": 5,
        "excitation_index": SAFE_EXCITATION_INDEX,
        "excitation_percent": SAFE_EXCITATION_PERCENT,
        "filter_index": 1,
    }


def default_settings() -> dict[str, Any]:
    """返回新安装模块的初始设置。

    Enable 只加载这份 desired settings 并发现 GPIB 资源，不会连接或发命令。真正
    Apply 后也只把桥置于最低激励；每个通道的量程、滤波和激励在 Measure 中切换并
    逐次读回确认。
    """

    return {
        "resource": "",
        "switch_settle_seconds": 2.0,
        "dwell_seconds": 1.0,
        "io_timeout_seconds": 3.0,
        "retry_attempts": 2,
        "channels": {
            f"r{slot}": default_channel(slot)
            for slot in range(1, 5)
        },
    }


__all__ = [
    "EXCITATIONS",
    "FILTERS",
    "OVERLOAD_BITS",
    "RESISTANCE_RANGES",
    "SAFE_EXCITATION_INDEX",
    "SAFE_EXCITATION_PERCENT",
    "default_channel",
    "default_settings",
]
