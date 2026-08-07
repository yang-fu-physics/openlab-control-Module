from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "keithley_2614b"
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.extensions.loading import load_source_object  # noqa: E402
from labcontrol.module_api import (  # noqa: E402
    ModuleError,
    _ModuleOperationCancelled as ModuleOperationCancelled,
)
from labcontrol.measurement.frontend_api import (  # noqa: E402
    ModuleUIAPI,
)
from module_contract import (  # noqa: E402
    TestModuleAPI,
    measure_module,
    module_slots,
    open_module,
    read_status,
    run_action,
    run_end,
    run_start,
)
from labcontrol.measurement.manifest import load_manifest  # noqa: E402
from labcontrol.measurement.settings import (  # noqa: E402
    load_settings,
    save_settings,
)


Keithley2614BBackend = load_source_object(
    MODULE,
    "backend:Keithley2614BBackend",
    "test_keithley_2614b_backend",
)
Keithley2614BFrontend = load_source_object(
    MODULE,
    "frontend:Keithley2614BFrontend",
    "test_keithley_2614b_frontend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_keithley_2614b_constants",
)


def _channel_state() -> dict:
    return {
        "output": False,
        "offmode": "HIGH_Z",
        "func": "DCAMPS",
        "autorangei": True,
        "autorangev": True,
        "leveli": 0.0,
        "levelv": 0.0,
        "limitv": 10.0,
        "limiti": 1.0e-3,
        "measure_autorangev": True,
        "measure_autorangei": True,
        "nplc": 1.0,
        "sense_remote": False,
        "voltage": 1.0,
        "current": 1.0e-3,
        "compliance": False,
    }


class _FakeState:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2614B,1234567,3.4.0"
        self.channels = {"smua": _channel_state(), "smub": _channel_state()}
        self.failures: dict[str, int] = {}
        self.query_overrides: dict[str, str] = {}

    def factory(self, resource: str, timeout: float):
        self.opened.append((resource, timeout))
        return _FakeTransport(self)


class _FakeTransport:
    _ASSIGNMENT = re.compile(r"^(smu[ab])\.([a-z.]+)\s*=\s*(.+)$", re.I)

    def __init__(self, state: _FakeState) -> None:
        self.state = state

    def write(self, command: str) -> None:
        self.state.commands.append(("write", command))
        self._fail(command)
        matched = self._ASSIGNMENT.match(command.strip())
        if matched is None:
            raise AssertionError(f"Unexpected write: {command}")
        smu, path, raw = matched.groups()
        smu = smu.lower()
        path = path.lower()
        value = raw.strip()
        channel = self.state.channels[smu]
        if path == "source.output":
            channel["output"] = value.upper().endswith("OUTPUT_ON")
        elif path == "source.offmode":
            channel["offmode"] = (
                "HIGH_Z" if value.upper().endswith("OUTPUT_HIGH_Z") else value
            )
        elif path == "source.func":
            channel["func"] = (
                "DCAMPS" if value.upper().endswith("OUTPUT_DCAMPS") else "DCVOLTS"
            )
        elif path == "source.autorangei":
            channel["autorangei"] = value.upper().endswith("AUTORANGE_ON")
        elif path == "source.autorangev":
            channel["autorangev"] = value.upper().endswith("AUTORANGE_ON")
        elif path in {"source.leveli", "source.levelv", "source.limitv", "source.limiti"}:
            channel[path.split(".", 1)[1]] = float(value)
        elif path == "measure.autorangev":
            channel["measure_autorangev"] = value.upper().endswith("AUTORANGE_ON")
        elif path == "measure.autorangei":
            channel["measure_autorangei"] = value.upper().endswith("AUTORANGE_ON")
        elif path == "measure.nplc":
            channel["nplc"] = float(value)
        elif path == "sense":
            channel["sense_remote"] = value.upper().endswith("SENSE_REMOTE")
        else:
            raise AssertionError(f"Unexpected assignment: {command}")

    def query(self, command: str) -> str:
        self.state.commands.append(("query", command))
        self._fail(command)
        if command in self.state.query_overrides:
            return self.state.query_overrides[command]
        if command == "*IDN?":
            return self.state.identity
        for smu in ("smua", "smub"):
            channel = self.state.channels[smu]
            if command == f"print({smu}.source.output == {smu}.OUTPUT_ON)":
                return self._boolean(channel["output"])
            if command == f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)":
                return self._boolean(channel["offmode"] == "HIGH_Z")
            if command == f"print({smu}.source.func == {smu}.OUTPUT_DCAMPS)":
                return self._boolean(channel["func"] == "DCAMPS")
            if command == f"print({smu}.source.func == {smu}.OUTPUT_DCVOLTS)":
                return self._boolean(channel["func"] == "DCVOLTS")
            if command == f"print({smu}.source.autorangei == {smu}.AUTORANGE_ON)":
                return self._boolean(channel["autorangei"])
            if command == f"print({smu}.source.autorangev == {smu}.AUTORANGE_ON)":
                return self._boolean(channel["autorangev"])
            if command == f"print({smu}.measure.autorangev == {smu}.AUTORANGE_ON)":
                return self._boolean(channel["measure_autorangev"])
            if command == f"print({smu}.measure.autorangei == {smu}.AUTORANGE_ON)":
                return self._boolean(channel["measure_autorangei"])
            if command == f"print({smu}.sense == {smu}.SENSE_REMOTE)":
                return self._boolean(channel["sense_remote"])
            for attribute in ("leveli", "levelv", "limitv", "limiti"):
                if command == f"print({smu}.source.{attribute})":
                    return f"{channel[attribute]:.12g}"
            if command == f"print({smu}.measure.nplc)":
                return f"{channel['nplc']:.12g}"
            if command == (
                f"print({smu}.measure.v(), {smu}.measure.i(), "
                f"{smu}.source.compliance)"
            ):
                return (
                    f"{channel['voltage']:.12g}\t{channel['current']:.12g}\t"
                    f"{self._boolean(channel['compliance'])}"
                )
        raise AssertionError(f"Unexpected query: {command}")

    @staticmethod
    def _boolean(value: bool) -> str:
        return "true" if value else "false"

    def _fail(self, command: str) -> None:
        remaining = self.state.failures.get(command, 0)
        if remaining > 0:
            self.state.failures[command] = remaining - 1
            raise OSError(f"simulated failure: {command}")

    def close(self) -> None:
        self.state.closed += 1
        self.state.commands.append(("close", ""))


