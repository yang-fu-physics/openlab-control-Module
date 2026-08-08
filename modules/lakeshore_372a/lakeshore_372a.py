"""Lake Shore 372 的底层 VISA、命令构造与响应解析。

本文件只描述仪表协议，不负责 SEQ 生命周期、通道调度、超时预算、重试策略或
Warning/Error 分级。``backend.py`` 负责决定何时调用这些命令，并负责把协议异常
转换为框架可识别的错误。
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Mapping
from typing import Protocol


class Transport(Protocol):
    """后端与测试替身共同使用的最小同步通信接口。"""

    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """仅在模块 worker 内创建并持有的 PyVISA GPIB 会话。"""

    def __init__(self, resource_name: str, timeout_seconds: float) -> None:
        pyvisa = importlib.import_module("pyvisa")
        self._manager = pyvisa.ResourceManager()
        try:
            self._instrument = self._manager.open_resource(resource_name)
            self._instrument.timeout = max(1, int(timeout_seconds * 1000))
            self._instrument.read_termination = "\n"
            self._instrument.write_termination = "\n"
        except Exception:
            self._manager.close()
            raise

    @staticmethod
    def list_resources() -> tuple[str, ...]:
        """枚举并排序 GPIB 资源，避免把其他总线地址混入设置下拉框。"""

        pyvisa = importlib.import_module("pyvisa")
        manager = pyvisa.ResourceManager()
        try:
            resources = tuple(str(item) for item in manager.list_resources())
        finally:
            manager.close()
        return tuple(
            sorted(
                {item for item in resources if item.upper().startswith("GPIB")},
                key=str.casefold,
            )
        )

    def write(self, command: str) -> None:
        self._instrument.write(command)

    def query(self, command: str) -> str:
        return str(self._instrument.query(command))

    def close(self) -> None:
        try:
            self._instrument.close()
        finally:
            self._manager.close()


IDENTIFY = "*IDN?"
SCAN_QUERY = "SCAN?"


def validate_identity(identity: str) -> bool:
    """允许常见分隔符差异，但必须明确识别为 Model 372。"""

    compact = re.sub(r"[\s_-]+", "", identity).upper()
    return "MODEL372" in compact


def frequency_command(index: object) -> str:
    return f"FREQ 0,{int(index)}"


def frequency_query() -> str:
    return "FREQ? 0"


def filter_command(
    input_channel: object,
    *,
    enabled: bool,
    settle_seconds: object,
    window_percent: object,
) -> str:
    return (
        f"FILTER {int(input_channel)},{1 if enabled else 0},"
        f"{int(settle_seconds)},{int(window_percent)}"
    )


def filter_query(input_channel: object) -> str:
    return f"FILTER? {int(input_channel)}"


def inset_command(
    input_channel: object,
    *,
    enabled: bool,
    dwell_seconds: object,
    pause_seconds: object,
) -> str:
    # 最后两个字段固定为 curve=0、temperature coefficient=2。
    return (
        f"INSET {int(input_channel)},{1 if enabled else 0},"
        f"{int(dwell_seconds)},{int(pause_seconds)},0,2"
    )


def inset_query(input_channel: object) -> str:
    return f"INSET? {int(input_channel)}"


def intype_values(
    channel: Mapping[str, object],
    *,
    shunted: bool,
) -> tuple[int, ...]:
    """返回 ``INTYPE?`` 应读回的六个字段（不含输入号）。"""

    mode = 1 if channel["excitation_mode"] == "current" else 0
    return (
        mode,
        int(channel["excitation_range"]),
        int(channel["autorange"]),
        int(channel["resistance_range"]),
        1 if shunted else 0,
        2,
    )


def intype_command(
    channel: Mapping[str, object],
    *,
    shunted: bool,
) -> str:
    """构造完整绝对 ``INTYPE``，不依赖仪表中残留的字段值。"""

    values = ",".join(str(value) for value in intype_values(channel, shunted=shunted))
    return f"INTYPE {int(channel['input_channel'])},{values}"


def intype_query(input_channel: object) -> str:
    return f"INTYPE? {int(input_channel)}"


def scan_command(input_channel: object) -> str:
    # 第二个字段为 0，明确关闭仪表内部自动扫描。
    return f"SCAN {int(input_channel)},0"


def resistance_query(input_channel: object) -> str:
    return f"RDGR? {int(input_channel)}"


def quadrature_query(input_channel: object) -> str:
    return f"QRDG? {int(input_channel)}"


def power_query(input_channel: object) -> str:
    return f"RDGPWR? {int(input_channel)}"


def status_query(input_channel: object) -> str:
    return f"RDGST? {int(input_channel)}"


def parse_integer_tuple(reply: str) -> tuple[int, ...]:
    return tuple(int(part.strip(), 10) for part in reply.split(","))


def parse_number(reply: str) -> float:
    value = float(reply)
    if not math.isfinite(value):
        raise ValueError(f"non-finite numeric response {reply!r}")
    return value


def parse_status(reply: str) -> int:
    value = int(reply, 10)
    if not 0 <= value <= 255:
        raise ValueError(f"status outside 0-255: {value}")
    return value


__all__ = [
    "Transport",
    "PyVisaTransport",
    "IDENTIFY",
    "SCAN_QUERY",
    "validate_identity",
    "frequency_command",
    "frequency_query",
    "filter_command",
    "filter_query",
    "inset_command",
    "inset_query",
    "intype_values",
    "intype_command",
    "intype_query",
    "scan_command",
    "resistance_query",
    "quadrature_query",
    "power_query",
    "status_query",
    "parse_integer_tuple",
    "parse_number",
    "parse_status",
]
