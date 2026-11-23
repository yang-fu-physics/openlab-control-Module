from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
if not CORE.is_dir():
    CORE = ROOT.parent / "openlab-control"
MODULE = ROOT / "modules" / "lr700"
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.package_support.loading import load_source_object  # noqa: E402
from labcontrol.module_api import (  # noqa: E402
    ModuleError,
)
from labcontrol.measurement.frontend_api import (  # noqa: E402
    ModuleUIAPI,
)
from module_contract import (  # noqa: E402
    TestModuleAPI,
    measurement_resources,
    measure_module,
    module_slots,
    open_module,
    read_status,
    run_action,
    run_end,
    run_start,
)
from labcontrol.measurement.manifest import (  # noqa: E402
    load_manifest,
)
from labcontrol.measurement.settings import (  # noqa: E402
    load_settings,
    save_settings,
)

LR700Backend = load_source_object(
    MODULE,
    "backend:LR700Backend",
    "test_lr700_backend",
)
LR700VisaTransport = load_source_object(
    MODULE,
    "lr700:LR700VisaTransport",
    "test_lr700_protocol",
)
FILTERS = load_source_object(
    MODULE,
    "constants:FILTERS",
    "test_lr700_constants_filters",
)
COMMON_MODE_OVERLOAD_HINT = load_source_object(
    MODULE,
    "constants:COMMON_MODE_OVERLOAD_HINT",
    "test_lr700_constants_hint",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_lr700_constants",
)
valid_excitation_indices = load_source_object(
    MODULE,
    "constants:valid_excitation_indices",
    "test_lr700_constants_valid",
)
excitation_current_a = load_source_object(
    MODULE,
    "constants:excitation_current_a",
    "test_lr700_constants_current",
)
LR700Frontend = load_source_object(
    MODULE,
    "frontend:LR700Frontend",
    "test_lr700_frontend",
)


