"""官方模块测试使用的最小 SDK 调用辅助函数。

生产 worker 直接消费 ``measure`` 返回值；这里额外把返回行记录为测试事件，便于既有
仪表安全与协议断言复用同一消息列表。辅助函数不会给模块补回任何旧生命周期方法。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from labcontrol.module_api import ModuleAPI


def measurement_resources(
    resources: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """为前后端测试构造“稳定 ID → 实际地址”的核心资源快照。"""

    return {
        resource_id: {
            "id": resource_id,
            "address": address,
            "identity": f"Test {resource_id}",
            "purpose": "measurement",
            "system_instrument": "",
            "primary_reading": "",
            "monitor_readings": [],
        }
        for resource_id, address in resources.items()
    }


class TestModuleAPI(ModuleAPI):
    """官方模块单元测试使用的可直接构造 ModuleAPI。"""

    __test__ = False

    def __init__(
        self,
        instruments: Mapping[str, Mapping[str, Any]],
        emit,
        sample_instruments=None,
        operation_state=None,
        timeout: float = 120.0,
        resources: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        # 后端测试中的既有假地址同时作为稳定测试资源 ID。生产环境的 ID 由扫描工具
        # 验证为简短名称；这里保留协议断言使用的地址文本，避免测试夹具掩盖实际打开值。
        test_addresses = (
            "GPIB0::7::INSTR",
            "GPIB0::5::INSTR",
            "GPIB0::8::INSTR",
            "GPIB0::12::INSTR",
            "GPIB0::18::INSTR",
            "GPIB0::24::INSTR",
            "GPIB0::26::INSTR",
            "GPIB0::27::INSTR",
            "GPIB0::22::INSTR",
            "GPIB0::99::INSTR",
            "GPIB9::12::INSTR",
            "GPIB9::18::INSTR",
            "GPIB9::24::INSTR",
            "GPIB9::26::INSTR",
            "GPIB9::27::INSTR",
        )
        resource_table = (
            resources
            if resources is not None
            else {
                address: {
                    "id": address,
                    "address": address,
                    "identity": "Test instrument",
                    "purpose": "measurement",
                    "system_instrument": "",
                    "primary_reading": "",
                    "monitor_readings": [],
                }
                for address in test_addresses
            }
        )
        super().__init__(
            instruments,
            emit,
            sample_instruments,
            operation_state,
            timeout,
            _instrument_resources=resource_table,
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
