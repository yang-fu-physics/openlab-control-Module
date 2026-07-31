from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "keithley_6517b"
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.extensions.loading import load_source_object  # noqa: E402
from labcontrol.measurement.api import (  # noqa: E402
    ModuleError,
    ModuleOperationCancelled,
    ModuleOperationContext,
)
from labcontrol.measurement.frontend_api import (  # noqa: E402
    ModuleFrontendContext,
)
from labcontrol.measurement.manifest import load_manifest  # noqa: E402
from labcontrol.measurement.settings import (  # noqa: E402
    load_settings,
    save_settings,
)


Keithley6517BBackend = load_source_object(
    MODULE,
    "backend:Keithley6517BBackend",
    "test_keithley_6517b_backend",
)
Keithley6517BFrontend = load_source_object(
    MODULE,
    "frontend:Keithley6517BFrontend",
    "test_keithley_6517b_frontend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_keithley_6517b_constants",
)


class _FakeState:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.identity = "KEITHLEY INSTRUMENTS INC.,MODEL 6517B,1234567,1.0"
        self.output = False
        self.zero_check = True
        self.source_range = 100.0
        self.source_voltage = 0.0
        self.voltage_limit = 100.0
        self.voltage_limit_enabled = True
        # 模拟前面板遗留的 1 MΩ resistive limit；Apply 必须明确关闭。
        self.resistive_limit = True
        self.meter_connect = False
        self.sense_function = "CURR:DC"
        self.current_autorange = True
        self.nplc = 1.0
        self.elements = "READ,STAT,VSOUR"
        self.read_reply = "1e-9,N,100"
        self.current_compliance = False
        self.error_reply = '0,"No error"'
        self.failures: dict[str, int] = {}
        self.query_overrides: dict[str, str] = {}

    def factory(self, resource: str, timeout: float):
        self.opened.append((resource, timeout))
        return _FakeTransport(self)


class _FakeTransport:
    def __init__(self, state: _FakeState) -> None:
        self.state = state

    def write(self, command: str) -> None:
        self.state.commands.append(("write", command))
        self._fail(command)
        upper = command.upper()
        if upper == "*CLS":
            return
        if upper == "OUTP1 ON":
            self.state.output = True
            return
        if upper == "OUTP1 OFF":
            self.state.output = False
            return
        if upper == "SYST:ZCH ON":
            self.state.zero_check = True
            return
        if upper == "SYST:ZCH OFF":
            self.state.zero_check = False
            return
        if upper.startswith("SOUR:VOLT:RANG "):
            self.state.source_range = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SOUR:VOLT:LIM:STAT "):
            self.state.voltage_limit_enabled = upper.endswith(" ON")
            return
        if upper.startswith("SOUR:VOLT:LIM "):
            self.state.voltage_limit = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SOUR:CURR:RLIM:STAT "):
            self.state.resistive_limit = upper.endswith(" ON")
            return
        if upper.startswith("SOUR:VOLT:MCON "):
            self.state.meter_connect = upper.endswith(" ON")
            return
        if upper.startswith("SOUR:VOLT "):
            self.state.source_voltage = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SENS:FUNC "):
            self.state.sense_function = (
                command.split(" ", 1)[1].strip().strip("'").strip('"').upper()
            )
            return
        if upper.startswith("SENS:CURR:RANG:AUTO "):
            self.state.current_autorange = upper.endswith(" ON")
            return
        if upper.startswith("SENS:CURR:NPLC "):
            self.state.nplc = float(command.rsplit(" ", 1)[1])
            return
        if upper == "FORM:DATA ASC":
            return
        if upper.startswith("FORM:ELEM "):
            self.state.elements = command.split(" ", 1)[1].upper()
            return
        raise AssertionError(f"Unexpected write: {command}")

    def query(self, command: str) -> str:
        self.state.commands.append(("query", command))
        self._fail(command)
        if command in self.state.query_overrides:
            return self.state.query_overrides[command]
        replies = {
            "*IDN?": self.state.identity,
            "OUTP1?": "1" if self.state.output else "0",
            "SYST:ZCH?": "1" if self.state.zero_check else "0",
            "SOUR:VOLT:RANG?": f"{self.state.source_range:.12g}",
            "SOUR:VOLT?": f"{self.state.source_voltage:.12g}",
            "SOUR:VOLT:LIM?": f"{self.state.voltage_limit:.12g}",
            "SOUR:VOLT:LIM:STAT?": (
                "1" if self.state.voltage_limit_enabled else "0"
            ),
            "SOUR:CURR:RLIM:STAT?": (
                "1" if self.state.resistive_limit else "0"
            ),
            "SOUR:VOLT:MCON?": "1" if self.state.meter_connect else "0",
            "SENS:FUNC?": f'"{self.state.sense_function}"',
            "SENS:CURR:RANG:AUTO?": (
                "1" if self.state.current_autorange else "0"
            ),
            "SENS:CURR:NPLC?": f"{self.state.nplc:.12g}",
            "FORM:ELEM?": self.state.elements,
            "READ?": self.state.read_reply,
            "SOUR:CURR:LIM:STAT?": (
                "1" if self.state.current_compliance else "0"
            ),
            "SYST:ERR?": self.state.error_reply,
        }
        if command not in replies:
            raise AssertionError(f"Unexpected query: {command}")
        return replies[command]

    def _fail(self, command: str) -> None:
        remaining = self.state.failures.get(command, 0)
        if remaining > 0:
            self.state.failures[command] = remaining - 1
            raise OSError(f"simulated failure: {command}")

    def close(self) -> None:
        self.state.closed += 1
        self.state.commands.append(("close", ""))


