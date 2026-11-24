"""LR-700/LR-720-16 的底层 VISA、命令构造与响应解析。

这里集中保存手册协议细节与 LR-700 所需的 GPIB 终止符/查询延迟配置，不修改主框架
通用 ``VisaSession`` 的默认值。模块生命周期、通道轮转、查询重试、数据异常降级和
最低激励恢复仍由 ``backend.py`` 决定。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Protocol

from labcontrol.measurement.visa import VisaSession

from .constants import (
    SAFE_EXCITATION_INDEX,
    SAFE_EXCITATION_PERCENT,
    autorange_trace_excitation_index,
    minimum_excitation_index,
)


# 实机 GPIB（NI GPIB-USB-HS）验证可稳定连续写入/回读的 LR-700 适配参数：
# 读取依赖 EOI（read_termination=None），写入以 "\n" 结尾，查询预留 0.2 s 仪表处理时间。
LR700_READ_TERMINATION: str | None = None
LR700_WRITE_TERMINATION = "\n"
LR700_QUERY_DELAY_SECONDS = 0.2

# 下降换挡中转量程：20 kOhm（编码 7）既支持 EXCITATION 0（20 uV），
# 从 200 kOhm / 2 MOhm（2E / 4E）切到 20 kOhm 时在 5% 下激励电流不超过 5 nA。
_SAFE_STEP_DOWN_RANGE_INDEX = 7


class LR700VisaTransport(VisaSession):
    """带 LR-700 专用 EOI 读取与查询延迟的 VISA 会话包装。"""

    def __init__(
        self,
        address: str,
        timeout_seconds: float,
        *,
        read_termination: str | None = LR700_READ_TERMINATION,
        write_termination: str = LR700_WRITE_TERMINATION,
        query_delay: float = LR700_QUERY_DELAY_SECONDS,
        **attributes: Any,
    ) -> None:
        super().__init__(
            address,
            timeout_seconds,
            read_termination=read_termination,
            write_termination=write_termination,
            query_delay=query_delay,
            **attributes,
        )


PyVisaTransport = LR700VisaTransport


class Transport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


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


def _range_transition_commands(
    target_range: int,
    current_range_index: int | None,
) -> tuple[str, ...]:
    """从高阻量程（8/9，最低激励 2E/4E）向低阻量程切换时先经 20 kOhm 降至 0E。"""

    if (
        current_range_index is not None
        and int(current_range_index) >= 8
        and int(target_range) < _SAFE_STEP_DOWN_RANGE_INDEX
    ):
        return (
            f"RANGE {_SAFE_STEP_DOWN_RANGE_INDEX}",
            f"EXCITATION {SAFE_EXCITATION_INDEX}",
            f"RANGE {int(target_range)}",
        )
    return (f"RANGE {int(target_range)}",)


def autorange_start_commands(
    input_channel: object,
    channel: Mapping[str, object],
    *,
    current_range_index: int | None = None,
    target_range_index: int | None = None,
    measurement_mode: int = 0,
) -> tuple[str, ...]:
    """在 5% 安全态下切传感器、切到目标量程并同步自定义 trace 激励后开启 AUTORANGE 1。

    - 若传入该传感器上次稳定的 ``target_range_index`` 且落在 ``[min_range, max_range]`` 内，
      则先在 5% 下安全切至该缓存量程；
    - 否则保持当前量程（若当前量程落在 ``[min_range, max_range]`` 之外则钳位进边界）；
    - 无论是否改变电阻量程，都会把 ``EXCITATION`` 更新为自定义 trace 表中对应目标量程的
      激发电压（即对应目标激发电流），再下发 ``AUTORANGE L=``、``VAREXC 0`` 与
      ``AUTORANGE 1``。Trace 已在 Apply 阶段写入，此处复用已保存的映射。
    """

    min_range = int(channel["autorange_min_range"])
    max_range = int(channel["autorange_max_range"])
    filter_index = int(channel["filter_index"])
    target_range: int | None = None
    if (
        target_range_index is not None
        and min_range <= int(target_range_index) <= max_range
    ):
        target_range = int(target_range_index)
    elif current_range_index is not None:
        target_range = max(min_range, min(max_range, int(current_range_index)))

    select_cmd = f"SELECT S={int(input_channel):02d}"
    if target_range is None:
        switch_sequence: tuple[str, ...] = (select_cmd,)
    else:
        target_exc = channel.get("autorange_trace", tuple(autorange_trace_excitation_index(r) for r in range(10)))[target_range]
        move_commands: tuple[str, ...] = (
            ()
            if current_range_index is not None
            and int(current_range_index) == target_range
            else _range_transition_commands(target_range, current_range_index)
        )
        # 切向更高阻（更小电流）量程时先升量程再切传感器，保证新旧通道均不承受更高电流；
        # 切向更低阻量程或保持同量程时先在 5% 下切传感器，再安全降量程并更新对应激励。
        if (
            current_range_index is not None
            and target_range > int(current_range_index)
        ):
            switch_sequence = (
                *move_commands,
                select_cmd,
                f"EXCITATION {target_exc}",
            )
        else:
            switch_sequence = (
                select_cmd,
                *move_commands,
                f"EXCITATION {target_exc}",
            )

    return (
        "AUTORANGE 0",
        f"MODE {measurement_mode}",
        *switch_sequence,
        f"FILTER {filter_index}",
        f"AUTORANGE L={min_range}{max_range}",
        "VAREXC 0",
        "AUTORANGE 1",
    )


def autorange_switch_commands(
    input_channel: object,
    channel: Mapping[str, object],
    *,
    active_limits: tuple[int, int] | None = None,
    active_filter: int | None = None,
) -> tuple[str, ...]:
    """同量程且在边界内的连续自动量程切通道：若 L= 变化先更新 L=，再发 SELECT。"""

    min_range = int(channel["autorange_min_range"])
    max_range = int(channel["autorange_max_range"])
    filter_index = int(channel["filter_index"])
    commands: list[str] = []
    if active_limits != (min_range, max_range):
        commands.append(f"AUTORANGE L={min_range}{max_range}")
    commands.append(f"SELECT S={int(input_channel):02d}")
    if active_filter != filter_index:
        commands.append(f"FILTER {filter_index}")
    return tuple(commands)


def configuration_commands(
    input_channel: object,
    channel: Mapping[str, object],
    *,
    current_range_index: int | None = None,
    measurement_mode: int = 0,
) -> tuple[str, ...]:
    """生成当前传感器的完整绝对配置，并保证切换中间态不提前抬高激励。

    调用前桥应处于最低激励 x 5%。固定量程模式先切传感器、量程、激励档和滤波，
    最后才抬升激励百分比；自动量程模式复用 ``autorange_start_commands``，
    按现场已确认的跟踪表同步目标量程激励，仅下发 ``AUTORANGE L=``，不改写 Trace。
    """

    mode = str(channel.get("range_mode", "fixed")).strip().casefold()
    if mode == "autorange":
        return autorange_start_commands(
            input_channel,
            channel,
            current_range_index=current_range_index,
            measurement_mode=measurement_mode,
        )

    filter_index = int(channel["filter_index"])
    target_range = int(channel["range_index"])
    target_excitation = int(channel["excitation_index"])
    return (
        "AUTORANGE 0",
        f"MODE {measurement_mode}",
        f"SELECT S={int(input_channel):02d}",
        *_range_transition_commands(target_range, current_range_index),
        f"EXCITATION {target_excitation}",
        f"FILTER {filter_index}",
        *variable_excitation_commands(channel["excitation_percent"]),
    )


def safe_state_commands(range_index: int | None = None) -> tuple[str, ...]:
    """LR-700 没有 Off；返回当前量程可读回确认的最低激励绝对命令。

    先退出自动量程并将激励百分比降至 5%，再把激励档降到当前量程允许的最低档
    （0-7 -> 0；8 -> 2；9 -> 4），避免高阻档下发送 EXCITATION 0 被仪表静默拒绝。
    """

    excitation_index = (
        minimum_excitation_index(range_index)
        if range_index is not None
        else SAFE_EXCITATION_INDEX
    )
    return (
        "AUTORANGE 0",
        f"VAREXC ={SAFE_EXCITATION_PERCENT:02d}",
        "VAREXC 1",
        f"EXCITATION {excitation_index}",
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
    "LR700_QUERY_DELAY_SECONDS",
    "LR700_READ_TERMINATION",
    "LR700_WRITE_TERMINATION",
    "LR700VisaTransport",
    "Transport",
    "PyVisaTransport",
    "GET_RESISTANCE",
    "GET_REACTANCE",
    "GET_SETTINGS",
    "GET_OVERLOADS",
    "autorange_start_commands",
    "autorange_switch_commands",
    "configuration_commands",
    "variable_excitation_commands",
    "safe_state_commands",
    "parse_bridge_settings",
    "parse_measurement",
    "parse_overloads",
]
