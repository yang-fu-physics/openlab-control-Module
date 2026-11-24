"""Linear Research LR-700 与 LR-720-16 的协议枚举、合法组合与默认设置。

这里的整数不是界面列表位置，而是 LR-700 v1.3 手册规定的命令参数。协议表一旦写错，
界面仍可能看似正常但会把错误量程或激励发给真实仪表，因此后端、前端和测试都直接复用
这些常量与辅助函数，不在其他文件重新维护第二份映射。
"""

from __future__ import annotations

from typing import Any


# RANGE <0-9>：索引、界面文字、对应满量程电阻（Ohm）。（手册 p. 3-46 / p. 4-12）
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

# EXCITATION <0-6>：索引、界面文字、100% 时的满量程激励电压（V）。（手册 p. 3-34 / p. 4-12）
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

# 手册 p. 4-4 / p. 4-12：电流源 100% 满量程范围为 1 nA 至 30 mA，
# 因此不同电阻量程各自仅允许满足 1 nA <= V_fs / R_fs <= 30 mA 的激励档。
# 200 kOhm 最低为 2 (200 uV)，2 MOhm 最低为 4 (2 mV)，已与实机逐档探测核对一致。
VALID_EXCITATION_INDICES_BY_RANGE: dict[int, tuple[int, ...]] = {
    0: (0, 1),
    1: (0, 1, 2, 3),
    2: (0, 1, 2, 3, 4, 5),
    3: (0, 1, 2, 3, 4, 5, 6),
    4: (0, 1, 2, 3, 4, 5, 6),
    5: (0, 1, 2, 3, 4, 5, 6),
    6: (0, 1, 2, 3, 4, 5, 6),
    7: (0, 1, 2, 3, 4, 5, 6),
    8: (2, 3, 4, 5, 6),
    9: (4, 5, 6),
}

# 手册 p. 2-6 / p. 3-35：固定数字滤波编码 0/1/2 对应 1 s / 3 s / 10 s，不存在 Off 档。
# 用户可变滤波 FILTER 3 带第二参数且 GET 6 回读附带括号时长文本，当前仅开放三个固定档。
FILTERS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (0, "1 s", 1.0),
    (1, "3 s", 3.0),
    (2, "10 s", 10.0),
)

# 切换等待下限：所有已启用通道中最长固定数字滤波时长的 3 倍（1 s -> 3 s，3 s -> 9 s，10 s -> 30 s）。
FILTER_SETTLE_MULTIPLIER = 3.0

# 通道量程模式：固定量程与仪表自动量程（LR-700-AR）。
# 自动量程使用操作者已在仪表上设置的 T=4 / U=0135666666；软件不改写 U= 或 T=。
# 下面的映射只用于恢复量程时同步激励，仅开放 20 mOhm (1R) 及以上量程。
RANGE_MODES: tuple[tuple[str, str], ...] = (
    ("fixed", "Fixed"),
    ("autorange", "Autorange"),
)

AUTORANGE_MIN_RANGE_INDEX = 1
AUTORANGE_MAX_RANGE_INDEX = 9

# 现场已确认的 10 档激励映射；更改仪表 Trace 时须同步此映射。
# 本模块自动量程仅允许 20 mOhm (1) 及以上量程，不使用索引 0 (2 mOhm)。
# 1: 20 mOhm  -> 1 (60 uV,  3 mA)
# 2: 200 mOhm -> 3 (600 uV, 3 mA)
# 3: 2 Ohm    -> 5 (6 mV,   3 mA)
# 4-9: 20 Ohm..2 MOhm -> 6 (20 mV, 1 mA..10 nA)
AUTORANGE_USER_TRACE_EXCITATIONS: tuple[int, ...] = (
    0,
    1,
    3,
    5,
    6,
    6,
    6,
    6,
    6,
    6,
)

# DAT 的 StatusCode 是本模块自有整数枚举。LR-700 没有 372A 的 current-source
# compliance 状态，因此代码 2 在本模块表示 overload，而不是跨模块统一含义。
STATUS_CODE_NORMAL = 0
STATUS_CODE_OVER_RANGE = 1
STATUS_CODE_OVERLOAD = 2
STATUS_CODE_INVALID_READING = 3