def _settings(**updates) -> dict:
    result = default_settings()
    result["resource"] = "GPIB0::27::INSTR"
    result.update(updates)
    return result


class Keithley6517BBackendTests(unittest.TestCase):
    @staticmethod
    def _context(messages: list[tuple[str, dict]]) -> ModuleOperationContext:
        return ModuleOperationContext(
            {},
            lambda kind, values: messages.append((kind, values)),
            None,
            None,
            120.0,
        )

    @staticmethod
    def _backend(state: _FakeState, waits: list[float] | None = None):
        return Keithley6517BBackend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::27::INSTR",
                "GPIB0::8::INSTR",
            ),
            waiter=(
                (lambda _context, seconds: waits.append(seconds))
                if waits is not None
                else (lambda context, _seconds: context.checkpoint())
            ),
        )

    def test_initialize_discovers_without_connection_or_high_voltage_writes(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = backend.initialize(_settings(), self._context(messages))

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertEqual(status["Applied Settings"], "Not applied")

    def test_apply_sets_and_verifies_meter_connect_in_safe_state(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        settings = _settings(source_voltage="50V", voltage_limit="75V", nplc=2.0)
        backend.initialize(settings, context)

        status = backend.apply_settings(settings, context)

        self.assertTrue(state.meter_connect)
        self.assertFalse(state.resistive_limit)
        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        self.assertAlmostEqual(state.source_voltage, 50.0)
        self.assertAlmostEqual(state.voltage_limit, 75.0)
        self.assertEqual(status["METER-CONNECT"], "On")
        self.assertIn(("write", "SOUR:VOLT:MCON ON"), state.commands)
        self.assertIn(("query", "SOUR:VOLT:MCON?"), state.commands)
        self.assertIn(("write", "SOUR:CURR:RLIM:STAT OFF"), state.commands)
        self.assertIn(("query", "SOUR:CURR:RLIM:STAT?"), state.commands)

    def test_resistive_limit_changed_before_measure_prevents_operate(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(_settings(), context)
        backend.apply_settings(_settings(), context)
        backend.begin_sequence(context)
        state.resistive_limit = True

        with self.assertRaisesRegex(ModuleError, "resistive_current_limit"):
            backend.measure(context)

        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)

    def test_meter_connect_readback_failure_rejects_apply_and_closes(self) -> None:
        state = _FakeState()
        state.query_overrides["SOUR:VOLT:MCON?"] = "0"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(_settings(), context)

        with self.assertRaisesRegex(ModuleError, "METER-CONNECT"):
            backend.apply_settings(_settings(), context)

        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        self.assertEqual(state.closed, 1)

    def test_valid_measurement_emits_fvmi_resistance_after_safe_state(self) -> None:
        state = _FakeState()
        state.read_reply = "+1.0E-9A,N,+100.0V"
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waits)
        context = self._context(messages)
        settings = _settings(source_voltage="100", voltage_limit="100", settle_seconds=2.0)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        self.assertEqual(waits, [2.0])
        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"]["StatusCode"], 0)
        self.assertAlmostEqual(row["values"]["Resistance"], 1.0e11)
        self.assertAlmostEqual(row["values"]["Voltage"], 100.0)
        self.assertAlmostEqual(row["values"]["Current"], 1.0e-9)

    def test_current_compliance_writes_blank_status_row(self) -> None:
        state = _FakeState()
        state.current_compliance = True
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_voltage="100")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"], {"StatusCode": 2})
        self.assertTrue(any(kind == "warning" for kind, _ in messages))

    def test_output_can_be_retained_between_rows_but_end_restores_safe_state(self) -> None:
        state = _FakeState()
        state.read_reply = "+1.0E-9A,N,+100.0V"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(
            source_voltage="100",
            voltage_limit="100",
            output_off_between_measurements=False,
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)
        self.assertTrue(state.output)
        self.assertFalse(state.zero_check)
        backend.measure(context)
        self.assertTrue(state.output)
        self.assertFalse(state.zero_check)
        backend.end_sequence("completed", context)

        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        self.assertEqual(
            sum(kind == "row" for kind, _payload in messages),
            2,
        )

    def test_overflow_status_writes_blank_row_and_continues(self) -> None:
        state = _FakeState()
        state.read_reply = "9.9E37,O,100"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_voltage="100")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"], {"StatusCode": 1})
        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)

    def test_meter_connect_changed_before_measure_prevents_operate(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_voltage="10")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        state.meter_connect = False
        operate_count = sum(
            command == "OUTP1 ON"
            for kind, command in state.commands
            if kind == "write"
        )

        with self.assertRaisesRegex(ModuleError, "METER-CONNECT"):
            backend.measure(context)

        self.assertEqual(
            operate_count,
            sum(
                command == "OUTP1 ON"
                for kind, command in state.commands
                if kind == "write"
            ),
        )
        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)

    def test_cancel_during_settle_directly_restores_safe_state(self) -> None:
        state = _FakeState()

        def cancel(_context, _seconds):
            raise ModuleOperationCancelled("stop")

        backend = Keithley6517BBackend(
            transport_factory=state.factory,
            resource_lister=lambda: (),
            waiter=cancel,
        )
        messages: list[tuple[str, dict]] = []
        context = self._context(messages)
        settings = _settings(source_voltage="10")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        with self.assertRaises(ModuleOperationCancelled):
            backend.measure(context)

        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_unconfirmed_standby_is_fatal_and_no_row_is_written(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_voltage="10")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()
        state.failures["READ?"] = 1
        state.failures["OUTP1 OFF"] = 1
        state.query_overrides["OUTP1?"] = "1"

        with self.assertRaisesRegex(ModuleError, "could not be confirmed"):
            backend.measure(context)

        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_identity_mismatch_and_instrument_error_close_transport(self) -> None:
        state = _FakeState()
        state.identity = "KEITHLEY,MODEL 6514,0,1"
        backend = self._backend(state)
        context = self._context([])
        backend.initialize(_settings(), context)
        with self.assertRaisesRegex(ModuleError, "Expected Keithley Model 6517B"):
            backend.apply_settings(_settings(), context)
        self.assertEqual(state.closed, 1)

        state2 = _FakeState()
        state2.error_reply = '-222,"Data out of range"'
        backend2 = self._backend(state2)
        backend2.initialize(_settings(), context)
        with self.assertRaisesRegex(ModuleError, "instrument error"):
            backend2.apply_settings(_settings(), context)
        self.assertFalse(state2.output)
        self.assertTrue(state2.zero_check)
        self.assertEqual(state2.closed, 1)

    def test_voltage_range_limit_and_operation_budget_are_validated(self) -> None:
        backend = self._backend(_FakeState())
        context = self._context([])
        with self.assertRaises(ModuleError) as range_error:
            backend.initialize(_settings(source_voltage="101V"), context)
        self.assertEqual(range_error.exception.context, "source_voltage")
        with self.assertRaises(ModuleError) as limit_error:
            backend.initialize(
                _settings(source_voltage="50V", voltage_limit="40V"),
                context,
            )
        self.assertEqual(limit_error.exception.context, "voltage_limit")

        accepted = _settings(
            source_range="1000v",
            source_voltage="600V",
            voltage_limit="700V",
        )
        backend.initialize(accepted, context)

        short = ModuleOperationContext({}, lambda *_: None, None, None, 20.0)
        with self.assertRaises(ModuleError) as timeout_error:
            backend.initialize(_settings(io_timeout_seconds=1.0), short)
        self.assertEqual(timeout_error.exception.context, "operation_timeout_seconds")

    def test_abort_is_idempotent_and_keeps_confirmed_safe_state(self) -> None:
        state = _FakeState()
        backend = self._backend(state)
        context = self._context([])
        backend.initialize(_settings(), context)
        backend.apply_settings(_settings(), context)

        backend.abort(context)
        backend.abort(context)

        self.assertFalse(state.output)
        self.assertTrue(state.zero_check)
        self.assertEqual(state.closed, 1)