class _FakeVisaState:
    """按 LR-700 v1.3 手册及实机规则模拟仪表行为（含高阻最低激励与自动量程）。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.current_trace: list[tuple[str, float]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.failures: dict[str, int] = {}
        self.query_overrides: dict[str, str] = {}
        self.range_index = 5
        self.excitation_index = 0
        self.excitation_percent = 5
        self.pending_excitation_percent = 5
        self.filter_index = 1
        self.mode = 0
        self.local_lockout = 0
        self.sensor = 0
        self.autorange = 0
        self.autorange_limits: tuple[int, int] = (0, 9)
        self.autorange_trace = 1
        self.autorange_target_by_sensor: dict[int, tuple[int, int]] = {}
        self.autorange_mid_read_shift: tuple[int, int] | None = None
        self.autorange_get6_shifts: list[tuple[int, int] | None] = []
        self.overloads: dict[int, int] = {}
        self.resistance_reply_by_sensor: dict[int, str] = {}
        self.reactance_reply_by_sensor: dict[int, str] = {}

    def factory(
        self,
        resource: str,
        timeout: float,
    ):
        self.opened.append((resource, timeout))
        return _FakeTransport(self)


class _FakeTransport:
    def __init__(self, state: _FakeVisaState) -> None:
        self.state = state

    def write(self, command: str) -> None:
        self.state.commands.append(("write", command))
        self._fail(command)
        upper = command.upper()
        if upper.startswith("AUTORANGE L="):
            limits = upper.split("=", 1)[1].strip()
            self.state.autorange_limits = (int(limits[0]), int(limits[1]))
        elif upper.startswith("AUTORANGE T="):
            self.state.autorange_trace = int(upper.split("=", 1)[1].strip())
        elif upper.startswith("AUTORANGE "):
            self.state.autorange = int(command.split(" ", 1)[1])
            if (
                self.state.autorange == 1
                and self.state.sensor in self.state.autorange_target_by_sensor
            ):
                target_r, target_e = self.state.autorange_target_by_sensor[
                    self.state.sensor
                ]
                self.state.range_index = target_r
                self.state.excitation_index = target_e
        elif upper.startswith("MODE "):
            self.state.mode = int(command.split(" ", 1)[1])
        elif upper.startswith("SELECT S="):
            self.state.sensor = int(command.rsplit("=", 1)[1])
        elif upper.startswith("RANGE "):
            target = int(command.split(" ", 1)[1])
            self.state.range_index = target
            allowed = valid_excitation_indices(target)
            if self.state.excitation_index < allowed[0]:
                self.state.excitation_index = allowed[0]
            elif self.state.excitation_index > allowed[-1]:
                self.state.excitation_index = allowed[-1]
        elif upper.startswith("EXCITATION "):
            requested = int(command.split(" ", 1)[1])
            # 模拟真实 LR-700：不在当前量程合法表内的激励命令被静默忽略，回读保持原值。
            if requested in valid_excitation_indices(self.state.range_index):
                self.state.excitation_index = requested
        elif upper.startswith("FILTER "):
            self.state.filter_index = int(command.split(" ", 1)[1])
        elif upper.startswith("VAREXC ="):
            self.state.pending_excitation_percent = int(
                command.rsplit("=", 1)[1]
            )
        elif upper == "VAREXC 0":
            self.state.excitation_percent = 100
        elif upper == "VAREXC 1":
            self.state.excitation_percent = (
                self.state.pending_excitation_percent
            )
        else:
            raise AssertionError(f"Unexpected write: {command}")

        self.state.current_trace.append(
            (
                command,
                excitation_current_a(
                    self.state.range_index,
                    self.state.excitation_index,
                    self.state.excitation_percent,
                ),
            )
        )

    def query(self, command: str) -> str:
        self.state.commands.append(("query", command))
        self._fail(command)
        if command in self.state.query_overrides:
            return self.state.query_overrides[command]
        if command == "GET 6":
            if self.state.autorange == 1 and self.state.autorange_get6_shifts:
                shift = self.state.autorange_get6_shifts.pop(0)
                if shift is not None:
                    self.state.range_index, self.state.excitation_index = shift
            return (
                f"{self.state.range_index}R,"
                f"{self.state.excitation_index}E,"
                f"{self.state.excitation_percent:02d}%,"
                f"{self.state.filter_index}F,"
                f"{self.state.mode}M,"
                f"{self.state.local_lockout}L,"
                f"{self.state.sensor:02d}S"
            )
        if command == "GET 0":
            if (
                self.state.autorange == 1
                and self.state.autorange_mid_read_shift is not None
            ):
                new_r, new_e = self.state.autorange_mid_read_shift
                self.state.autorange_mid_read_shift = None
                self.state.range_index = new_r
                self.state.excitation_index = new_e
            if self.state.sensor in self.state.resistance_reply_by_sensor:
                return self.state.resistance_reply_by_sensor[self.state.sensor]
            return f"+{self.state.sensor * 10:.5f}  OHMR"
        if command == "GET 1":
            if self.state.sensor in self.state.reactance_reply_by_sensor:
                return self.state.reactance_reply_by_sensor[self.state.sensor]
            return f"+{self.state.sensor:.5f}  OHMX"
        if command == "GET 7":
            return (
                f"{self.state.overloads.get(self.state.sensor, 0):03d} "
                "OVERLOADS"
            )
        raise AssertionError(f"Unexpected query: {command}")

    def _fail(self, command: str) -> None:
        remaining = self.state.failures.get(command, 0)
        if remaining > 0:
            self.state.failures[command] = remaining - 1
            raise OSError(f"simulated failure: {command}")

    def close(self) -> None:
        self.state.closed += 1
        self.state.commands.append(("close", ""))


def _two_slot_settings() -> dict:
    settings = default_settings()
    settings["resource"] = "GPIB0::18::INSTR"
    for channel in settings["channels"].values():
        channel["enabled"] = False
    settings["channels"]["r1"].update({
        "input_channel": 5,
        "enabled": True,
        "range_mode": "fixed",
        "range_index": 5,
        "excitation_index": 3,
        "excitation_percent": 50,
        "filter_index": 1,
    })
    settings["channels"]["r2"].update({
        "input_channel": 12,
        "enabled": True,
        "range_mode": "fixed",
        "range_index": 9,
        "excitation_index": 4,
        "excitation_percent": 100,
        "filter_index": 0,
    })
    return settings


def _system_sample(
    timestamp: float,
    temperature: float,
    field: float,
) -> dict:
    return {
        "temperature": {
            "kind": "temperature",
            "role": "primary",
            "control_enabled": True,
            "connected": True,
            "current": temperature,
            "unit": "K",
            "timestamp": timestamp,
        },
        "field": {
            "kind": "field",
            "role": "primary",
            "control_enabled": True,
            "connected": True,
            "current": field,
            "unit": "Oe",
            "timestamp": timestamp,
        },
    }


def _samples(sensor_count: int) -> list[dict]:
    result: list[dict] = []
    for sensor in range(1, sensor_count + 1):
        result.extend([
            _system_sample(
                float(sensor * 2 - 1),
                float(sensor * 10),
                float(sensor * 100),
            ),
            _system_sample(
                float(sensor * 2),
                float(sensor * 10 + 2),
                float(sensor * 100 + 20),
            ),
        ])
    return result


class LR700BackendTests(unittest.TestCase):
    def _backend(
        self,
        state: _FakeVisaState,
        waits: list[float] | None = None,
    ) -> LR700Backend:
        return LR700Backend(
            transport_factory=state.factory,
            waiter=(
                lambda context, seconds: (
                    waits.append(seconds)
                    if waits is not None
                    else context.sleep(0)
                )
            ),
        )

    @staticmethod
    def _context(
        messages: list[tuple[str, dict]],
        samples: list[dict] | None = None,
        resources: dict[str, dict] | None = None,
    ) -> TestModuleAPI:
        iterator = iter(samples or [])
        return TestModuleAPI(
            {},
            lambda kind, values: messages.append(
                (kind, values)
            ),
            (
                (lambda _timeout: next(iterator))
                if samples is not None
                else None
            ),
            lambda _timeout: "running",
            120.0,
            resources=resources,
        )

    @staticmethod
    def _measure_enabled_slots(
        backend,
        context: TestModuleAPI,
    ) -> None:
        for logical_slot in module_slots(backend):
            measure_module(backend, context, logical_slot)

    def test_open_reads_registry_without_connecting_or_writing(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = open_module(backend, self._context(messages))

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertEqual(
            status["Applied Settings"],
            "Not applied",
        )
        self.assertNotIn("Available GPIB Resources", status)

    def test_four_slot_mapping_emits_two_sparse_rows_and_restores_range_minimum_excitation(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.overloads[12] = 4
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        settings = _two_slot_settings()
        backend = self._backend(state, waits)
        context = self._context(
            messages,
            _samples(2),
        )

        open_module(backend, context)
        applied = backend.configure(
            settings,
            context,
        )
        run_start(backend, context)
        self._measure_enabled_slots(backend, context)
        ended = run_end(backend, "completed", context)

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            set(rows[0]),
            {
                "TemperatureAverage",
                "FieldAverage",
                "R1",
                "X1",
                "StatusCode",
            },
        )
        self.assertEqual(
            set(rows[1]),
            {
                "TemperatureAverage",
                "FieldAverage",
                "StatusCode",
            },
        )
        self.assertAlmostEqual(rows[0]["R1"], 50.0)
        self.assertAlmostEqual(rows[0]["X1"], 5.0)
        self.assertAlmostEqual(
            rows[0]["TemperatureAverage"],
            11.0,
        )
        self.assertAlmostEqual(
            rows[0]["FieldAverage"],
            110.0,
        )
        self.assertEqual(rows[0]["StatusCode"], 0)
        self.assertEqual(rows[1]["StatusCode"], 2)
        self.assertNotIn("R2", rows[1])
        self.assertNotIn("X2", rows[1])
        self.assertEqual(waits, [9.0, 1.0] * 2)
        self.assertIn(("write", "SELECT S=05"), state.commands)
        self.assertIn(("write", "SELECT S=12"), state.commands)
        self.assertIn(("write", "VAREXC =50"), state.commands)
        self.assertIn(("write", "VAREXC 0"), state.commands)
        # r2 在 2 MOhm (9R) 上测量，退出时按当前量程恢复到 4E (2 mV) x 5%，而非 0E。
        self.assertEqual(state.range_index, 9)
        self.assertEqual(state.excitation_index, 4)
        self.assertEqual(state.excitation_percent, 5)
        self.assertIn("200 Ohm", applied["Excitation Safety"])
        self.assertIn("20 uV x 5%", applied["Excitation Safety"])
        self.assertIn("2 MOhm", ended["Excitation Safety"])
        self.assertIn("2 mV x 5%", ended["Excitation Safety"])
        warnings = [
            payload
            for kind, payload in messages
            if kind == "warning"
        ]
        self.assertTrue(
            any(
                payload["code"] == "LR700_READING_WARNING"
                and payload["context"] == "R2 / sensor 12"
                and "Common-mode overload +" in payload["message"]
                and COMMON_MODE_OVERLOAD_HINT in payload["message"]
                for payload in warnings
            )
        )

    def test_safe_state_adapts_to_200k_and_2m_and_steps_down_safely(
        self,
    ) -> None:
        for high_range, expected_min_exc, expected_label in (
            (8, 2, "200 uV x 5%"),
            (9, 4, "2 mV x 5%"),
        ):
            with self.subTest(high_range=high_range):
                state = _FakeVisaState()
                state.range_index = high_range
                state.excitation_index = 6
                state.excitation_percent = 100
                messages: list[tuple[str, dict]] = []
                settings = default_settings()
                settings["resource"] = "GPIB0::18::INSTR"
                settings["channels"]["r1"].update({
                    "enabled": True,
                    "range_mode": "fixed",
                    "range_index": 2,  # 200 mOhm
                    "excitation_index": 3,  # 600 uV -> 3 mA
                    "excitation_percent": 100,
                    "filter_index": 1,
                })
                backend = self._backend(state, [])
                context = self._context(messages, _samples(1))
                open_module(backend, context)

                applied = backend.configure(settings, context)
                self.assertEqual(state.range_index, high_range)
                self.assertEqual(state.excitation_index, expected_min_exc)
                self.assertEqual(state.excitation_percent, 5)
                self.assertIn(expected_label, applied["Excitation Safety"])

                run_start(backend, context)
                state.current_trace.clear()
                measure_module(backend, context, 1)
                # 从 200 kOhm / 2 MOhm（2E / 4E）下降到 200 mOhm 须经 RANGE 7 + EXCITATION 0，
                # 保证整个换挡过程中间电流不超过目标 3 mA 且切换传感器前已处于最低激励。
                self.assertIn(("write", "RANGE 7"), state.commands)
                self.assertIn(("write", "EXCITATION 0"), state.commands)
                peak_current = max(cur for _cmd, cur in state.current_trace)
                self.assertLessEqual(peak_current, 3.0e-3 + 1e-12)
                self.assertEqual(state.range_index, 2)
                self.assertEqual(state.excitation_index, 0)
                self.assertEqual(state.excitation_percent, 5)
                merged_status: dict[str, object] = {}
                for kind, payload in messages:
                    if kind == "status":
                        merged_status.update(payload["values"])
                self.assertEqual(merged_status["Current Range Index"], "2 (200 mOhm)")
                self.assertEqual(merged_status["Current Excitation Index"], "0 (20 uV)")
                self.assertEqual(merged_status["Current Excitation (%)"], 5)
                self.assertEqual(merged_status["Current Excitation Current"], "5 uA")

                # 再切回高阻态并测试 Stop / Close 恢复路径。
                state.range_index = high_range
                state.excitation_index = 6
                state.excitation_percent = 100
                ended = run_end(backend, "stopped", context)
                self.assertEqual(state.excitation_index, expected_min_exc)
                self.assertEqual(state.excitation_percent, 5)
                self.assertIn(expected_label, ended["Excitation Safety"])
                self.assertEqual(ended["Current Excitation (%)"], 5)
                self.assertEqual(ended["Current Excitation Current"], "50 pA")

                state.excitation_index = 6
                state.excitation_percent = 100
                closed = backend.close(context)
                self.assertEqual(state.excitation_index, expected_min_exc)
                self.assertEqual(state.excitation_percent, 5)
                self.assertEqual(closed["Connection"], "Disconnected")
                self.assertEqual(closed["Current Excitation Current"], "-")

    def test_autorange_mode_preserves_bridge_trace_handles_mid_read_step_and_restores_safe_state(
        self,
    ) -> None:
        state = _FakeVisaState()
        # 模拟仪器上已预先设定自定义跟踪表（T=4），运行期间不得被模块改写。
        state.autorange_trace = 4
        state.resistance_reply_by_sensor[1] = "+15.248 M OHMR"
        state.reactance_reply_by_sensor[1] = "+0.067 M OHMX"
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        settings["channels"]["r1"].update({
            "input_channel": 1,
            "enabled": True,
            "range_mode": "autorange",
            "autorange_min_range": 1,
            "autorange_max_range": 9,
            "filter_index": 1,
        })
        backend = self._backend(state, waits)
        # 两组温场快照：第一组在换挡前采集（~10.5 K），第二组在换挡重等稳定后采集（~20.5 K）。
        samples = [
            _system_sample(1.0, 10.0, 100.0),
            _system_sample(2.0, 11.0, 120.0),
            _system_sample(11.0, 20.0, 200.0),
            _system_sample(12.0, 21.0, 220.0),
        ]
        context = self._context(messages, samples)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        # 场景 A：自动量程在 1/4 等待点（5R）与等待结束点（2R/3E）之间换挡，
        # 应跳过 dwell 与采数，直接重等一整轮 [2.25, 6.75]，稳定后再进 dwell [1.0] 并读数。
        state_settle = _FakeVisaState()
        state_settle.autorange_trace = 4
        state_settle.resistance_reply_by_sensor[1] = "+15.248 M OHMR"
        state_settle.reactance_reply_by_sensor[1] = "+0.067 M OHMX"
        state_settle.autorange_target_by_sensor[1] = (5, 0)
        # GET 6 顺序：_configure_sensor 回读(不变)、1/4 点(不变仍 5R)、结束点(跳到 2R/3E)。
        state_settle.autorange_get6_shifts = [None, None, (2, 3)]
        waits_settle: list[float] = []
        messages_settle: list[tuple[str, dict]] = []
        backend_settle = self._backend(state_settle, waits_settle)
        context_settle = self._context(messages_settle, _samples(1))
        open_module(backend_settle, context_settle)
        backend_settle.configure(settings, context_settle)
        run_start(backend_settle, context_settle)
        measure_module(backend_settle, context_settle, 1)
        self.assertEqual(waits_settle, [2.25, 6.75, 2.25, 6.75, 1.0])
        self.assertEqual(
            sum(
                action == "query" and cmd == "GET 0"
                for action, cmd in state_settle.commands
            ),
            1,
        )
        settle_rows = [
            payload["values"]
            for kind, payload in messages_settle
            if kind == "row"
        ]
        self.assertEqual(len(settle_rows), 1)
        self.assertAlmostEqual(settle_rows[0]["R1"], 0.015248, places=6)
        self.assertEqual(state_settle.autorange_trace, 4)

        # 场景 B：模拟仪器自定义 Trace 在第一次读数瞬间从 3R 步进到 2R/3E（3 mA）：
        # 后端必须重新等待完整 1/4 + 3/4 switch_settle_seconds 与 dwell_seconds 并刷新温场快照后再重读，
        # 且绝不发送 AUTORANGE T= 或 AUTORANGE U= 覆盖仪器既有 Trace。
        state.autorange_target_by_sensor[1] = (3, 0)
        state.autorange_mid_read_shift = (2, 3)
        measure_module(backend, context, 1)

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["StatusCode"], 0)
        self.assertAlmostEqual(rows[0]["R1"], 0.015248, places=6)
        self.assertAlmostEqual(rows[0]["X1"], 0.000067, places=6)
        self.assertAlmostEqual(rows[0]["TemperatureAverage"], 20.5, places=6)
        self.assertAlmostEqual(rows[0]["FieldAverage"], 210.0, places=6)
        self.assertIn(("write", "AUTORANGE L=19"), state.commands)
        self.assertIn(("write", "AUTORANGE 1"), state.commands)
        self.assertFalse(
            any(
                action == "write"
                and (
                    command.upper().startswith("AUTORANGE T=")
                    or command.upper().startswith("AUTORANGE U=")
                )
                for action, command in state.commands
            )
        )
        self.assertEqual(state.autorange_trace, 4)
        self.assertEqual(waits, [2.25, 6.75, 1.0, 2.25, 6.75, 1.0])
        # 测量结束后必须关闭自动量程、恢复当前量程（2R）最低激励 0E x 5%，并刷新状态页当前电流。
        self.assertEqual(state.autorange, 0)
        self.assertEqual(state.range_index, 2)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        merged_status: dict[str, object] = {}
        for kind, payload in messages:
            if kind == "status":
                merged_status.update(payload["values"])
        self.assertEqual(merged_status["Current Range Index"], "2 (200 mOhm)")
        self.assertEqual(merged_status["Current Excitation Index"], "0 (20 uV)")
        self.assertEqual(merged_status["Current Excitation (%)"], 5)
        self.assertEqual(merged_status["Current Excitation Current"], "5 uA")

        # 场景 C：连续两轮 1/4 与结束点都不一致（超过 retry_attempts=2），必须报不稳定并恢复安全状态。
        state_osc = _FakeVisaState()
        state_osc.autorange_target_by_sensor[1] = (5, 0)
        state_osc.autorange_get6_shifts = [None, None, (4, 0), None, (3, 0)]
        backend_osc = self._backend(state_osc, [])
        context_osc = self._context([], _samples(1))
        open_module(backend_osc, context_osc)
        backend_osc.configure(settings, context_osc)
        run_start(backend_osc, context_osc)
        with self.assertRaises(ModuleError) as unstable:
            measure_module(backend_osc, context_osc, 1)
        self.assertEqual(unstable.exception.code, "LR700_AUTORANGE_UNSTABLE")
        self.assertEqual(state_osc.autorange, 0)
        self.assertEqual(state_osc.excitation_percent, 5)

        # 若自动量程回读超出 L= 范围（例如 0R < min=1），必须判为设置校验失败。
        state.autorange_target_by_sensor[1] = (0, 0)
        state.autorange_mid_read_shift = None
        context2 = self._context([], _samples(1))
        with self.assertRaises(ModuleError) as out_of_bounds:
            measure_module(backend, context2, 1)
        self.assertEqual(
            out_of_bounds.exception.code,
            "LR700_SETTINGS_VERIFY_FAILED",
        )
        self.assertEqual(state.autorange, 0)
        self.assertEqual(state.excitation_percent, 5)

        # 自动量程多次重试重等稳定也必须纳入单次 Measure 超时预算。
        timeout_bad = default_settings()
        timeout_bad["resource"] = "GPIB0::18::INSTR"
        timeout_bad["switch_settle_seconds"] = 30.0
        timeout_bad["retry_attempts"] = 4
        timeout_bad["channels"]["r1"].update({
            "range_mode": "autorange",
            "filter_index": 2,
        })
        with self.assertRaises(ModuleError) as ar_timeout:
            backend.configure(timeout_bad, context2)
        self.assertEqual(
            ar_timeout.exception.code,
            "LR700_MEASURE_TIMEOUT_UNSAFE",
        )

    def test_transient_read_failure_reopens_and_resolves_warning(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["GET 0"] = 1
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "bridge-lr700"
        backend = self._backend(state, waits)
        context = self._context(
            messages,
            _samples(1),
            measurement_resources({
                "bridge-lr700": "GPIB0::18::INSTR",
            }),
        )
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        measure_module(backend, context)

        self.assertGreaterEqual(len(state.opened), 2)
        self.assertTrue(
            all(
                resource == "GPIB0::18::INSTR"
                for resource, _timeout in state.opened
            )
        )
        self.assertEqual(
            sum(
                action == "query"
                and command == "GET 0"
                for action, command in state.commands
            ),
            2,
        )
        self.assertIn(0.2, waits)
        warning_codes = [
            payload.get("code")
            for kind, payload in messages
            if kind == "warning"
        ]
        resolve_codes = [
            payload.get("code")
            for kind, payload in messages
            if kind == "resolve"
        ]
        self.assertIn("LR700_IO_RETRY", warning_codes)
        self.assertIn("LR700_IO_RETRY", resolve_codes)

    def test_invalid_measurements_mark_rows_and_continue_scan(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.query_overrides["GET 0"] = "BROKEN"
        messages: list[tuple[str, dict]] = []
        settings = _two_slot_settings()
        backend = self._backend(state, [])
        context = self._context(messages, _samples(2))
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        self._measure_enabled_slots(backend, context)

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["StatusCode"], 3)
        self.assertNotIn("R1", rows[0])
        self.assertNotIn("X1", rows[0])
        self.assertEqual(rows[1]["StatusCode"], 3)
        self.assertNotIn("R2", rows[1])
        self.assertNotIn("X2", rows[1])
        self.assertEqual(state.range_index, 9)
        self.assertEqual(state.excitation_index, 4)
        self.assertEqual(state.excitation_percent, 5)
        invalid_warnings = [
            payload
            for kind, payload in messages
            if kind == "warning"
            and payload["code"] == "LR700_READING_INVALID"
        ]
        self.assertEqual(
            {payload["context"] for payload in invalid_warnings},
            {"R1 / sensor 5", "R2 / sensor 12"},
        )

    def test_cleanup_failure_keeps_safety_error_and_sensor_context(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state, [])
        context = self._context(messages, _samples(1))
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        backend._best_effort_safe_state = (
            lambda _api: "simulated cleanup failure"
        )

        with self.assertRaises(ModuleError) as captured:
            measure_module(backend, context)

        self.assertEqual(
            captured.exception.code,
            "LR700_SAFE_STATE_FAILED",
        )
        self.assertEqual(
            captured.exception.context,
            "sensor 1",
        )
        self.assertIn(
            "after sensor 1",
            str(captured.exception),
        )
        merged_status: dict[str, object] = {}
        for kind, payload in messages:
            if kind == "status":
                merged_status.update(payload["values"])
        self.assertEqual(
            merged_status["Excitation Safety"],
            "Minimum excitation unconfirmed",
        )
        self.assertEqual(
            merged_status["Current Excitation Current"],
            "Unknown",
        )

    def test_uncertain_write_is_not_replayed_and_cleanup_reopens(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["SELECT S=01"] = 1
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages, _samples(1))
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        with self.assertRaises(ModuleError) as captured:
            measure_module(backend, context)

        self.assertEqual(
            captured.exception.code,
            "LR700_WRITE_UNCERTAIN",
        )
        self.assertEqual(
            sum(
                action == "write"
                and command == "SELECT S=01"
                for action, command in state.commands
            ),
            1,
        )
        self.assertGreaterEqual(len(state.opened), 2)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)

    def test_stale_system_snapshot_is_fatal_and_safe_state_is_restored(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(
            messages,
            [
                _system_sample(1.0, 1.0, 10.0),
                _system_sample(1.0, 2.0, 20.0),
            ],
        )
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        with self.assertRaises(ModuleError) as captured:
            measure_module(backend, context)

        self.assertEqual(
            captured.exception.code,
            "LR700_SYSTEM_SNAPSHOT_NOT_FRESH",
        )
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)

    def test_close_confirms_minimum_excitation_and_closes_transport(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        open_module(backend, context)
        backend.configure(settings, context)
        state.excitation_index = 6
        state.excitation_percent = 100

        status = backend.close(context)

        self.assertIsNone(backend.transport)
        self.assertGreaterEqual(state.closed, 1)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertEqual(
            status["Connection"],
            "Disconnected",
        )

    def test_reapply_confirms_minimum_excitation_before_closing_old_session(
        self,
    ) -> None:
        state = _FakeVisaState()
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context([])
        open_module(backend, context)
        backend.configure(settings, context)

        # 模拟 Idle 期间前面板把激励调高。重新 Apply 不能先关闭旧 session；
        # 必须先发绝对最低激励命令并通过 GET 6 确认。
        state.excitation_index = 6
        state.excitation_percent = 100
        state.commands.clear()

        backend.configure(settings, context)

        close_index = state.commands.index(("close", ""))
        for command in (
            "AUTORANGE 0",
            "VAREXC =05",
            "VAREXC 1",
            "EXCITATION 0",
        ):
            self.assertLess(
                state.commands.index(("write", command)),
                close_index,
            )
        # 百分比降到 5% 应先于激励档调整。
        self.assertLess(
            state.commands.index(("write", "VAREXC 1")),
            state.commands.index(("write", "EXCITATION 0")),
        )
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)

    def test_disable_before_apply_does_not_open_or_change_instrument(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        open_module(backend, context)

        status = backend.close(context)

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertIn(
            "No instrument connection",
            status["Excitation Safety"],
        )

    def test_safe_cleanup_discards_broken_session_and_retries_once(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        open_module(backend, context)
        backend.configure(settings, context)
        state.excitation_index = 6
        state.excitation_percent = 100
        state.failures["AUTORANGE 0"] = 1

        status = run_end(backend, "stopped", context)

        self.assertGreaterEqual(len(state.opened), 2)
        self.assertGreaterEqual(state.closed, 1)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertIn(
            "20 uV x 5%",
            status["Excitation Safety"],
        )

    def test_apply_fails_when_minimum_excitation_cannot_be_read_back(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.query_overrides["GET 6"] = "5R,6E,100%,1F,0M,0L,01S"
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        open_module(backend, context)

        with self.assertRaises(ModuleError) as captured:
            backend.configure(settings, context)

        self.assertEqual(
            captured.exception.code,
            "LR700_SAFE_STATE_FAILED",
        )
        self.assertIsNone(backend.transport)
        self.assertEqual(backend.applied_settings, {})
        self.assertGreaterEqual(len(state.opened), 2)

    def test_filter_mapping_three_times_settle_and_per_slot_duration_fail_closed(
        self,
    ) -> None:
        self.assertEqual(
            FILTERS,
            (
                (0, "1 s", 1.0),
                (1, "3 s", 3.0),
                (2, "10 s", 10.0),
            ),
        )
        self.assertEqual(default_settings()["switch_settle_seconds"], 9.0)

        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)

        # 1 s -> 至少 3 s，3 s -> 至少 9 s，10 s -> 至少 30 s。
        for filter_idx, too_short, minimum_ok in (
            (0, 2.9, 3.0),
            (1, 8.9, 9.0),
            (2, 29.9, 30.0),
        ):
            with self.subTest(filter_idx=filter_idx):
                bad = default_settings()
                bad["resource"] = "GPIB0::18::INSTR"
                bad["channels"]["r1"]["filter_index"] = filter_idx
                bad["switch_settle_seconds"] = too_short
                with self.assertRaises(ModuleError) as err:
                    backend.configure(bad, context)
                self.assertEqual(err.exception.code, "LR700_INVALID_SETTINGS")
                self.assertIn("3x", str(err.exception))

                ok = default_settings()
                ok["resource"] = "GPIB0::18::INSTR"
                ok["channels"]["r1"]["filter_index"] = filter_idx
                ok["switch_settle_seconds"] = minimum_ok
                normalized = backend._normalized_settings(
                    ok,
                    require_resource=True,
                    operation_timeout_seconds=120.0,
                )
                self.assertEqual(
                    normalized["switch_settle_seconds"],
                    minimum_ok,
                )

        duration_unsafe = default_settings()
        duration_unsafe["resource"] = "GPIB0::18::INSTR"
        duration_unsafe["switch_settle_seconds"] = 70.0
        duration_unsafe["dwell_seconds"] = 70.0
        for channel in duration_unsafe["channels"].values():
            channel["enabled"] = True
        with self.assertRaises(ModuleError) as duration_error:
            backend.configure(duration_unsafe, context)
        self.assertEqual(
            duration_error.exception.code,
            "LR700_MEASURE_TIMEOUT_UNSAFE",
        )
        self.assertEqual(state.opened, [])

    def test_invalid_range_excitation_and_autorange_limits_rejected_before_connect(
        self,
    ) -> None:
        state = _FakeVisaState()
        backend = self._backend(state)
        context = self._context([])

        invalid_pairs = (
            (0, 2),  # 2 mOhm max is 60 uV (1)
            (1, 4),  # 20 mOhm max is 600 uV (3)
            (2, 6),  # 200 mOhm max is 6 mV (5)
            (8, 0),  # 200 kOhm min is 200 uV (2)
            (8, 1),
            (9, 0),  # 2 MOhm min is 2 mV (4)
            (9, 3),
        )
        for range_idx, exc_idx in invalid_pairs:
            with self.subTest(range_idx=range_idx, exc_idx=exc_idx):
                settings = default_settings()
                settings["resource"] = "GPIB0::18::INSTR"
                settings["channels"]["r1"]["range_index"] = range_idx
                settings["channels"]["r1"]["excitation_index"] = exc_idx
                with self.assertRaises(ModuleError) as captured:
                    backend.configure(settings, context)
                self.assertEqual(
                    captured.exception.code,
                    "LR700_INVALID_SETTINGS",
                )
        self.assertEqual(state.opened, [])

        # 自动量程要求上限严格大于下限；固定量程模式下停用的自动量程上下限不阻断 Apply。
        bad_ar = default_settings()
        bad_ar["resource"] = "GPIB0::18::INSTR"
        bad_ar["channels"]["r1"].update({
            "range_mode": "autorange",
            "autorange_min_range": 5,
            "autorange_max_range": 5,
        })
        with self.assertRaises(ModuleError) as ar_err:
            backend.configure(bad_ar, context)
        self.assertEqual(ar_err.exception.code, "LR700_INVALID_SETTINGS")
        self.assertEqual(state.opened, [])

        fixed_with_stale_ar = default_settings()
        fixed_with_stale_ar["resource"] = "GPIB0::18::INSTR"
        fixed_with_stale_ar["channels"]["r1"].update({
            "range_mode": "fixed",
            "autorange_min_range": 6,
            "autorange_max_range": 5,
        })
        normalized = backend._normalized_settings(
            fixed_with_stale_ar,
            require_resource=True,
            operation_timeout_seconds=120.0,
        )
        self.assertEqual(normalized["channels"]["r1"]["range_mode"], "fixed")

    def test_lr700_visa_transport_sets_eoi_and_query_delay_without_touching_core_defaults(
        self,
    ) -> None:
        from labcontrol.measurement.visa import VisaSession

        created_handles = []

        class _FakeHandle:
            def __init__(self) -> None:
                self.timeout = None
                self.read_termination = "UNSET"
                self.write_termination = "UNSET"
                self.query_delay = None
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class _FakeRM:
            def open_resource(self, address, open_timeout=None):
                handle = _FakeHandle()
                handle.address = address
                handle.open_timeout = open_timeout
                created_handles.append(handle)
                return handle

        fake_pyvisa = types.ModuleType("pyvisa")
        fake_pyvisa.ResourceManager = _FakeRM

        with patch.dict(sys.modules, {"pyvisa": fake_pyvisa}):
            lr_session = LR700VisaTransport("GPIB0::18::INSTR", 3.0)
            core_session = VisaSession("GPIB0::10::INSTR", 3.0)
            lr_session.close()
            core_session.close()

        lr_handle, core_handle = created_handles
        self.assertIsNone(lr_handle.read_termination)
        self.assertEqual(lr_handle.write_termination, "\n")
        self.assertAlmostEqual(lr_handle.query_delay, 0.2)
        # 框架通用 VisaSession 默认值保持不变。
        self.assertEqual(core_handle.read_termination, "\n")
        self.assertEqual(core_handle.write_termination, "\n")
        self.assertIsNone(core_handle.query_delay)

    def test_duplicate_physical_inputs_fail_before_connect(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        settings["channels"]["r2"]["input_channel"] = 1
        backend = self._backend(state)

        with self.assertRaises(ModuleError) as captured:
            backend.configure(
                settings,
                self._context(messages),
            )

        self.assertEqual(
            captured.exception.code,
            "LR700_INVALID_SETTINGS",
        )
        self.assertEqual(state.opened, [])

    def test_response_parser_handles_documented_milli_mega_and_common_mode_hint(
        self,
    ) -> None:
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "+1.50000 M OHMR",
                "R",
                0,
                "GET 0",
            ),
            1.5e-3,
        )
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "+1.50000 M OHMR",
                "R",
                9,
                "GET 0",
            ),
            1.5e6,
        )
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "-2.00000 U OHMX",
                "X",
                3,
                "GET 1",
            ),
            -2.0e-6,
        )
        self.assertEqual(LR700Backend._status(0), ("NORMAL", ""))
        self.assertEqual(LR700Backend._status(64)[0], "OVER_RANGE")
        self.assertEqual(LR700Backend._status(16)[0], "OVERLOAD")
        self.assertNotIn(COMMON_MODE_OVERLOAD_HINT, LR700Backend._status(16)[1])
        self.assertEqual(LR700Backend._status(16 | 64)[0], "OVERLOAD")

        for bits, expected_bit_name in (
            (4, "Common-mode overload +"),
            (8, "Common-mode overload -"),
            (4 | 128, "Common-mode overload +"),
        ):
            status, details = LR700Backend._status(bits)
            self.assertEqual(status, "OVERLOAD")
            self.assertIn(expected_bit_name, details)
            self.assertIn(COMMON_MODE_OVERLOAD_HINT, details)

        with self.assertRaises(ModuleError):
            LR700Backend._parse_measurement(
                "+1.00000 M OHMR",
                "R",
                5,
                "GET 0",
            )

    def test_connection_test_uses_unsaved_address_and_performs_no_write(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        current = default_settings()
        current["resource"] = "GPIB0::7::INSTR"
        context = self._context(messages)
        open_module(backend, context)

        run_action(backend, "test_connection", {"settings": current}, context)

        self.assertEqual(state.opened[-1][0], "GPIB0::7::INSTR")
        self.assertFalse(
            any(action == "write" for action, _command in state.commands)
        )
        self.assertGreaterEqual(state.closed, 1)


class LR700FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance()
            or QApplication([])
        )

    def test_settings_round_trip_valid_excitation_filter_and_autorange_ui(
        self,
    ) -> None:
        context = ModuleUIAPI(resources=measurement_resources({
            "bridge-lr700": "GPIB0::18::INSTR",
            "alternate-bridge": "GPIB0::5::INSTR",
        }))
        frontend = LR700Frontend(context)
        owner = QWidget()
        settings_page = frontend
        status_page = frontend.status_widget
        self.assertGreaterEqual(settings_page.sizeHint().width(), 1180)
        self.assertGreaterEqual(settings_page.sizeHint().height(), 700)

        # 滤波下拉框必须为 1 s / 3 s / 10 s，不包含 Off。
        r1_filter = frontend.channel_widgets["r1"]["filter_index"]
        filter_labels = [r1_filter.itemText(i) for i in range(r1_filter.count())]
        self.assertEqual(filter_labels, ["1 s", "3 s", "10 s"])

        settings = _two_slot_settings()
        settings["resource"] = "bridge-lr700"
        settings["channels"]["r3"].update({
            "input_channel": 3,
            "enabled": True,
            "range_mode": "autorange",
            "autorange_min_range": 1,
            "autorange_max_range": 9,
            "filter_index": 1,
        })
        settings["channels"]["r4"].update({
            "input_channel": 16,
            "enabled": True,
            "range_mode": "fixed",
            "range_index": 8,
            "excitation_index": 2,
            "excitation_percent": 25,
            "filter_index": 1,
        })
        frontend.load(settings)
        frontend.show_status({"Connection": "Disconnected"})

        self.assertEqual(frontend.dump(), settings)
        # r3 为自动量程：固定量程控件禁用、自动量程控件启用。
        self.assertFalse(frontend.channel_widgets["r3"]["range_index"].isEnabled())
        self.assertTrue(
            frontend.channel_widgets["r3"]["autorange_min_range"].isEnabled()
        )
        # 切换 r1 到 2 MOhm (9) 时，激励下拉只保留 2 mV / 6 mV / 20 mV (4, 5, 6)。
        r1_range = frontend.channel_widgets["r1"]["range_index"]
        r1_exc = frontend.channel_widgets["r1"]["excitation_index"]
        r1_range.setCurrentIndex(r1_range.findData(9))
        exc_values = [r1_exc.itemData(i) for i in range(r1_exc.count())]
        self.assertEqual(exc_values, [4, 5, 6])
        self.assertEqual(r1_exc.currentData(), 4)

        self.assertGreaterEqual(frontend.resource.count(), 2)
        self.assertEqual(
            frontend.status_labels["Connection"].text(),
            "Disconnected",
        )
        actions: list[tuple[str, dict]] = []
        context.actionRequested.connect(
            lambda action, payload: actions.append((action, payload))
        )
        frontend.resource.setCurrentIndex(
            frontend.resource.findData("alternate-bridge")
        )
        frontend.test_connection_button.click()
        self.application.processEvents()
        self.assertEqual(actions[-1][0], "test_connection")
        self.assertEqual(
            actions[-1][1]["settings"]["resource"],
            "alternate-bridge",
        )
        settings_page.deleteLater()
        status_page.deleteLater()
        owner.deleteLater()
        self.application.processEvents()

    def test_nested_settings_can_be_saved_as_toml(
        self,
    ) -> None:
        settings = _two_slot_settings()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)


class LR700ManifestTests(unittest.TestCase):
    def test_minimal_manifest_and_backend_sparse_columns(
        self,
    ) -> None:
        descriptor = load_manifest(MODULE)
        self.assertTrue(descriptor.valid, descriptor.error)
        self.assertEqual(descriptor.id, "lr700")
        self.assertEqual(descriptor.version, "0.1.0b7")
        names = list(LR700Backend.columns)
        self.assertEqual(
            names[:4],
            ["TemperatureAverage", "FieldAverage", "R1", "X1"],
        )
        self.assertEqual(names[-3:], ["R4", "X4", "StatusCode"])
        self.assertEqual(len(names), 11)
        self.assertEqual(descriptor.columns, ())


if __name__ == "__main__":
    unittest.main()