# GET 7 的 8 位 OVERLOADS 状态字（手册 p. 2-23 / p. 3-51）。名称保留手册语义，
# 完整位名称显示在 Warning 和状态页中；DAT 只写上面的整数状态码。
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

COMMON_MODE_OVERLOAD_MASK = 4 | 8
COMMON_MODE_OVERLOAD_HINT = (
    "电流端接触电阻可能过大，请检查 I-LOW 回路及接线。"
)

# LR-700 没有 excitation-off 命令。最低可确认状态为“当前电阻量程允许的最低激励档 x 5%”。
# 编码 0-7 对应 0 (20 uV)，编码 8 (200 kOhm) 对应 2 (200 uV)，编码 9 (2 MOhm) 对应 4 (2 mV)。
SAFE_EXCITATION_INDEX = 0
SAFE_EXCITATION_PERCENT = 5

_RANGE_BY_INDEX: dict[int, tuple[str, float]] = {
    index: (label, ohms)
    for index, label, ohms in RESISTANCE_RANGES
}
_EXCITATION_BY_INDEX: dict[int, tuple[str, float]] = {
    index: (label, volts)
    for index, label, volts in EXCITATIONS
}
_FILTER_BY_INDEX: dict[int, tuple[str, float]] = {
    index: (label, seconds)
    for index, label, seconds in FILTERS
}


def range_label(range_index: int) -> str:
    return _RANGE_BY_INDEX[int(range_index)][0]


def range_ohms(range_index: int) -> float:
    return _RANGE_BY_INDEX[int(range_index)][1]


def excitation_label(excitation_index: int) -> str:
    return _EXCITATION_BY_INDEX[int(excitation_index)][0]


def excitation_volts(excitation_index: int) -> float:
    return _EXCITATION_BY_INDEX[int(excitation_index)][1]


def filter_label(filter_index: int) -> str:
    return _FILTER_BY_INDEX[int(filter_index)][0]


def filter_seconds(filter_index: int) -> float:
    return _FILTER_BY_INDEX[int(filter_index)][1]


def valid_excitation_indices(range_index: int) -> tuple[int, ...]:
    """返回指定电阻量程索引（0-9）硬件允许的激励档编码元组。"""

    return VALID_EXCITATION_INDICES_BY_RANGE.get(int(range_index), ())


def minimum_excitation_index(range_index: int) -> int:
    """返回指定电阻量程索引允许的最低激励档编码（0-7 -> 0，8 -> 2，9 -> 4）。"""

    valid = valid_excitation_indices(range_index)
    if not valid:
        raise ValueError(f"invalid LR-700 range index: {range_index!r}")
    return valid[0]


def is_valid_range_excitation(
    range_index: int,
    excitation_index: int,
) -> bool:
    return int(excitation_index) in valid_excitation_indices(range_index)


def autorange_trace_excitation_index(range_index: int) -> int:
    """返回自定义自动量程 trace 表中指定电阻量程对应的满量程激励档编码。"""

    index = int(range_index)
    if not 0 <= index < len(AUTORANGE_USER_TRACE_EXCITATIONS):
        raise ValueError(f"invalid LR-700 range index: {range_index!r}")
    return AUTORANGE_USER_TRACE_EXCITATIONS[index]


def excitation_current_a(
    range_index: int,
    excitation_index: int,
    excitation_percent: int = 100,
) -> float:
    """计算配置对应的满量程激励电流 I = V_fs / R_fs * (percent / 100)（单位 A）。"""

    return (
        excitation_volts(excitation_index)
        / range_ohms(range_index)
        * (float(excitation_percent) / 100.0)
    )


def format_current(amperes: float) -> str:
    """把激励电流格式化为易读的 SI 文本（pA / nA / uA / mA / A）。"""

    magnitude = abs(amperes)
    for threshold, scale, unit in (
        (1.0, 1.0, "A"),
        (1.0e-3, 1.0e3, "mA"),
        (1.0e-6, 1.0e6, "uA"),
        (1.0e-9, 1.0e9, "nA"),
    ):
        if magnitude >= threshold:
            return f"{amperes * scale:.4g} {unit}"
    return f"{amperes * 1.0e12:.4g} pA"


