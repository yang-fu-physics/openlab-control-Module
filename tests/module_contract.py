"""官方模块测试使用的最小 SDK 调用辅助函数。

生产 worker 直接消费 ``measure`` 返回值；这里额外把返回行记录为测试事件，便于既有
仪表安全与协议断言复用同一消息列表。辅助函数不会给模块补回任何旧生命周期方法。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from labcontrol.module_api import ModuleAPI


class TestModuleAPI(ModuleAPI):
    """官方模块单元测试使用的可直接构造 ModuleAPI。"""

    __test__ = False

    def __init__(
        self,
        devices: Mapping[str, Mapping[str, Any]],
        emit,
        sample_devices=None,
        operation_state=None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(
            devices,
            emit,
            sample_devices,
            operation_state,
            timeout,
        )


def open_module(module, api: ModuleAPI):
    """调用真实 ``open(api)``。"""

    return module.open(api)


def run_start(module, api: ModuleAPI):
    return module.on_event("run_start", {}, api)


def run_end(module, reason: str, api: ModuleAPI):
    return module.on_event("run_end", {"reason": reason}, api)


def read_status(module, api: ModuleAPI):
    return module.on_event("status", {}, api)


def run_action(
    module,
    name: str,
    payload: Mapping[str, Any],
    api: ModuleAPI,
):
    return module.on_event(
        "action",
        {"name": name, "payload": dict(payload)},
        api,
    )


def module_slots(module) -> tuple[int, ...]:
    value = module.slots
    if isinstance(value, int):
        return tuple(range(1, value + 1))
    return tuple(value)


def measure_module(module, api: TestModuleAPI, slot: int = 1):
    """调用真实 ``measure(slot, api)``，并为测试断言记录一条 row 事件。"""

    result = module.measure(slot, api)
    raw_values = None
    row = result
    if isinstance(result, tuple):
        row, raw_values = result
    payload: dict[str, Any] = {"values": dict(row)}
    if raw_values is not None:
        payload["raw_values"] = list(raw_values)
    api._emit("row", payload)
    return result