def _settings(two_channels: bool = False) -> dict:
    result = default_settings()
    result["resource"] = "GPIB0::26::INSTR"
    result["channels"]["ch1"].update(
        {
            "enabled": True,
            "source_mode": "current",
            "source_current": "1m",
            "voltage_limit": "10",
            "sense_mode": "4wire",
            "nplc": 2.0,
        }
    )
    result["channels"]["ch2"].update(
        {
            "enabled": two_channels,
            "source_mode": "voltage",
            "source_voltage": "2",
            "current_limit": "2m",
            "sense_mode": "2wire",
            "nplc": 1.0,
        }
    )
    return result


class Keithley2614BBackendTests(unittest.TestCase):
    @staticmethod
    def _context(messages: list[tuple[str, dict]]) -> TestModuleAPI:
        return TestModuleAPI(
            {},
            lambda kind, values: messages.append((kind, values)),
            None,
            None,
            120.0,
        )

    @staticmethod
    def _backend(state: _FakeState, waiter=None):
        return Keithley2614BBackend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::26::INSTR",
                "GPIB0::5::INSTR",
            ),
            waiter=waiter or (lambda context, _seconds: context.sleep(0)),
        )

    def test_open_discovers_without_opening_or_writing(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = open_module(backend, self._context(messages))

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertEqual(status["Applied Settings"], "Not applied")

    def test_apply_configures_two_independent_channels_and_high_z_off(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        open_module(backend, context)

        status = backend.configure(settings, context)

        a = state.channels["smua"]
        b = state.channels["smub"]
        self.assertFalse(a["output"])
        self.assertFalse(b["output"])
        self.assertEqual(a["offmode"], "HIGH_Z")
        self.assertEqual(b["offmode"], "HIGH_Z")
        self.assertEqual(a["func"], "DCAMPS")
        self.assertAlmostEqual(a["leveli"], 1.0e-3)
        self.assertTrue(a["sense_remote"])
        self.assertEqual(b["func"], "DCVOLTS")
        self.assertAlmostEqual(b["levelv"], 2.0)
        self.assertFalse(b["sense_remote"])
        self.assertEqual(status["Applied Settings"], "Applied")

    def test_two_channels_are_biased_together_and_emit_one_wide_row(self) -> None:
        state = _FakeState()
        state.channels["smua"].update(voltage=1.0, current=1.0e-3)
        state.channels["smub"].update(voltage=2.0, current=2.0e-3)
        waits: list[float] = []

        def waiter(_context, seconds):
            self.assertTrue(state.channels["smua"]["output"])
            self.assertTrue(state.channels["smub"]["output"])
            waits.append(seconds)

        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waiter)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        settings["settle_seconds"] = 0.5
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        messages.clear()

        measure_module(backend, context)

        self.assertEqual(waits, [0.5])
        self.assertFalse(state.channels["smua"]["output"])
        self.assertFalse(state.channels["smub"]["output"])
        rows = [payload["values"] for kind, payload in messages if kind == "row"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["StatusCode1"], 0)
        self.assertEqual(rows[0]["StatusCode2"], 0)
        self.assertAlmostEqual(rows[0]["R1"], 1000.0)
        self.assertAlmostEqual(rows[0]["R2"], 1000.0)

    def test_one_channel_compliance_is_blank_warning_other_channel_remains_valid(self) -> None:
        state = _FakeState()
        state.channels["smub"]["compliance"] = True
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        messages.clear()

        measure_module(backend, context)

        rows = [payload["values"] for kind, payload in messages if kind == "row"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["StatusCode1"], 0)
        self.assertEqual(rows[0]["StatusCode2"], 2)
        self.assertIn("R1", rows[0])
        self.assertNotIn("R2", rows[0])
        warnings = [payload for kind, payload in messages if kind == "warning"]
        self.assertEqual(warnings[0]["context"], "ch2")

    def test_outputs_can_be_retained_between_rows_but_end_turns_both_off(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        settings["output_off_between_measurements"] = False
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)

        measure_module(backend, context)
        self.assertTrue(state.channels["smua"]["output"])
        self.assertTrue(state.channels["smub"]["output"])
        measure_module(backend, context)
        self.assertTrue(state.channels["smua"]["output"])
        self.assertTrue(state.channels["smub"]["output"])
        run_end(backend, "completed", context)

        self.assertFalse(state.channels["smua"]["output"])
        self.assertFalse(state.channels["smub"]["output"])
        self.assertEqual(
            sum(kind == "row" for kind, _payload in messages),
            2,
        )

    def test_cancel_during_shared_settle_turns_both_outputs_off(self) -> None:
        state = _FakeState()

        def cancel(_context, _seconds):
            raise ModuleOperationCancelled("stop")

        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, cancel)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        messages.clear()

        with self.assertRaises(ModuleOperationCancelled):
            measure_module(backend, context)

        self.assertFalse(state.channels["smua"]["output"])
        self.assertFalse(state.channels["smub"]["output"])
        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_second_channel_output_readback_failure_aborts_without_rows(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        messages.clear()
        command = "print(smub.source.output == smub.OUTPUT_ON)"
        state.query_overrides[command] = "false"

        with self.assertRaisesRegex(ModuleError, "interlock"):
            measure_module(backend, context)

        self.assertFalse(state.channels["smua"]["output"])
        self.assertFalse(state.channels["smub"]["output"])
        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_front_panel_level_change_is_detected_before_outputs_turn_on(self) -> None:
        state = _FakeState()
        backend = self._backend(state)
        context = self._context([])
        settings = _settings()
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        state.channels["smua"]["leveli"] = 2.0e-3
        on_before = sum(
            command.endswith("OUTPUT_ON")
            for kind, command in state.commands
            if kind == "write"
        )

        with self.assertRaisesRegex(ModuleError, "readback mismatch"):
            measure_module(backend, context)

        on_after = sum(
            command.endswith("OUTPUT_ON")
            for kind, command in state.commands
            if kind == "write"
        )
        self.assertEqual(on_before, on_after)

    def test_unconfirmed_dual_output_cleanup_is_fatal(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(two_channels=True)
        open_module(backend, context)
        backend.configure(settings, context)
        run_start(backend, context)
        messages.clear()
        read_command = (
            "print(smua.measure.v(), smua.measure.i(), smua.source.compliance)"
        )
        state.failures[read_command] = 1
        for smu in ("smua", "smub"):
            state.failures[f"{smu}.source.output = {smu}.OUTPUT_OFF"] = 1
            state.query_overrides[
                f"print({smu}.source.output == {smu}.OUTPUT_ON)"
            ] = "true"

        with self.assertRaisesRegex(ModuleError, "could not be confirmed OFF"):
            measure_module(backend, context)

        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_identity_mismatch_closes_transport(self) -> None:
        state = _FakeState()
        state.identity = "KEITHLEY INSTRUMENTS,MODEL 2612B,0,1"
        backend = self._backend(state)
        context = self._context([])
        open_module(backend, context)

        with self.assertRaisesRegex(ModuleError, "Expected Keithley Model 2614B"):
            backend.configure(_settings(), context)

        self.assertEqual(state.closed, 1)
        self.assertIsNone(backend.transport)

    def test_hardware_limit_combinations_and_enabled_channel_count_are_validated(self) -> None:
        backend = self._backend(_FakeState())
        context = self._context([])
        high_current = _settings()
        high_current["channels"]["ch1"].update(
            source_current="200mA", voltage_limit="30V"
        )
        with self.assertRaises(ModuleError) as voltage_error:
            backend.configure(high_current, context)
        self.assertEqual(voltage_error.exception.context, "ch1.voltage_limit")

        high_voltage = _settings()
        high_voltage["channels"]["ch1"].update(
            source_mode="voltage",
            source_voltage="25V",
            current_limit="200mA",
        )
        with self.assertRaises(ModuleError) as current_error:
            backend.configure(high_voltage, context)
        self.assertEqual(current_error.exception.context, "ch1.current_limit")

        none_enabled = _settings()
        none_enabled["channels"]["ch1"]["enabled"] = False
        none_enabled["channels"]["ch2"]["enabled"] = False
        with self.assertRaises(ModuleError) as channel_error:
            backend.configure(none_enabled, context)
        self.assertEqual(channel_error.exception.context, "channels")

        # 默认 2 s VISA timeout 下，两个 Enabled 通道仍应落在 120 s lifecycle budget 内。
        backend.configure(_settings(two_channels=True), context)

    def test_short_operation_timeout_rejects_before_connection(self) -> None:
        backend = self._backend(_FakeState())
        short = TestModuleAPI({}, lambda *_: None, None, None, 50.0)
        with self.assertRaises(ModuleError) as timeout_error:
            backend.configure(_settings(two_channels=True), short)
        self.assertEqual(timeout_error.exception.context, "operation_timeout_seconds")

    def test_close_is_idempotent_and_closes_once(self) -> None:
        state = _FakeState()
        backend = self._backend(state)
        context = self._context([])
        settings = _settings(two_channels=True)
        open_module(backend, context)
        backend.configure(settings, context)

        backend.close(context)
        backend.close(context)

        self.assertFalse(state.channels["smua"]["output"])
        self.assertFalse(state.channels["smub"]["output"])
        self.assertEqual(state.closed, 1)


class Keithley2614BFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manifest_and_nested_settings_round_trip(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.id, "keithley_2614b")
        self.assertEqual(
            list(Keithley2614BBackend.columns),
            [
                "R1",
                "Voltage1",
                "Current1",
                "StatusCode1",
                "R2",
                "Voltage2",
                "Current2",
                "StatusCode2",
            ],
        )
        self.assertEqual(manifest.columns, ())
        frontend = Keithley2614BFrontend(ModuleUIAPI())
        settings_page = frontend
        status_page = frontend.status_widget
        supplied = _settings(two_channels=True)
        supplied["channels"]["ch2"].update(
            source_voltage="5V",
            current_limit="500uA",
            sense_mode="4wire",
        )

        frontend.load(supplied)
        saved = frontend.dump()

        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)
        self.assertTrue(saved["channels"]["ch2"]["enabled"])
        self.assertEqual(saved["channels"]["ch2"]["source_mode"], "voltage")
        self.assertEqual(saved["channels"]["ch2"]["source_voltage"], "5")
        self.assertEqual(saved["channels"]["ch2"]["current_limit"], "500u")
        self.assertEqual(saved["channels"]["ch2"]["sense_mode"], "4wire")

    def test_resource_refresh_preserves_manual_address(self) -> None:
        frontend = Keithley2614BFrontend(ModuleUIAPI())
        settings_page = frontend
        status_page = frontend.status_widget
        frontend.resource.setCurrentText("GPIB9::26::INSTR")

        frontend.show_status(
            {"Available GPIB Resources": ["GPIB0::26::INSTR"]}
        )

        self.assertEqual(frontend.resource.currentText(), "GPIB9::26::INSTR")
        self.assertGreaterEqual(frontend.resource.findText("GPIB0::26::INSTR"), 0)
        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)

    def test_nested_settings_can_round_trip_through_core_toml(self) -> None:
        settings = _settings(two_channels=True)
        settings["channels"]["ch2"].update(
            source_mode="voltage",
            source_voltage="5V",
            current_limit="500uA",
            sense_mode="4wire",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)


if __name__ == "__main__":
    unittest.main()
