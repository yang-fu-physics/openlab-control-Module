"""Lake Shore Model 372 协议枚举和模块默认设置。

元组第一项是仪表命令使用的离散索引，显示文字只用于 UI，第三项统一使用 SI 数值供
计算。不要仅按列表位置推导协议索引；修改表格时应同时对照 Model 372 手册和测试。
"""

from __future__ import annotations

from typing import Any


# FREQ 命令的离散频率索引。Model 372 的协议顺序并非按数值单调递增。
FREQUENCIES_HZ: tuple[tuple[int, float], ...] = (
    (1, 9.8),
    (2, 13.7),
    (3, 16.2),
    (4, 11.6),
    (5, 18.2),
)

# INTYPE 电流激励量程：协议索引、UI 标签、安培值。
CURRENT_EXCITATIONS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "1.00 pA", 1.00e-12),
    (2, "3.16 pA", 3.16e-12),
    (3, "10.0 pA", 10.0e-12),
    (4, "31.6 pA", 31.6e-12),
    (5, "100 pA", 100e-12),
    (6, "316 pA", 316e-12),
    (7, "1.00 nA", 1.00e-9),
    (8, "3.16 nA", 3.16e-9),
    (9, "10.0 nA", 10.0e-9),
    (10, "31.6 nA", 31.6e-9),
    (11, "100 nA", 100e-9),
    (12, "316 nA", 316e-9),
    (13, "1.00 uA", 1.00e-6),
    (14, "3.16 uA", 3.16e-6),
    (15, "10.0 uA", 10.0e-6),
    (16, "31.6 uA", 31.6e-6),
    (17, "100 uA", 100e-6),
    (18, "316 uA", 316e-6),
    (19, "1.00 mA", 1.00e-3),
    (20, "3.16 mA", 3.16e-3),
    (21, "10.0 mA", 10.0e-3),
    (22, "31.6 mA", 31.6e-3),
)

# INTYPE 电压激励量程：协议索引、UI 标签、伏特值。
VOLTAGE_EXCITATIONS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "2.00 uV", 2.00e-6),
    (2, "6.32 uV", 6.32e-6),
    (3, "20.0 uV", 20.0e-6),
    (4, "63.2 uV", 63.2e-6),
    (5, "200 uV", 200e-6),
    (6, "632 uV", 632e-6),
    (7, "2.00 mV", 2.00e-3),
    (8, "6.32 mV", 6.32e-3),
    (9, "20.0 mV", 20.0e-3),
    (10, "63.2 mV", 63.2e-3),
    (11, "200 mV", 200e-3),
    (12, "632 mV", 632e-3),
)

# INTYPE 电阻量程：协议索引、UI 标签、欧姆值。
RESISTANCE_RANGES: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "2.00 mOhm", 2.00e-3),
    (2, "6.32 mOhm", 6.32e-3),
    (3, "20.0 mOhm", 20.0e-3),
    (4, "63.2 mOhm", 63.2e-3),
    (5, "200 mOhm", 200e-3),
    (6, "632 mOhm", 632e-3),
    (7, "2.00 Ohm", 2.00),
    (8, "6.32 Ohm", 6.32),
    (9, "20.0 Ohm", 20.0),
    (10, "63.2 Ohm", 63.2),
    (11, "200 Ohm", 200.0),
    (12, "632 Ohm", 632.0),
    (13, "2.00 kOhm", 2.00e3),
    (14, "6.32 kOhm", 6.32e3),
    (15, "20.0 kOhm", 20.0e3),
    (16, "63.2 kOhm", 63.2e3),
    (17, "200 kOhm", 200e3),
    (18, "632 kOhm", 632e3),
    (19, "2.00 MOhm", 2.00e6),
    (20, "6.32 MOhm", 6.32e6),
    (21, "20.0 MOhm", 20.0e6),
    (22, "63.2 MOhm", 63.2e6),
)

# RDGST? 返回的 8 位状态字。bit 0 是电流源 compliance，后端会把它单独升级显示；
# 其他位仍完整保存在 StatusDetails，不能只保留归一化后的 NORMAL/OVER_RANGE。
STATUS_BITS: tuple[tuple[int, str], ...] = (
    (1, "CS_OVL"),
    (2, "VCM_OVL"),
    (4, "VMIX_OVL"),
    (8, "VDIF_OVL"),
    (16, "R_OVER"),
    (32, "R_UNDER"),
    (64, "T_OVER"),
    (128, "T_UNDER"),
)


def compatible_resistance_range_indices(
    excitation_mode: str,
    excitation_index: int,
) -> tuple[int, ...]:
    """返回 Figure 1-16 允许的测量输入电阻量程索引。

    手册性能矩阵的行是 22 档电流激励，列是 12 档电压激励，每个非星号单元格给出
    一个可用电阻量程。三组量程都按 ``1, 3.16, 10`` 的半十倍频程排列，因此矩阵中
    的索引满足 ``voltage = current + resistance - 19``。由此可直接得到连续合法
    区间，避免在前端和后端各维护一份容易错位的 22×12 布尔表。

    返回空元组表示模式或激励索引本身无效；调用后端前仍应先执行各字段的独立范围
    校验。带 ``**``、手册标为“可用但未给性能指标”的高阻量程仍属于可选择范围。
    """

    mode = str(excitation_mode).strip().casefold()
    index = int(excitation_index)
    if mode == "current":
        if not 1 <= index <= 22:
            return ()
        minimum = max(1, 20 - index)
        maximum = min(22, 31 - index)
    elif mode == "voltage":
        if not 1 <= index <= 12:
            return ()
        minimum = max(1, index - 3)
        maximum = min(22, index + 18)
    else:
        return ()
    return tuple(range(minimum, maximum + 1))


def default_channel(slot: int) -> dict[str, Any]:
    """返回一个逻辑 R 槽位的全新默认字典。

    默认仅 R1 Enabled，物理输入号等于槽位号；量程索引 5/17 分别对应手册中的
    100 pA 与 200 kOhm。每次新建字典，避免多个窗口共享可变通道设置。
    """

    return {
        "enabled": slot == 1,
        "input_channel": slot,
        "excitation_mode": "current",
        "excitation_range": 5,
        "autorange": 1,
        "resistance_range": 17,
    }


def default_settings() -> dict[str, Any]:
    """返回模块初始设置；资源为空，且默认要求每通道读完后恢复分流。

    保存设置由界面读取但不会在 Enable 时自动 Apply。这些默认值只是初始选择，真正
    发给仪表前仍需后端做类型、范围、唯一物理输入和总超时校验。
    """

    return {
        "resource": "",
        "frequency_index": 2,
        "pause_seconds": 3,
        "dwell_seconds": 10,
        "filter_enabled": False,
        "filter_settle_seconds": 10,
        "filter_window_percent": 40,
        "io_timeout_seconds": 3.0,
        "retry_attempts": 2,
        "shunt_after_read": True,
        "channels": {
            f"r{slot}": default_channel(slot)
            for slot in range(1, 5)
        },
    }


__all__ = [
    "CURRENT_EXCITATIONS",
    "FREQUENCIES_HZ",
    "RESISTANCE_RANGES",
    "STATUS_BITS",
    "VOLTAGE_EXCITATIONS",
    "compatible_resistance_range_indices",
    "default_channel",
    "default_settings",
]