class Keithley6517BFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manifest_and_frontend_round_trip(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.id, "keithley_6517b")
        self.assertEqual(
            [column.name for column in manifest.columns],
            ["Resistance", "Voltage", "Current", "StatusCode"],
        )
        frontend = Keithley6517BFrontend(ModuleFrontendContext())
        settings_page = frontend.create_settings_page()
        status_page = frontend.create_status_page()
        supplied = _settings(
            source_range="1000v",
            source_voltage="500V",
            voltage_limit="750V",
            nplc=3.0,
            settle_seconds=5.0,
        )

        frontend.load_settings(supplied)
        saved = frontend.settings()

        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)
        self.assertEqual(saved["source_range"], "1000v")
        self.assertEqual(saved["source_voltage"], "500")
        self.assertEqual(saved["voltage_limit"], "750")
        self.assertAlmostEqual(saved["settle_seconds"], 5.0)

    def test_resource_refresh_preserves_manual_address(self) -> None:
        frontend = Keithley6517BFrontend(ModuleFrontendContext())
        settings_page = frontend.create_settings_page()
        status_page = frontend.create_status_page()
        frontend.resource.setCurrentText("GPIB9::27::INSTR")

        frontend.update_status(
            {"Available GPIB Resources": ["GPIB0::27::INSTR"]}
        )

        self.assertEqual(frontend.resource.currentText(), "GPIB9::27::INSTR")
        self.assertGreaterEqual(frontend.resource.findText("GPIB0::27::INSTR"), 0)
        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)

    def test_settings_can_round_trip_through_core_toml(self) -> None:
        settings = _settings(
            source_range="1000v",
            source_voltage="500V",
            voltage_limit="750V",
            settle_seconds=5.0,
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)


if __name__ == "__main__":
    unittest.main()
