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
    ModuleWarning,
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
        self.rset = 0.0
        self.xset = 0.0
        self.ignore_mode_one = False
        self.local_lockout = 0
        self.sensor = 0
        self.autorange = 0
        self.autorange_limits: tuple[int, int] = (0, 9)
        self.autorange_trace = 1
        self.autorange_user_trace: tuple[int, ...] = ()
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
        elif upper.startswith("AUTORANGE U="):
            digits = "".join(upper.split("=", 1)[1].split())
            assert len(digits) == 10, f"AUTORANGE U= requires 10 digits: {command}"
            parsed = tuple(int(ch) for ch in digits)
            for r_idx, e_idx in enumerate(parsed):
                assert e_idx in valid_excitation_indices(r_idx), (
                    f"Invalid AUTORANGE U= entry R={r_idx}, E={e_idx}"
                )
            self.state.autorange_user_trace = parsed
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
            requested = int(command.split(" ", 1)[1])
            if not (requested == 1 and self.state.ignore_mode_one):
                self.state.mode = requested
        elif upper == "OFFSET =0":
            self.state.rset = 0.0
            self.state.xset = 0.0
        elif upper.startswith("OFFSET R="):
            counts = int(command.split("=", 1)[1])
            self.state.rset = counts / 200000 * (0.002 * 10**self.state.range_index)
        elif upper.startswith("SELECT S="):
            self.state.sensor = int(command.rsplit("=", 1)[1])
        elif upper.startswith("RANGE "):
            target = int(command.split(" ", 1)[1])
            if self.state.range_index != target:
                self.state.rset = 0.0
                self.state.xset = 0.0
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
        if command == "GET 4":
            return f"{self.state.rset:+.12f} OHMRSET"
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
        timeout: float = 120.0,
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
            timeout,
            resources=resources,
        )

    @staticmethod
    def _measure_enabled_slots(
        backend,
        context: TestModuleAPI,
    ) -> None:
        for logical_slot in module_slots(backend):
            measure_module(backend, context, logical_slot)

    def _common_settings(self, mode="autorange", limit=0.003):
        return {"resource": "GPIB0::18::INSTR", "common": {
            "range_mode": mode, "range_index": 2, "current_a": 0.003,
            "current_limit_a": limit, "autorange_min_range": 1,
            "autorange_max_range": 9, "filter_index": 1},
            "channels": {f"r{i}": {"enabled": i <= 2, "input_channel": i,
                "independent": False, "range_index": 2, "current_a": 0.003} for i in range(1, 5)}}

    def test_apply_generates_trace_once_and_switch_same_range_only_selects(self):
        state = _FakeVisaState()
        state.range_index = 2
        backend = self._backend(state, [])
        context = self._context([], _samples(3))
        settings = self._common_settings()
        open_module(backend, context)
        backend.configure(settings, context)
        self.assertEqual(state.autorange_user_trace, (0, 1, 3, 5, 6, 6, 6, 6, 6, 6))
        self.assertEqual(state.autorange_trace, 4)
        self.assertEqual(state.autorange, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertEqual(backend.applied_settings["autorange_trace"], list(state.autorange_user_trace))
        run_start(backend, context)
        measure_module(backend, context, 1)
        start = len(state.commands)
        measure_module(backend, context, 2)
        self.assertEqual([c for a, c in state.commands[start:] if a == "write"], ["SELECT S=02"])
        measure_module(backend, context, 1)
        self.assertEqual(sum(c.startswith("AUTORANGE U=") for a, c in state.commands if a == "write"), 1)
        run_end(backend, "completed", context)
        self.assertEqual((state.autorange, state.excitation_percent), (0, 5))
        backend.close(context)

    def test_generated_trace_limits_and_reapply_replaces_saved_map(self):
        state = _FakeVisaState()
        state.range_index = 2
        backend = self._backend(state, [])
        context = self._context([], _samples(1))
        settings = self._common_settings(limit=0.0003)
        open_module(backend, context)
        backend.configure(settings, context)
        self.assertEqual(backend.applied_settings["effective_autorange_min_range"], 2)
        for r in range(2, 10):
            self.assertLessEqual(excitation_current_a(r, state.autorange_user_trace[r]), 0.0003 * (1 + 1e-12))
        run_start(backend, context)
        self.assertEqual(state.autorange_limits, (2, 9))
        self.assertEqual(state.excitation_index, 1)
        run_end(backend, "completed", context)
        settings["common"]["current_limit_a"] = 0.003
        backend.configure(settings, context)
        self.assertEqual(backend.applied_settings["effective_autorange_min_range"], 1)
        self.assertEqual(state.autorange_user_trace[2], 3)
        self.assertEqual(backend._autorange_last_range, {})
        backend.close(context)

    def test_fixed_common_same_range_select_only_and_independent_current_restores(self):
        state = _FakeVisaState()
        state.range_index = 2
        backend = self._backend(state, [])
        context = self._context([], _samples(3))
        settings = self._common_settings(mode="fixed")
        settings["channels"]["r3"].update(enabled=True, independent=True, current_a=0.00123)
        open_module(backend, context)
        backend.configure(settings, context)
        self.assertFalse(any(c.startswith("AUTORANGE U=") for a, c in state.commands if a == "write"))
        run_start(backend, context)
        measure_module(backend, context, 1)
        start = len(state.commands)
        measure_module(backend, context, 2)
        self.assertEqual([c for a, c in state.commands[start:] if a == "write"], ["SELECT S=02"])
        start = len(state.commands)
        measure_module(backend, context, 3)
        writes = [c for a, c in state.commands[start:] if a == "write"]
        self.assertLess(writes.index("VAREXC =05"), writes.index("SELECT S=03"))
        self.assertAlmostEqual(excitation_current_a(state.range_index, state.excitation_index, state.excitation_percent), 0.00123)
        run_end(backend, "completed", context)
        self.assertEqual(state.excitation_percent, 5)
        backend.close(context)

    def test_independent_current_over_limit_is_rejected_before_connection(self):
        state = _FakeVisaState()
        backend = self._backend(state, [])
        context = self._context([])
        settings = self._common_settings()
        settings["channels"]["r2"].update(independent=True, current_a=0.004)
        open_module(backend, context)
        with self.assertRaises(ModuleError):
            backend.configure(settings, context)
        self.assertEqual(state.opened, [])

    def test_generated_trace_cache_restores_matching_excitation(self):
        state = _FakeVisaState()
        state.range_index = 2
        backend = self._backend(state, [])
        context = self._context([], _samples(2))
        settings = self._common_settings(limit=0.001)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        measure_module(backend, context, 1)
        run_end(backend, "completed", context)
        run_start(backend, context)
        self.assertEqual((state.range_index, state.excitation_index, state.excitation_percent), (2, 2, 100))
        measure_module(backend, context, 1)
        run_end(backend, "completed", context)
        backend.close(context)

    def test_trace_uses_largest_legal_excitation_without_exceeding_limit(self):
        for limit in (0.03, 0.003, 0.001, 0.0003, 1e-6, 1e-9):
            with self.subTest(limit=limit):
                normalized = LR700Backend._normalized_settings(self._common_settings(limit=limit),
                    require_resource=True, operation_timeout_seconds=120)
                channel = normalized["channels"]["r1"]
                lo, hi = channel["autorange_min_range"], channel["autorange_max_range"]
                for r in range(lo, hi + 1):
                    e = channel["autorange_trace"][r]
                    self.assertIn(e, valid_excitation_indices(r))
                    self.assertLessEqual(excitation_current_a(r, e), limit * (1 + 1e-12))
                    for higher in valid_excitation_indices(r):
                        if higher > e:
                            self.assertGreater(excitation_current_a(r, higher), limit * (1 + 1e-12))

    def test_fixed_low_current_does_not_require_usable_autorange_trace(self):
        settings = self._common_settings(mode="fixed", limit=1e-10)
        settings["common"].update(range_index=9, current_a=1e-10)
        normalized = LR700Backend._normalized_settings(settings, require_resource=True, operation_timeout_seconds=120)
        channel = normalized["channels"]["r1"]
        self.assertAlmostEqual(excitation_current_a(9, channel["excitation_index"], channel["excitation_percent"]), 1e-10)

    def test_mixed_autorange_and_fixed_override_returns_to_saved_trace(self):
        state = _FakeVisaState()
        state.range_index = 2
        backend = self._backend(state, [])
        context = self._context([], _samples(3))
        settings = self._common_settings(limit=0.001)
        settings["channels"]["r2"].update(independent=True, range_index=3, current_a=0.0006)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        measure_module(backend, context, 1)
        measure_module(backend, context, 2)
        self.assertEqual(state.autorange, 0)
        self.assertAlmostEqual(excitation_current_a(state.range_index, state.excitation_index, state.excitation_percent), 0.0006)
        measure_module(backend, context, 1)
        self.assertEqual((state.autorange, state.range_index, state.excitation_index, state.excitation_percent), (1, 2, 2, 100))
        run_end(backend, "completed", context)
        backend.close(context)

    def _reference_state(self):
        state = _FakeVisaState()
        state.range_index = 2
        state.sensor = 1
        state.resistance_reply_by_sensor.update({1: "+15.00000 MOHMR", 2: "+150.00000 MOHMR"})
        return state

    def test_x10_captures_all_enabled_channels_and_restores_fixed_references(self):
        state = self._reference_state()
        waits = []
        backend = self._backend(state, waits)
        context = self._context([], _samples(4))
        open_module(backend, context)
        backend.configure(self._common_settings(mode="fixed"), context)
        run_start(backend, context)
        waits.clear()
        backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual(set(backend._x10_channels), {1, 2})
        self.assertAlmostEqual(backend._x10_channels[1]["reference"], 0.015)
        self.assertAlmostEqual(backend._x10_channels[2]["reference"], 0.150)
        self.assertEqual(sum(waits), 27.0)
        state.resistance_reply_by_sensor.update({1: "+15.00100 MOHMR", 2: "+149.99900 MOHMR"})
        for slot, reference in ((1, .015), (2, .150), (1, .015), (2, .150)):
            start = len(state.commands)
            row = measure_module(backend, context, slot)
            writes = [c for action, c in state.commands[start:] if action == "write"]
            self.assertEqual(writes[0], "MODE 0")
            self.assertEqual(writes[-1], "MODE 1")
            self.assertLess(writes.index("MODE 0"), writes.index(f"SELECT S={slot:02d}"))
            self.assertAlmostEqual(state.rset, reference)
            self.assertEqual(state.mode, 1)
            self.assertEqual(state.autorange, 0)
            self.assertEqual(row["StatusCode"], 0)
        self.assertAlmostEqual(backend._x10_channels[1]["reference"], .015)
        self.assertAlmostEqual(backend._x10_channels[2]["reference"], .150)
        start = len(state.commands)
        backend.execute_sequence_command("x10_off", {}, context)
        self.assertEqual([c for action,c in state.commands[start:] if action == "write"], ["MODE 0"])
        self.assertAlmostEqual(state.rset, .150)
        self.assertEqual(backend._x10_channels, {})
        run_end(backend, "completed", context)
        backend.close(context)

    def test_x10_freezes_autorange_and_off_resumes_normal_settings(self):
        state = self._reference_state()
        backend = self._backend(state, [])
        context = self._context([], _samples(2))
        open_module(backend, context)
        backend.configure(self._common_settings(), context)
        run_start(backend, context)
        backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual(state.autorange, 0)
        self.assertEqual(backend.applied_settings["channels"]["r1"]["range_mode"], "autorange")
        measure_module(backend, context, 1)
        self.assertEqual((state.autorange, state.mode), (0, 1))
        backend.execute_sequence_command("x10_off", {}, context)
        measure_module(backend, context, 1)
        self.assertEqual((state.autorange, state.mode), (1, 0))
        run_end(backend, "completed", context)
        backend.close(context)

    def test_x10_restores_each_range_before_reference_without_recapturing(self):
        state = self._reference_state()
        settings = self._common_settings(mode="fixed")
        settings["channels"]["r2"].update(independent=True, range_index=3, current_a=.003)
        state.resistance_reply_by_sensor[2] = "+1.50000 OHMR"
        backend = self._backend(state, [])
        context = self._context([], _samples(2))
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        backend.execute_sequence_command("x10_on", {}, context)
        for slot, expected_range, reference in ((1, 2, .015), (2, 3, 1.5)):
            measure_module(backend, context, slot)
            self.assertEqual(state.range_index, expected_range)
            self.assertAlmostEqual(state.rset, reference)
            self.assertEqual(state.mode, 1)
        run_end(backend, "stopped", context)
        self.assertEqual((state.mode, state.excitation_percent), (0, 5))
        self.assertEqual(backend._x10_channels, {})
        backend.close(context)

    def test_x10_partial_capture_failure_does_not_activate_partial_cache(self):
        state = self._reference_state()
        state.overloads[2] = 4
        backend = self._backend(state, [])
        context = self._context([])
        open_module(backend, context)
        backend.configure(self._common_settings(mode="fixed"), context)
        run_start(backend, context)
        with self.assertRaises(ModuleWarning) as error:
            backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual(error.exception.code, "LR700_REFERENCE_OVERLOAD")
        self.assertEqual(backend._x10_channels, {})
        self.assertEqual(state.mode, 0)
        self.assertEqual(state.autorange, 0)
        self.assertEqual(state.excitation_percent, 5)
        run_end(backend, "error", context)
        backend.close(context)

    def test_x10_late_cancel_reapply_and_failed_measure_restore_1x_and_safe_state(self):
        # Late cancellation during the final 10x settle wait must reset MODE 0 & 5%
        state = self._reference_state()
        backend = self._backend(state, [])
        context = self._context([], _samples(2))
        open_module(backend, context)
        backend.configure(self._common_settings(mode="fixed"), context)
        run_start(backend, context)
        waits = 0

        def cancel_on_final_settle(_api, _seconds):
            nonlocal waits
            waits += 1
            if waits == 3:
                raise InterruptedError("cancelled during final 10x settle")

        backend._waiter = cancel_on_final_settle
        with self.assertRaises(InterruptedError):
            backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual((state.mode, state.excitation_percent, backend._measurement_mode), (0, 5, 0))
        self.assertEqual(backend._x10_channels, {})

        # Re-Apply while bridge is left in MODE 1 must reset MODE 0 & _measurement_mode
        backend._waiter = lambda _api, _s: None
        backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual((state.mode, backend._measurement_mode), (1, 1))
        status = backend.configure(self._common_settings(mode="fixed"), context)
        self.assertEqual((state.mode, backend._measurement_mode, backend._x10_channels), (0, 0, {}))
        self.assertEqual(status["10x References"], "Inactive")

        # Out-of-range capture (>199999 counts) is a recoverable Warning and cleans up
        run_start(backend, context)
        state.resistance_reply_by_sensor[1] = "+200.40000 MOHMR"
        with self.assertRaises(ModuleWarning) as out_of_range:
            backend.execute_sequence_command("x10_on", {}, context)
        self.assertEqual(out_of_range.exception.code, "LR700_REFERENCE_OUT_OF_RANGE")
        self.assertEqual((state.mode, state.excitation_percent), (0, 5))

        # x10_off checks timeout before writing
        short_ctx = self._context([], timeout=5.0)
        with self.assertRaises(ModuleError) as timeout_err:
            backend.execute_sequence_command("x10_off", {}, short_ctx)
        self.assertEqual(timeout_err.exception.code, "LR700_REFERENCE_TIMEOUT_UNSAFE")
        run_end(backend, "completed", context)
        backend.close(context)

    def test_x10_clears_offsets_before_starting_autorange(self):
        state = self._reference_state()
        state.xset = 0.05
        backend = self._backend(state, [])
        context = self._context([])
        open_module(backend, context)
        backend.configure(self._common_settings(mode="autorange"), context)
        run_start(backend, context)
        start = len(state.commands)
        backend.execute_sequence_command("x10_on", {}, context)
        writes = [c for action, c in state.commands[start:] if action == "write"]
        self.assertIn("OFFSET =0", writes)
        self.assertEqual(state.xset, 0.0)
        first_offset_zero = writes.index("OFFSET =0")
        first_autorange_on = next(i for i, c in enumerate(writes) if c == "AUTORANGE 1")
        self.assertLess(first_offset_zero, first_autorange_on)
        run_end(backend, "completed", context)
        backend.close(context)

    def test_fixed_low_to_high_range_raises_range_before_select_and_9_to_7_steps_via_8(self):
        from modules.lr700.lr700 import configuration_commands

        up_cmds = configuration_commands(
            2,
            {"range_mode": "fixed", "range_index": 9, "excitation_index": 6, "excitation_percent": 100, "filter_index": 1},
            current_range_index=1,
        )
        self.assertLess(up_cmds.index("RANGE 9"), up_cmds.index("SELECT S=02"))

        down_cmds = configuration_commands(
            1,
            {"range_mode": "fixed", "range_index": 7, "excitation_index": 0, "excitation_percent": 100, "filter_index": 1},
            current_range_index=9,
        )
        self.assertLess(down_cmds.index("RANGE 8"), down_cmds.index("EXCITATION 2"))
        self.assertLess(down_cmds.index("EXCITATION 2"), down_cmds.index("RANGE 7"))
        self.assertLess(down_cmds.index("RANGE 7"), down_cmds.index("EXCITATION 0"))

    def test_fixed_current_settings_prefers_100_percent_over_low_varexc_on_exact_ties(self):
        from modules.lr700.constants import fixed_current_settings

        for r in range(10):
            for e in valid_excitation_indices(r):
                i_full = excitation_current_a(r, e, 100)
                got_e, got_pct, _actual = fixed_current_settings(r, i_full)
                self.assertEqual((got_e, got_pct), (e, 100), f"range={r}, exc={e}, I={i_full}")

    def test_disabled_row_and_all_independent_fixed_do_not_block_apply(self):
        settings = self._common_settings(mode="autorange", limit=1e-10)
        settings["channels"]["r1"].update(enabled=True, independent=True, range_index=9, current_a=1e-10)
        settings["channels"]["r2"].update(enabled=False, independent=True, range_index=2, current_a=0.003)
        normalized = LR700Backend._normalized_settings(
            settings, require_resource=True, operation_timeout_seconds=120
        )
        self.assertTrue(normalized["channels"]["r1"]["enabled"])
        self.assertFalse(normalized["channels"]["r2"]["enabled"])

    def test_x10_readback_errors_and_cancellation_propagate(self):
        for failure in ("reference", "mode", "cancel"):
            with self.subTest(failure=failure):
                state = self._reference_state()
                backend = self._backend(state, [])
                context = self._context([])
                open_module(backend, context)
                backend.configure(self._common_settings(mode="fixed"), context)
                run_start(backend, context)
                if failure == "reference":
                    state.query_overrides["GET 4"] = "+0.00000 OHMRSET"
                elif failure == "mode":
                    state.ignore_mode_one = True
                else:
                    def cancel(_api, _seconds):
                        raise InterruptedError("operator stopped")
                    backend._waiter = cancel
                with self.assertRaises(InterruptedError if failure == "cancel" else ModuleError):
                    backend.execute_sequence_command("x10_on", {}, context)
                self.assertEqual(backend._x10_channels, {})
                run_end(backend, "error", context)
                self.assertEqual(state.mode, 0)
                backend.close(context)

    def test_x10_command_metadata_and_sequence_round_trip(self):
        from labcontrol.module_commands import normalize_module_commands
        from labcontrol.sequence.model import Command, CommandType, SequenceDocument
        from labcontrol.sequence.parser import parse_sequence, serialize_sequence
        commands = normalize_module_commands("lr700", LR700Backend.sequence_commands)
        self.assertEqual([command.command_id for command in commands], ["x10_on", "x10_off"])
        document = SequenceDocument([Command(CommandType.MODULE_COMMAND, {}, module_id="lr700", module_command_id=command.command_id) for command in commands])
        result = parse_sequence(serialize_sequence(document))
        self.assertFalse(result.has_errors)
        self.assertEqual([command.module_command_id for command in result.document.commands], ["x10_on", "x10_off"])

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

    def test_autorange_mode_preserves_site_trace_handles_mid_read_step_and_restores_safe_state(
        self,
    ) -> None:
        # 场景 A：四槽位中启用 R1/R2 共享自动量程（L=19, FILTER 1），保留现场已设定的
        # trace 表、同步当前量程对应的激励档，并只开启一次
        # AUTORANGE 1；同量程切通道只发 SELECT S=nn（不跳 max_range、不反复开关 AUTORANGE、不降 5%），
        # 1/4 到等待结束之间换挡会跳过 dwell 直接重等；Run 结束才恢复 AUTORANGE 0 + 5%。
        settings_two_ar = default_settings()
        settings_two_ar["resource"] = "GPIB0::18::INSTR"
        for key, sensor in (("r1", 1), ("r2", 2)):
            settings_two_ar["channels"][key].update({
                "input_channel": sensor,
                "enabled": True,
                "range_mode": "autorange",
                "autorange_min_range": 1,
                "autorange_max_range": 9,
                "filter_index": 1,
            })

        state_settle = _FakeVisaState()
        state_settle.range_index = 2  # 桥已在 200 mOhm，不得被强行拉到 2 MOhm (9R)
        state_settle.excitation_index = 0
        state_settle.autorange_trace = 4
        state_settle.autorange_user_trace = (0, 1, 3, 5, 6, 6, 6, 6, 6, 6)
        state_settle.resistance_reply_by_sensor[1] = "+15.248 M OHMR"
        state_settle.reactance_reply_by_sensor[1] = "+0.067 M OHMX"
        state_settle.resistance_reply_by_sensor[2] = "+19.626 M OHMR"
        state_settle.reactance_reply_by_sensor[2] = "+0.061 M OHMX"
        # GET 6 顺序（autorange=1 起）：run_start回读(不变，已在 2R/3E)、
        # R1 _switch前状态查询(不变)、R1 _switch后回读(不变)、
        # R1 1/4 点(仍在 2R/3E)、R1 结束点(模拟跳到 3R/5E -> 触发重等)、
        # 重等 1/4 点(2R/3E)、重等结束点(不变)、R1 before/after(不变)。
        state_settle.autorange_get6_shifts = [
            None,
            None,
            None,
            None,
            (3, 5),
            (2, 3),
        ]
        waits_settle: list[float] = []
        messages_settle: list[tuple[str, dict]] = []
        backend_settle = self._backend(state_settle, waits_settle)
        context_settle = self._context(messages_settle, _samples(2))
        open_module(backend_settle, context_settle)
        applied = backend_settle.configure(settings_two_ar, context_settle)
        # Apply 后仍处于安全 5% 状态，未提前打开 AUTORANGE 1。
        self.assertEqual(state_settle.autorange, 0)
        self.assertEqual(state_settle.excitation_percent, 5)
        self.assertEqual(applied["Current Excitation (%)"], 5)

        run_start(backend_settle, context_settle)
        self.assertEqual(state_settle.autorange, 1)
        self.assertEqual(state_settle.range_index, 2)
        # 200 mOhm (2R) 必须同步自定义 trace 表对应的 600 uV (3E -> 3 mA)，而不是停在安全态 0E。
        self.assertEqual(state_settle.excitation_index, 3)
        self.assertEqual(state_settle.autorange_trace, 4)
        self.assertEqual(
            state_settle.autorange_user_trace,
            (0, 1, 3, 5, 6, 6, 6, 6, 6, 6),
        )
        self.assertFalse(any(
            action == "write" and cmd.startswith(("AUTORANGE U=", "AUTORANGE T="))
            for action, cmd in state_settle.commands
        ))
        self.assertIn(("write", "EXCITATION 3"), state_settle.commands)
        self.assertNotIn(("write", "RANGE 9"), state_settle.commands)

        writes_before_measure = len(state_settle.commands)
        measure_module(backend_settle, context_settle, 1)
        # R1 测完后继续保持 AUTORANGE 1，不降 5%。
        self.assertEqual(state_settle.autorange, 1)
        self.assertEqual(state_settle.excitation_percent, 100)
        writes_r1 = [
            cmd
            for action, cmd in state_settle.commands[writes_before_measure:]
            if action == "write"
        ]
        self.assertEqual(writes_r1, ["SELECT S=01"])

        writes_before_r2 = len(state_settle.commands)
        measure_module(backend_settle, context_settle, 2)
        writes_r2 = [
            cmd
            for action, cmd in state_settle.commands[writes_before_r2:]
            if action == "write"
        ]
        # 共用 L=19 与 FILTER 1 且激励符合 trace 表：切到 R2 只发一条 SELECT S=02。
        self.assertEqual(writes_r2, ["SELECT S=02"])
        self.assertEqual(
            waits_settle,
            [2.25, 6.75, 2.25, 6.75, 1.0, 2.25, 6.75, 1.0],
        )
        self.assertEqual(
            sum(
                action == "query" and cmd == "GET 0"
                for action, cmd in state_settle.commands
            ),
            2,
        )
        settle_rows = [
            payload["values"]
            for kind, payload in messages_settle
            if kind == "row"
        ]
        self.assertEqual(len(settle_rows), 2)
        self.assertAlmostEqual(settle_rows[0]["R1"], 0.015248, places=6)
        self.assertAlmostEqual(settle_rows[1]["R2"], 0.019626, places=6)
        # Run 结束时恢复 AUTORANGE 0 + 5% 最低激励，并刷新状态页当前电流。
        ended = run_end(backend_settle, "completed", context_settle)
        self.assertEqual(state_settle.autorange, 0)
        self.assertEqual(state_settle.range_index, 2)
        self.assertEqual(state_settle.excitation_index, 0)
        self.assertEqual(state_settle.excitation_percent, 5)
        self.assertEqual(ended["Current Range Index"], "2 (200 mOhm)")
        self.assertEqual(ended["Current Excitation Index"], "0 (20 uV)")
        self.assertEqual(ended["Current Excitation (%)"], 5)
        self.assertEqual(ended["Current Excitation Current"], "5 uA")

        # 场景 B：读数瞬间从 3R/5E 步进到 2R/3E（3 mA）：
        # 后端必须重新等待完整 1/4 + 3/4 switch_settle_seconds 与 dwell_seconds 并刷新温场快照后再重读。
        state = _FakeVisaState()
        state.range_index = 3
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
        samples = [
            _system_sample(1.0, 10.0, 100.0),
            _system_sample(2.0, 11.0, 120.0),
            _system_sample(11.0, 20.0, 200.0),
            _system_sample(12.0, 21.0, 220.0),
        ]
        context = self._context(messages, samples)
        open_module(backend, context)
        backend.configure(settings, context)
        state.autorange_target_by_sensor[1] = (3, 5)
        run_start(backend, context)
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
        self.assertEqual(waits, [2.25, 6.75, 1.0, 2.25, 6.75, 1.0])
        self.assertEqual(state.autorange, 1)

        # 若自动量程激活期间硬件回读超出 L= 范围（例如在 1/4 稳定点读回 0R < min=1），
        # 必须判为设置校验失败并立即恢复 5%。
        state.autorange_get6_shifts = [None, None, (0, 0)]
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

        # 场景 C：连续两轮 1/4 与结束点都不一致（超过 retry_attempts=2），必须报不稳定并立即恢复安全状态。
        state_osc = _FakeVisaState()
        state_osc.range_index = 5
        state_osc.autorange_target_by_sensor[1] = (5, 6)
        state_osc.autorange_get6_shifts = [
            None,
            None,
            None,
            None,
            (4, 6),
            None,
            (3, 5),
        ]
        backend_osc = self._backend(state_osc, [])
        context_osc = self._context([], _samples(1))
        open_module(backend_osc, context_osc)
        backend_osc.configure(settings, context_osc)
        run_start(backend_osc, context_osc)
        with self.assertRaises(ModuleWarning) as unstable:
            measure_module(backend_osc, context_osc, 1)
        self.assertEqual(unstable.exception.code, "LR700_AUTORANGE_UNSTABLE")
        self.assertEqual(state_osc.autorange, 0)
        self.assertEqual(state_osc.excitation_percent, 5)

        # 场景 D：S1 停在 1R（20 mOhm, 60 uV -> 3 mA，L=19）切换到 S2（L=49，20 Ohm 以上最大仅 1 mA），
        # 必须先降 5% 并升量程至 4R 再发 SELECT S=02，然后同步 6E (20 mV)，接通 S2 后峰值电流不超过 1 mA。
        settings_limit = default_settings()
        settings_limit["resource"] = "GPIB0::18::INSTR"
        settings_limit["channels"]["r1"].update({
            "input_channel": 1,
            "enabled": True,
            "range_mode": "autorange",
            "autorange_min_range": 1,
            "autorange_max_range": 9,
            "filter_index": 1,
        })
        settings_limit["channels"]["r2"].update({
            "input_channel": 2,
            "enabled": True,
            "range_mode": "autorange",
            "autorange_min_range": 4,
            "autorange_max_range": 9,
            "filter_index": 1,
        })
        state_lim = _FakeVisaState()
        state_lim.range_index = 1
        state_lim.excitation_index = 0
        state_lim.autorange_target_by_sensor[1] = (1, 1)  # 20 mOhm, 60 uV -> 3 mA
        state_lim.autorange_target_by_sensor[2] = (4, 6)  # 20 Ohm, 20 mV -> 1 mA
        state_lim.resistance_reply_by_sensor[1] = "+10.0000 M OHMR"
        state_lim.reactance_reply_by_sensor[1] = "+0.0100 M OHMX"
        state_lim.resistance_reply_by_sensor[2] = "+12.3450  OHMR"
        state_lim.reactance_reply_by_sensor[2] = "+0.0200  OHMX"
        backend_lim = self._backend(state_lim, [])
        context_lim = self._context([], _samples(4))
        open_module(backend_lim, context_lim)
        backend_lim.configure(settings_limit, context_lim)
        run_start(backend_lim, context_lim)
        measure_module(backend_lim, context_lim, 1)

        state_lim.current_trace.clear()
        writes_before_s2 = len(state_lim.commands)
        measure_module(backend_lim, context_lim, 2)
        writes_s2 = [
            cmd
            for action, cmd in state_lim.commands[writes_before_s2:]
            if action == "write"
        ]
        # 必须先降 5%、升到 RANGE 4，再发 SELECT S=02，并同步 4R 对应激励 EXCITATION 6！
        self.assertLess(writes_s2.index("VAREXC 1"), writes_s2.index("SELECT S=02"))
        self.assertLess(writes_s2.index("RANGE 4"), writes_s2.index("SELECT S=02"))
        self.assertIn("EXCITATION 6", writes_s2)
        s2_select_idx = next(
            i
            for i, (cmd, _cur) in enumerate(state_lim.current_trace)
            if cmd == "SELECT S=02"
        )
        peak_on_s2 = max(
            cur for _cmd, cur in state_lim.current_trace[s2_select_idx:]
        )
        self.assertLessEqual(peak_on_s2, 1.0e-3 + 1e-12)

        # 场景 E：缓存异阻值切换（S1=1R/20 mOhm，S2=8R/200 kOhm，共用 L=19）：
        # 第一轮稳定后各自缓存量程；第二轮写回缓存量程时必须按自定义 trace 表更新对应激发电流：
        # - 切回 S1 (1R) 须先降 5%、经 RANGE 7/EXCITATION 0 降到 RANGE 1，并写入 EXCITATION 1 (60 uV -> 3 mA)；
        # - 切回 S2 (8R) 须先降 5%、先升 RANGE 8 再 SELECT S=02，并写入 EXCITATION 6 (20 mV -> 100 nA)；
        # - 若连续测同一量程且激励匹配 trace（S1->S1）则仍只发 SELECT S=01。
        settings_mixed = default_settings()
        settings_mixed["resource"] = "GPIB0::18::INSTR"
        for key, sensor in (("r1", 1), ("r2", 2)):
            settings_mixed["channels"][key].update({
                "input_channel": sensor,
                "enabled": True,
                "range_mode": "autorange",
                "autorange_min_range": 1,
                "autorange_max_range": 9,
                "filter_index": 1,
            })
        state_mix = _FakeVisaState()
        state_mix.range_index = 1
        state_mix.autorange_target_by_sensor[1] = (1, 1)  # 20 mOhm, 60 uV -> 3 mA
        state_mix.autorange_target_by_sensor[2] = (8, 6)  # 200 kOhm, 20 mV -> 100 nA
        state_mix.resistance_reply_by_sensor[1] = "+15.248 M OHMR"
        state_mix.reactance_reply_by_sensor[1] = "+0.067 M OHMX"
        state_mix.resistance_reply_by_sensor[2] = "+100.000 K OHMR"
        state_mix.reactance_reply_by_sensor[2] = "+0.100 K OHMX"
        backend_mix = self._backend(state_mix, [])
        context_mix = self._context([], _samples(5))
        open_module(backend_mix, context_mix)
        backend_mix.configure(settings_mixed, context_mix)
        run_start(backend_mix, context_mix)
        # 第 1 轮：S1 稳定在 1R/1E；S2 稳定在 8R/6E。
        measure_module(backend_mix, context_mix, 1)
        state_mix.range_index = 8
        state_mix.excitation_index = 6
        measure_module(backend_mix, context_mix, 2)
        self.assertEqual(backend_mix._autorange_last_range[1], 1)
        self.assertEqual(backend_mix._autorange_last_range[2], 8)

        # 第 2 轮从 S2 (8R) 切回 S1 (缓存 1R)：写回 RANGE 1 时同步写入 EXCITATION 1 (60 uV -> 3 mA)。
        state_mix.autorange_target_by_sensor.clear()
        writes_before_back_s1 = len(state_mix.commands)
        measure_module(backend_mix, context_mix, 1)
        writes_back_s1 = [
            cmd
            for action, cmd in state_mix.commands[writes_before_back_s1:]
            if action == "write"
        ]
        self.assertIn("VAREXC 1", writes_back_s1)
        self.assertIn("RANGE 7", writes_back_s1)
        self.assertIn("RANGE 1", writes_back_s1)
        self.assertIn("EXCITATION 1", writes_back_s1)
        self.assertFalse(any(
            cmd.startswith(("AUTORANGE U=", "AUTORANGE T="))
            for cmd in writes_back_s1
        ))
        self.assertLess(
            writes_back_s1.index("SELECT S=01"),
            writes_back_s1.index("RANGE 1"),
        )
        self.assertLess(
            writes_back_s1.index("RANGE 1"),
            writes_back_s1.index("EXCITATION 1"),
        )
        self.assertEqual(state_mix.range_index, 1)
        self.assertEqual(state_mix.excitation_index, 1)
        self.assertAlmostEqual(
            excitation_current_a(
                state_mix.range_index,
                state_mix.excitation_index,
                state_mix.excitation_percent,
            ),
            3.0e-3,
        )

        # 再测 S1（当前已在 1R/1E 且与 S1 缓存量程一致）：仅发 SELECT S=01！
        writes_before_same = len(state_mix.commands)
        measure_module(backend_mix, context_mix, 1)
        writes_same = [
            cmd
            for action, cmd in state_mix.commands[writes_before_same:]
            if action == "write"
        ]
        self.assertEqual(writes_same, ["SELECT S=01"])

        # 再从 S1 (1R) 切回 S2 (缓存 8R)：先降 5%、先升 RANGE 8 再发 SELECT S=02，并同步 EXCITATION 6 (20 mV -> 100 nA)！
        state_mix.current_trace.clear()
        writes_before_back_s2 = len(state_mix.commands)
        measure_module(backend_mix, context_mix, 2)
        writes_back_s2 = [
            cmd
            for action, cmd in state_mix.commands[writes_before_back_s2:]
            if action == "write"
        ]
        self.assertIn("VAREXC 1", writes_back_s2)
        self.assertIn("RANGE 8", writes_back_s2)
        self.assertIn("EXCITATION 6", writes_back_s2)
        self.assertLess(
            writes_back_s2.index("RANGE 8"),
            writes_back_s2.index("SELECT S=02"),
        )
        self.assertEqual(state_mix.range_index, 8)
        self.assertEqual(state_mix.excitation_index, 6)
        s2_back_idx = next(
            i
            for i, (cmd, _cur) in enumerate(state_mix.current_trace)
            if cmd == "SELECT S=02"
        )
        self.assertLessEqual(
            max(cur for _cmd, cur in state_mix.current_trace[s2_back_idx:]),
            100.0e-9 + 1e-15,
        )

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

    def test_cached_range_restores_excitation_without_rewriting_site_trace(self) -> None:
        state = _FakeVisaState()
        state.range_index = 2
        state.autorange_trace = 4
        state.autorange_user_trace = (0, 1, 3, 5, 6, 6, 6, 6, 6, 6)
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        settings["channels"]["r1"]["range_mode"] = "autorange"
        backend = self._backend(state, [])
        context = self._context([], _samples(1))
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        measure_module(backend, context, 1)
        run_end(backend, "completed", context)
        self.assertEqual(backend._autorange_last_range, {1: 2})
        self.assertEqual((state.excitation_index, state.excitation_percent), (0, 5))

        start = len(state.commands)
        run_start(backend, context)
        writes = [cmd for action, cmd in state.commands[start:] if action == "write"]
        self.assertLess(writes.index("EXCITATION 3"), writes.index("VAREXC 0"))
        self.assertEqual((state.range_index, state.excitation_index, state.excitation_percent), (2, 3, 100))
        self.assertEqual(state.autorange_trace, 4)
        self.assertEqual(state.autorange_user_trace, (0, 1, 3, 5, 6, 6, 6, 6, 6, 6))
        self.assertFalse(any(
            action == "write" and cmd.startswith(("AUTORANGE U=", "AUTORANGE T="))
            for action, cmd in state.commands
        ))
        run_end(backend, "completed", context)
        backend.close(context)

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

        # 自动量程要求上限严格大于下限且下限不得低于 20 mOhm (1)；固定量程模式下停用的自动量程上下限不阻断 Apply。
        for bad_min, bad_max in ((5, 5), (0, 9)):
            with self.subTest(bad_min=bad_min, bad_max=bad_max):
                bad_ar = default_settings()
                bad_ar["resource"] = "GPIB0::18::INSTR"
                bad_ar["channels"]["r1"].update({
                    "range_mode": "autorange",
                    "autorange_min_range": bad_min,
                    "autorange_max_range": bad_max,
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

    def test_common_table_round_trip_preview_and_fixed_override(self) -> None:
        context = ModuleUIAPI(resources=measurement_resources({"bridge-lr700": "GPIB0::18::INSTR"}))
        frontend = LR700Frontend(context)
        settings = frontend.dump()
        settings["resource"] = "bridge-lr700"
        settings["channels"]["r2"].update(enabled=True, independent=True, range_index=2, current_a=0.00123)
        frontend.load(settings)
        self.assertEqual(frontend.dump(), settings)
        self.assertEqual(frontend.channel_table.rowCount(), 4)
        self.assertFalse(frontend.channel_widgets["r1"]["range_index"].isEnabled())
        self.assertTrue(frontend.channel_widgets["r2"]["range_index"].isEnabled())
        self.assertIn("1.23 mA", frontend.channel_widgets["r2"]["actual"].text())
        self.assertIn("U=0135666666", frontend.trace_preview.text())
        filter_widget = frontend.common_widgets["filter_index"]
        self.assertEqual([filter_widget.itemText(i) for i in range(filter_widget.count())], ["1 s", "3 s", "10 s"])
        frontend.common_widgets["current_limit_a"].setValue(1)
        self.assertIn("200 mOhm", frontend.trace_preview.text())
        self.assertIn("exceeds limit", frontend.channel_widgets["r2"]["actual"].text())
        actions = []
        context.actionRequested.connect(lambda action, payload: actions.append((action, payload)))
        frontend.test_connection_button.click()
        self.assertEqual(actions[-1][0], "test_connection")
        self.assertEqual(actions[-1][1]["settings"]["common"]["current_limit_a"], 0.001)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, frontend.dump())
            self.assertEqual(load_settings(path), frontend.dump())
        frontend.deleteLater()
        frontend.status_widget.deleteLater()
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
        self.assertEqual(descriptor.version, "0.1.0b9")
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
