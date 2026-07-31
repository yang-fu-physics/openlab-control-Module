from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "keithley_2400"
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


Keithley2400Backend = load_source_object(
    MODULE,
    "backend:Keithley2400Backend",
    "test_keithley_2400_backend",
)
Keithley2400Frontend = load_source_object(
    MODULE,
    "frontend:Keithley2400Frontend",
    "test_keithley_2400_frontend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_keithley_2400_constants",
)


class _FakeState:
    """用 2400 SCPI 文本模拟实际读回，避免测试绕过协议解析。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C32"
        self.output = False
        self.source_func = "CURR"
        self.source_current = 0.0
        self.source_voltage = 0.0
        self.voltage_compliance = 10.0
        self.current_compliance = 1.0e-3
        self.concurrent_measurements = False
        # 模拟前面板遗留 RES；Apply 必须把它从精确的 V/I 方案中移除。
        self.measurement_functions = {"RES"}
        self.remote_sense = False
        self.nplc_voltage = 1.0
        self.nplc_current = 1.0
        self.elements = "VOLT,CURR"
        self.read_reply = "2.0,0.001"
        self.voltage_trip = False
        self.current_trip = False
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
        if upper == "OUTP ON":
            self.state.output = True
            return
        if upper == "OUTP OFF":
            self.state.output = False
            return
        if upper == "SENS:FUNC:CONC ON":
            self.state.concurrent_measurements = True
            return
        if upper == "SENS:FUNC:CONC OFF":
            self.state.concurrent_measurements = False
            self.state.measurement_functions = {"VOLT:DC"}
            return
        if upper == "SENS:FUNC:OFF:ALL":
            self.state.measurement_functions.clear()
            return
        if upper.startswith("SOUR:FUNC "):
            self.state.source_func = upper.rsplit(" ", 1)[1]
            return
        if upper in {
            "SOUR:CURR:MODE FIX",
            "SOUR:VOLT:MODE FIX",
            "SOUR:CURR:RANG:AUTO ON",
            "SOUR:VOLT:RANG:AUTO ON",
            "SENS:VOLT:RANG:AUTO ON",
            "SENS:CURR:RANG:AUTO ON",
            "FORM:DATA ASC",
        }:
            return
        if upper.startswith("SOUR:CURR:LEV "):
            self.state.source_current = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SOUR:VOLT:LEV "):
            self.state.source_voltage = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SENS:VOLT:PROT "):
            self.state.voltage_compliance = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SENS:CURR:PROT "):
            self.state.current_compliance = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SENS:FUNC:ON "):
            raw_functions = command.split(" ", 1)[1]
            self.state.measurement_functions = {
                item.strip().strip("'").strip('"').upper()
                for item in raw_functions.split(",")
            }
            return
        if upper.startswith("SENS:VOLT:NPLC "):
            self.state.nplc_voltage = float(command.rsplit(" ", 1)[1])
            return
        if upper.startswith("SENS:CURR:NPLC "):
            self.state.nplc_current = float(command.rsplit(" ", 1)[1])
            return
        if upper == "SYST:RSEN ON":
            self.state.remote_sense = True
            return
        if upper == "SYST:RSEN OFF":
            self.state.remote_sense = False
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
            "OUTP?": "1" if self.state.output else "0",
            "SOUR:FUNC?": self.state.source_func,
            "SOUR:CURR:LEV?": f"{self.state.source_current:.12g}",
            "SOUR:VOLT:LEV?": f"{self.state.source_voltage:.12g}",
            "SENS:VOLT:PROT?": f"{self.state.voltage_compliance:.12g}",
            "SENS:CURR:PROT?": f"{self.state.current_compliance:.12g}",
            "SENS:FUNC:CONC?": (
                "1" if self.state.concurrent_measurements else "0"
            ),
            "SENS:FUNC:ON?": ",".join(
                f'"{item}"' for item in sorted(self.state.measurement_functions)
            ),
            "SENS:VOLT:NPLC?": f"{self.state.nplc_voltage:.12g}",
            "SENS:CURR:NPLC?": f"{self.state.nplc_current:.12g}",
            "SYST:RSEN?": "1" if self.state.remote_sense else "0",
            "FORM:ELEM?": self.state.elements,
            "READ?": self.state.read_reply,
            "SENS:VOLT:PROT:TRIP?": "1" if self.state.voltage_trip else "0",
            "SENS:CURR:PROT:TRIP?": "1" if self.state.current_trip else "0",
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
    values = default_settings()
    values["resource"] = "GPIB0::24::INSTR"
    values.update(updates)
    return values


class Keithley2400BackendTests(unittest.TestCase):
    @staticmethod
    def _context(
        messages: list[tuple[str, dict]],
        operation_state=None,
    ) -> ModuleOperationContext:
        return ModuleOperationContext(
            {},
            lambda kind, values: messages.append((kind, values)),
            None,
            operation_state,
            120.0,
        )

    @staticmethod
    def _backend(
        state: _FakeState,
        waits: list[float] | None = None,
    ):
        return Keithley2400Backend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::24::INSTR",
                "GPIB0::7::INSTR",
            ),
            waiter=(
                (lambda _context, seconds: waits.append(seconds))
                if waits is not None
                else (lambda context, _seconds: context.checkpoint())
            ),
        )

    def test_initialize_only_discovers_resources(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = backend.initialize(_settings(), self._context(messages))

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertEqual(status["Applied Settings"], "Not applied")
        self.assertEqual(
            status["Available GPIB Resources"],
            ["GPIB0::24::INSTR", "GPIB0::7::INSTR"],
        )

    def test_apply_constant_current_four_wire_reads_back_and_stays_off(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        settings = _settings(
            source_mode="current",
            source_current="1m",
            voltage_compliance="20",
            sense_mode="4wire",
            nplc=2.0,
        )

        backend.initialize(settings, self._context(messages))
        status = backend.apply_settings(settings, self._context(messages))

        self.assertFalse(state.output)
        self.assertEqual(state.source_func, "CURR")
        self.assertAlmostEqual(state.source_current, 1.0e-3)
        self.assertAlmostEqual(state.voltage_compliance, 20.0)
        self.assertTrue(state.remote_sense)
        self.assertTrue(state.concurrent_measurements)
        self.assertEqual(
            state.measurement_functions,
            {"VOLT:DC", "CURR:DC"},
        )
        self.assertAlmostEqual(state.nplc_voltage, 2.0)
        self.assertAlmostEqual(state.nplc_current, 2.0)
        self.assertEqual(status["Applied Settings"], "Applied")
        source_write = state.commands.index(("write", "SOUR:FUNC CURR"))
        first_off = state.commands.index(("write", "OUTP OFF"))
        self.assertLess(first_off, source_write)

    def test_measure_emits_valid_resistance_after_output_is_confirmed_off(self) -> None:
        state = _FakeState()
        state.read_reply = "2.0,0.001"
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waits)
        context = self._context(messages)
        settings = _settings(source_current="1m", settle_seconds=0.25)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        self.assertEqual(waits, [0.25])
        self.assertFalse(state.output)
        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"]["StatusCode"], 0)
        self.assertAlmostEqual(row["values"]["Resistance"], 2000.0)
        self.assertAlmostEqual(row["values"]["Voltage"], 2.0)
        self.assertAlmostEqual(row["values"]["Current"], 1.0e-3)
        row_index = next(
            index for index, item in enumerate(messages) if item[0] == "row"
        )
        self.assertGreaterEqual(row_index, 0)
        self.assertEqual(
            [command for kind, command in state.commands if kind == "write"][-1],
            "OUTP OFF",
        )

    def test_voltage_mode_compliance_writes_blank_status_row_and_warning(self) -> None:
        state = _FakeState()
        state.read_reply = "1.0,0.001"
        state.current_trip = True
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(
            source_mode="voltage",
            source_voltage="1",
            current_compliance="1m",
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"], {"StatusCode": 2})
        self.assertTrue(any(kind == "warning" for kind, _ in messages))
        self.assertFalse(state.output)

    def test_output_can_be_retained_between_rows_but_end_is_always_off(self) -> None:
        state = _FakeState()
        state.read_reply = "2.0,0.001"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(
            source_current="1m",
            output_off_between_measurements=False,
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)
        self.assertTrue(state.output)
        backend.measure(context)
        self.assertTrue(state.output)
        backend.end_sequence("completed", context)

        self.assertFalse(state.output)
        self.assertEqual(
            sum(kind == "row" for kind, _payload in messages),
            2,
        )

    def test_overrange_sentinel_is_data_warning_not_framework_error(self) -> None:
        state = _FakeState()
        state.read_reply = "9.91e37,0.001"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_current="1m")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"], {"StatusCode": 1})
        self.assertFalse(any(kind == "error" for kind, _ in messages))

    def test_zero_current_is_invalid_blank_row(self) -> None:
        state = _FakeState()
        state.read_reply = "1.0,0.0"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_voltage="1", source_mode="voltage")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        messages.clear()

        backend.measure(context)

        row = next(payload for kind, payload in messages if kind == "row")
        self.assertEqual(row["values"], {"StatusCode": 3})

    def test_cancel_during_settle_turns_output_off_and_propagates_cancel(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []

        def cancel(_context, _seconds):
            raise ModuleOperationCancelled("stop")

        backend = Keithley2400Backend(
            transport_factory=state.factory,
            resource_lister=lambda: (),
            waiter=cancel,
        )
        context = self._context(messages)
        settings = _settings(source_current="1m")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        with self.assertRaises(ModuleOperationCancelled):
            backend.measure(context)

        self.assertFalse(state.output)
        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_measurement_io_failure_that_cannot_safe_off_is_fatal(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_current="1m")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        state.failures["READ?"] = 1
        state.failures["OUTP OFF"] = 1
        state.query_overrides["OUTP?"] = "1"

        with self.assertRaisesRegex(ModuleError, "output-off could not be confirmed"):
            backend.measure(context)

        self.assertFalse(any(kind == "row" for kind, _ in messages))

    def test_front_panel_setting_change_is_detected_before_output_on(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        context = self._context(messages)
        settings = _settings(source_current="1m")
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        state.source_current = 2.0e-3
        output_on_before = sum(
            command == "OUTP ON" for kind, command in state.commands if kind == "write"
        )

        with self.assertRaisesRegex(ModuleError, "readback mismatch"):
            backend.measure(context)

        output_on_after = sum(
            command == "OUTP ON" for kind, command in state.commands if kind == "write"
        )
        self.assertEqual(output_on_before, output_on_after)
        self.assertFalse(state.output)

    def test_identity_mismatch_closes_transport(self) -> None:
        state = _FakeState()
        state.identity = "ACME,MODEL 123,0,1"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(_settings(), context)

        with self.assertRaisesRegex(ModuleError, "Expected Keithley Model 2400"):
            backend.apply_settings(_settings(), context)

        self.assertEqual(state.closed, 1)
        self.assertIsNone(backend.transport)

    def test_instrument_error_queue_rejects_apply(self) -> None:
        state = _FakeState()
        state.error_reply = '-222,"Data out of range"'
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(_settings(), context)

        with self.assertRaisesRegex(ModuleError, "instrument error"):
            backend.apply_settings(_settings(), context)

        self.assertFalse(state.output)
        self.assertEqual(state.closed, 1)

    def test_abort_is_idempotent_and_closes_transport(self) -> None:
        state = _FakeState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(_settings(), context)
        backend.apply_settings(_settings(), context)

        backend.abort(context)
        backend.abort(context)

        self.assertFalse(state.output)
        self.assertEqual(state.closed, 1)
        self.assertIsNone(backend.transport)

    def test_device_limits_and_operation_budget_are_validated(self) -> None:
        state = _FakeState()
        backend = self._backend(state)
        context = self._context([])
        with self.assertRaises(ModuleError) as current_error:
            backend.initialize(_settings(source_current="2A"), context)
        self.assertEqual(current_error.exception.context, "source_current")

        short_context = ModuleOperationContext({}, lambda *_: None, None, None, 10.0)
        with self.assertRaises(ModuleError) as timeout_error:
            backend.initialize(
                _settings(settle_seconds=9.0, io_timeout_seconds=1.0),
                short_context,
            )
        self.assertEqual(timeout_error.exception.context, "operation_timeout_seconds")


class Keithley2400FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manifest_and_frontend_settings_round_trip(self) -> None:
        manifest = load_manifest(MODULE)
        self.assertEqual(manifest.id, "keithley_2400")
        self.assertEqual(
            [column.name for column in manifest.columns],
            ["Resistance", "Voltage", "Current", "StatusCode"],
        )
        frontend = Keithley2400Frontend(ModuleFrontendContext())
        settings_page = frontend.create_settings_page()
        status_page = frontend.create_status_page()
        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)
        supplied = _settings(
            source_mode="voltage",
            source_voltage="2V",
            current_compliance="500uA",
            sense_mode="4wire",
            nplc=3.0,
            settle_seconds=0.75,
        )

        frontend.load_settings(supplied)
        saved = frontend.settings()

        self.assertEqual(saved["source_mode"], "voltage")
        self.assertAlmostEqual(
            float(
                load_source_object(
                    MODULE,
                    "quantities:parse_quantity",
                    "test_keithley_2400_quantities",
                )(saved["current_compliance"], expected_unit="A")
            ),
            500.0e-6,
        )
        self.assertEqual(saved["sense_mode"], "4wire")
        self.assertFalse(frontend.source_current.isEnabled())
        self.assertTrue(frontend.source_voltage.isEnabled())

    def test_status_resource_refresh_preserves_manual_resource(self) -> None:
        frontend = Keithley2400Frontend(ModuleFrontendContext())
        settings_page = frontend.create_settings_page()
        status_page = frontend.create_status_page()
        frontend.resource.setCurrentText("GPIB9::24::INSTR")

        frontend.update_status(
            {"Available GPIB Resources": ["GPIB0::24::INSTR"]}
        )

        self.assertEqual(frontend.resource.currentText(), "GPIB9::24::INSTR")
        self.assertGreaterEqual(frontend.resource.findText("GPIB0::24::INSTR"), 0)
        self.assertIsInstance(settings_page, QWidget)
        self.assertIsInstance(status_page, QWidget)

    def test_settings_can_round_trip_through_core_toml(self) -> None:
        settings = _settings(
            source_mode="voltage",
            source_voltage="2V",
            current_compliance="500uA",
            sense_mode="4wire",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)


if __name__ == "__main__":
    unittest.main()