def common_defaults() -> dict[str, Any]:
    return {
        "range_mode": "autorange", "range_index": 2,
        "current_a": 0.003, "current_limit_a": 0.003,
        "autorange_min_range": 1, "autorange_max_range": 9,
        "filter_index": 1,
    }


def generate_trace(limit_a: float, lo: int, hi: int) -> tuple[list[int], int, int]:
    """Trace uses 100%; exclude ranges whose minimum excitation exceeds the limit."""
    trace: list[int] = []
    usable: list[int] = []
    for r in range(10):
        allowed = valid_excitation_indices(r)
        candidates = [e for e in allowed if excitation_current_a(r, e) <= limit_a * (1 + 1e-12)]
        trace.append(candidates[-1] if candidates else allowed[0])
        if lo <= r <= hi and candidates:
            usable.append(r)
    if len(usable) < 2:
        raise ValueError("Current limit leaves fewer than two autorange ranges")
    return trace, usable[0], usable[-1]


def fixed_current_settings(range_index: int, current_a: float) -> tuple[int, int, float]:
    """Choose the closest representable current at or below the requested current."""
    import math
    candidates = []
    for e in valid_excitation_indices(range_index):
        full = excitation_current_a(range_index, e)
        percent = min(100, math.floor(current_a / full * 100 + 1e-10))
        if percent >= 5:
            candidates.append((e, percent, full * percent / 100))
    if not candidates:
        raise ValueError(f"Requested current is below the minimum on {range_label(range_index)}")
    return max(candidates, key=lambda item: item[2])


def default_channel(slot: int) -> dict[str, Any]:
    """返回一个 R1-R4 逻辑槽位的独立默认设置字典。

    每个槽位可选择 LR-720-16 的一个物理输入。默认只启用 R1，并把 R1-R4 映射到
    物理输入 1-4；最低激励和 200 Ohm 都只是保守初值，接入样品前必须人工确认。
    """

    return {
        "input_channel": slot,
        "enabled": slot == 1,
        "range_mode": "fixed",
        "range_index": 5,
        "excitation_index": SAFE_EXCITATION_INDEX,
        "excitation_percent": SAFE_EXCITATION_PERCENT,
        "autorange_min_range": 1,
        "autorange_max_range": 9,
        "filter_index": 1,
    }


def default_settings() -> dict[str, Any]:
    """返回新安装模块的初始设置。

    默认固定滤波为 FILTER 1（3 s），默认切换等待取其 3 倍（9.0 s）。
    Enable 只加载这份 desired settings 并发现 GPIB 资源，不会连接或发命令。
    """

    return {
        "resource": "",
        "switch_settle_seconds": 9.0,
        "dwell_seconds": 1.0,
        "io_timeout_seconds": 3.0,
        "retry_attempts": 2,
        "channels": {
            f"r{slot}": default_channel(slot)
            for slot in range(1, 5)
        },
    }


__all__ = [
    "AUTORANGE_MAX_RANGE_INDEX",
    "AUTORANGE_MIN_RANGE_INDEX",
    "AUTORANGE_USER_TRACE_EXCITATIONS",
    "COMMON_MODE_OVERLOAD_HINT",
    "COMMON_MODE_OVERLOAD_MASK",
    "EXCITATIONS",
    "FILTERS",
    "FILTER_SETTLE_MULTIPLIER",
    "OVERLOAD_BITS",
    "RANGE_MODES",
    "RESISTANCE_RANGES",
    "SAFE_EXCITATION_INDEX",
    "SAFE_EXCITATION_PERCENT",
    "STATUS_CODE_INVALID_READING",
    "STATUS_CODE_NORMAL",
    "STATUS_CODE_OVERLOAD",
    "STATUS_CODE_OVER_RANGE",
    "VALID_EXCITATION_INDICES_BY_RANGE",
    "autorange_trace_excitation_index",
    "default_channel",
    "default_settings",
    "excitation_current_a",
    "excitation_label",
    "excitation_volts",
    "filter_label",
    "filter_seconds",
    "format_current",
    "is_valid_range_excitation",
    "minimum_excitation_index",
    "range_label",
    "range_ohms",
    "valid_excitation_indices",
]
