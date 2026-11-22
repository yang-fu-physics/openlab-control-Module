import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "OpenLabControl" / "src"))

from PySide6.QtWidgets import QApplication, QTabWidget
from labcontrol.measurement.frontend_api import ModuleUIAPI
from labcontrol.module_api import ModuleAPI, ModuleError
from labcontrol.module_commands import normalize_module_commands
from labcontrol.package_support.loading import load_source_object


PATH = ROOT / "modules" / "oe1022d_lockin"
Module = load_source_object(PATH, "backend:Module", "test_oe_lockin")
settings = load_source_object(PATH, "backend:default_settings", "test_oe_lockin")
Frontend = load_source_object(PATH, "frontend:Frontend", "test_oe_lockin")
SOURCE = ROOT / "modules" / "keithley_6221_ac"
SourceModule = load_source_object(SOURCE, "backend:Module", "test_6221_ac")
source_settings = load_source_object(SOURCE, "backend:default_settings", "test_6221_ac")
SourceFrontend = load_source_object(SOURCE, "frontend:Frontend", "test_6221_ac")
RESOURCES = {
    "lock": {"address": "ASRL7::INSTR", "purpose": "measurement"},
    "source": {"address": "GPIB0::10::INSTR", "purpose": "measurement"},
}


class Instrument:
    def __init__(self, lockin):
        self.lockin = lockin
        self.writes = []
        self.queries = []
        self.values = {
            "*IDND?": "SSI LIA-OE1022D,SN001,Ver1.00", "*IDN?": "KEITHLEY,MODEL 6221,001,D03",
            "OUTP?": "0", "STAT:MEAS:COND?": "0",
        }
        for i in (1, 2):
            self.values.update({
                f"INOVD? {i}": "0", f"GNOVD? {i}": "0", f"*PLLD? {i}": "1",
                f"SNAPD? {i},5,6,7,8,4": "0.002,-0.001,0.002236,0,137.77",
                f"SNAPD? {i},9,10,11,12,4": "0.0001,0.00002,0.000102,10,137.77",
            })
        self.closed = False

    def write(self, command):
        self.writes.append(command)
        if command in ("SOUR:WAVE:ABOR", "OUTP OFF", "SOUR:WAVE:INIT"):
            self.values["OUTP?"] = "1" if command.endswith("INIT") else "0"
        elif " " in command:
            prefix, args = command.split(" ", 1)
            if self.lockin:
                selector, value = args.rsplit(",", 1)
                self.values[f"{prefix}? {selector}"] = value
            else:
                self.values[f"{prefix}?"] = args

    def query(self, command):
        self.queries.append(command)
        return self.values[command]

    def close(self):
        self.closed = True


class LockInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.lockin = Instrument(True)
        self.source = Instrument(False)
        manager = Mock()
        manager.open_resource.side_effect = lambda address, **_: (
            self.lockin if address.startswith("ASRL") else self.source
        )
        self.manager = manager
        self.enterContext(patch("pyvisa.ResourceManager", return_value=manager))
        self.events = []
        self.api = ModuleAPI({}, lambda *event: self.events.append(event), _instrument_resources=RESOURCES)
        self.module = Module()
        self.addCleanup(self.module.close, self.api)
        self.current_module = SourceModule()
        self.addCleanup(self.current_module.close, self.api)
        self.values = settings() | {
            "lockin_resource": "lock", "max_voltage": "100m", "settle_seconds": 0.0,
        }
        self.source_values = source_settings() | {
            "resource": "source", "max_current": "1m", "ramp_step": "50u", "ramp_interval": 0.1,
        }

    def start(self):
        self.module.configure(self.values, self.api)
        self.module.on_event("run_start", {}, self.api)

    def start_current(self):
        self.current_module.configure(self.source_values, self.api)
        self.current_module.on_event("run_start", {}, self.api)

    def scan(self, mode, value):
        module = self.module if mode == "voltage" else self.current_module
        with patch.object(ModuleAPI, "sleep", autospec=True):
            module.execute_sequence_command("scan_" + mode, {mode: value, "settle_seconds": 0}, self.api)

    def test_enable_is_read_only_and_commands_are_valid(self):
        self.module.open(self.api)
        self.current_module.open(self.api)
        self.manager.open_resource.assert_not_called()
        commands = normalize_module_commands("oe1022d_lockin", self.module.sequence_commands)
        self.assertEqual([item.command_id for item in commands], ["scan_voltage", "auto_range"])
        commands = normalize_module_commands("keithley_6221_ac", self.current_module.sequence_commands)
        self.assertEqual([item.command_id for item in commands], ["scan_current", "scan_frequency", "set_current"])

    def test_signed_current_has_positive_peak_and_changes_only_marker(self):
        self.start()
        self.start_current()
        self.assertNotIn("SOUR:WAVE:INIT", self.source.writes)
        self.assertEqual([self.lockin.values[f"FMODD? {i}"] for i in (1, 2)], ["0", "0"])
        for value, phase in (("100u", 0), ("-100u", 180)):
            self.scan("current", value)
            self.assertTrue(math.isclose(float(self.source.values["SOUR:WAVE:AMPL?"]), 1e-4 * math.sqrt(2)))
            self.assertEqual(self.source.values["SOUR:WAVE:OFFS?"], "0")
            self.assertEqual(int(self.source.values["SOUR:WAVE:PMAR?"]), phase)
            reads_from = len(self.source.writes)
            row = self.current_module.measure(1, self.api)
            self.assertEqual(len(self.source.writes), reads_from)
            self.assertEqual(row["Marker Phase"], phase)
            self.assertAlmostEqual(row["Current Magnitude"], 1e-4)
            self.assertAlmostEqual(row["Source Current"], 1e-4 if phase == 0 else -1e-4)
            self.assertTrue(row.keys() <= self.current_module.columns.keys())
            lockin_row = self.module.measure(1, self.api)
            self.assertEqual(lockin_row["Frequency2s"], 275.54)
            self.assertAlmostEqual(lockin_row["Y2s/X1p^2"], 5)
            self.assertTrue(lockin_row.keys() <= self.module.columns.keys())
            self.assertTrue(row.keys().isdisjoint(lockin_row))
        self.assertEqual(self.source.writes.count("SOUR:WAVE:INIT"), 1)
        self.scan("current", "0")
        row = self.current_module.measure(1, self.api)
        self.assertEqual(row["Source Current"], 0)
        self.assertEqual(self.module.measure(1, self.api)["X1p"], 0.002)
        self.current_module.on_event("run_end", {"reason": "completed"}, self.api)
        self.assertEqual(self.source.values["OUTP?"], "0")

    def test_voltage_standalone_uses_selected_sine_out_and_goes_to_dc_zero(self):
        self.values.update(mode="sine_out", sine_channel=2)
        self.start()
        self.assertEqual(self.manager.open_resource.call_count, 1)
        self.assertEqual(self.lockin.values["FMODD? 2"], "1")
        self.assertEqual(self.lockin.values["FMODD? 1"], "0")
        self.assertEqual(self.lockin.writes[:2], ["SVDCD 2,0", "SWVTD 2,3"])
        self.scan("voltage", "10mV")
        self.assertEqual(self.lockin.writes[-2:], ["SLVLD 2,0.01", "SWVTD 2,0"])
        row = self.module.measure(1, self.api)
        self.assertEqual(row["Source Voltage"], 0.01)
        self.assertNotIn("Source Current", row)
        self.assertNotIn("X1p Resistance", row)
        self.module.on_event("run_end", {"reason": "stopped"}, self.api)
        self.assertEqual(self.lockin.writes[-2:], ["SVDCD 2,0", "SWVTD 2,3"])

    def test_invalid_or_wrong_mode_points_do_not_write(self):
        self.start()
        self.start_current()
        before = self.source.writes[:]
        for mode, value in (("current", "2m"), ("current", "3mV"), ("voltage", "10m")):
            with self.assertRaises(ModuleError):
                self.scan(mode, value)
        self.assertEqual(self.source.writes, before)

    def test_overload_warns_and_keeps_raw_data_but_no_derived_values(self):
        self.start()
        self.start_current()
        self.scan("current", "100u")
        self.lockin.values["INOVD? 2"] = "1"
        self.lockin.values["*PLLD? 2"] = "0"
        self.source.values["STAT:MEAS:COND?"] = "8"
        row = self.module.measure(1, self.api)
        self.assertEqual(self.current_module.measure(1, self.api)["6221 Compliance"], 1)
        self.assertEqual(row["Status2"], 18)
        self.assertIn("Y2s", row)
        self.assertNotIn("Y2s/X1p^2", row)
        self.assertNotIn("X1p Resistance", row)
        self.assertTrue(any(kind == "warning" for kind, _ in self.events))

    def test_measurement_records_harmonic_reported_after_external_frequency_changes(self):
        self.start()
        self.lockin.values["HARMD? 2,2"] = "1"
        self.lockin.values["SNAPD? 2,9,10,11,12,4"] = "0.0001,0.00002,0.000102,10,60000"

        row = self.module.measure(1, self.api)

        self.assertEqual(row["Harmonic2s"], 1)
        self.assertEqual(row["Frequency2s"], 60000)
        self.assertEqual(row["Y2s"], 0.00002)
        self.assertNotIn("Y2s/X1p^2", row)
        self.assertTrue(any(kind == "warning" for kind, _ in self.events))
        self.assertEqual(row["Harmonic1s"], 2)

    def test_failed_and_cancelled_source_operations_go_safe(self):
        self.start()
        self.start_current()
        with patch.object(ModuleAPI, "sleep", side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):
                self.current_module.execute_sequence_command("scan_current", {"current": "100u", "settle_seconds": 0}, self.api)
        self.assertEqual(self.source.values["OUTP?"], "0")
        self.assertEqual(self.current_module.point, 0)
        self.scan("current", "100u")
        with patch.object(self.source, "query", return_value="1"):
            with self.assertRaisesRegex(ModuleError, "off could not be confirmed"):
                self.current_module.close(self.api)
        self.assertFalse(self.lockin.closed)
        self.assertTrue(self.source.closed)

    def test_frontend_has_source_and_channel_pages_and_roundtrips(self):
        widget = Frontend(ModuleUIAPI(resources=RESOURCES))
        self.addCleanup(widget.close)
        widget.load(self.values)
        tabs = widget.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], ["Source", "Channel 1 (A)", "Channel 2 (B)"])
        self.assertEqual(widget.dump(), self.values)
        source_widget = SourceFrontend(ModuleUIAPI(resources=RESOURCES))
        self.addCleanup(source_widget.close)
        source_widget.load(self.source_values)
        self.assertEqual(source_widget.dump(), self.source_values)

    def test_current_changes_without_rearming_and_with_bounded_steps(self):
        self.start_current()
        self.assertEqual(self.manager.open_resource.call_count, 1)
        self.scan("current", "100u")
        before = len(self.source.writes)
        self.scan("current", "225u")
        writes = self.source.writes[before:]
        self.assertTrue(all(item.startswith("SOUR:WAVE:AMPL ") for item in writes))
        points = [100e-6] + [float(item.split()[-1]) / math.sqrt(2) for item in writes]
        self.assertAlmostEqual(points[-1], 225e-6)
        self.assertEqual(len(points), 4)
        self.assertTrue(all(0 < b - a < 50.0001e-6 for a, b in zip(points, points[1:])))
        before = len(self.source.writes)
        with patch.object(ModuleAPI, "sleep", autospec=True):
            self.current_module.on_event("run_end", {"reason": "completed"}, self.api)
        self.assertTrue(self.source.writes[before].startswith("SOUR:WAVE:AMPL "))
        self.assertEqual(self.source.writes[-2:], ["SOUR:WAVE:ABOR", "OUTP OFF"])

    def test_live_frequency_scan_keeps_current_and_phase_and_does_not_start_output(self):
        self.start_current()
        before = len(self.source.writes)
        self.scan("frequency", "137.771Hz")
        self.assertEqual(self.source.writes[before:], [])
        self.scan("current", "-100u")
        self.assertEqual(self.source.values["SOUR:WAVE:FREQ?"], "137.771")
        before = len(self.source.writes)
        self.scan("frequency", "1kHz")
        self.assertEqual(self.source.writes[before:], ["SOUR:WAVE:FREQ 1000"])
        row = self.current_module.measure(1, self.api)
        self.assertEqual(row["Source Frequency"], 1000)
        self.assertEqual(row["Marker Phase"], 180)
        self.assertAlmostEqual(row["Source Current"], -1e-4)
        before = len(self.source.writes)
        for value in ("137.7712Hz", "101kHz", "100uA"):
            with self.assertRaises(ModuleError):
                self.scan("frequency", value)
        self.assertEqual(self.source.writes[before:], [])

    def test_amplitude_readback_failure_forces_safe_off_without_restart(self):
        self.start_current()
        self.scan("current", "100u")
        original = self.source.query
        before = len(self.source.writes)

        def query(command):
            if command == "SOUR:WAVE:AMPL?" and len(self.source.writes) > before:
                return "0"
            return original(command)

        with patch.object(self.source, "query", side_effect=query):
            with self.assertRaisesRegex(ModuleError, "expected.*got.*amplitude before ramp-down"):
                self.scan("current", "200u")
        self.assertNotIn("SOUR:WAVE:INIT", self.source.writes[before:])
        self.assertEqual(self.source.values["OUTP?"], "0")

    def test_set_current_holds_rejects_below_range_floor_and_ramps_to_zero(self):
        self.start_current()
        with patch.object(ModuleAPI, "sleep", autospec=True):
            status = self.current_module.execute_sequence_command(
                "set_current", {"current": "100u", "settle_seconds": 0.0}, self.api
            )
        self.assertAlmostEqual(status["Set RMS Current"], 1e-4)
        self.assertEqual(self.source.values["OUTP?"], "1")
        with self.assertRaisesRegex(ModuleError, "minimum waveform amplitude"):
            self.current_module.execute_sequence_command(
                "set_current", {"current": "1u", "settle_seconds": 0.0}, self.api
            )
        before = len(self.source.writes)
        with patch.object(ModuleAPI, "sleep", autospec=True):
            status = self.current_module.execute_sequence_command(
                "set_current", {"current": "0", "settle_seconds": 0.0}, self.api
            )
        self.assertEqual(status["Set RMS Current"], 0.0)
        self.assertTrue(self.source.writes[before].startswith("SOUR:WAVE:AMPL "))
        self.assertEqual(self.source.writes[-2:], ["SOUR:WAVE:ABOR", "OUTP OFF"])
        self.assertEqual(self.source.values["OUTP?"], "0")

    def test_ramp_off_respects_timeout_budget_and_readback_boundary_tolerance(self):
        self.source_values.update(max_current="1m", ramp_step="10u", ramp_interval=0.8, io_timeout=0.1)
        self.start_current()
        # Forward 0 -> 1 mA + settle 8 s + interrupted ramp-down 1 mA -> min exceeds 120 s.
        with self.assertRaisesRegex(ModuleError, "operation budget"):
            self.current_module.execute_sequence_command(
                "set_current", {"current": "1m", "settle_seconds": 8.0}, self.api
            )
        self.assertEqual(self.source.values["OUTP?"], "0")

        self.source_values.update(max_current="1m", ramp_step="20u", ramp_interval=0.5, io_timeout=0.1)
        self.start_current()
        with patch.object(ModuleAPI, "sleep", autospec=True):
            self.current_module.execute_sequence_command(
                "set_current", {"current": "1m", "settle_seconds": 0.0}, self.api
            )
        short_events = []
        short_api = ModuleAPI(
            {},
            lambda *event: short_events.append(event),
            _operation_timeout_seconds=3.0,
            _instrument_resources=RESOURCES,
        )
        sleep_calls = []
        clock = [100.0]

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            clock[0] += seconds

        with (
            patch.object(SourceModule, "_active_now", side_effect=lambda _api: clock[0]),
            patch.object(SourceModule, "_sleep", side_effect=fake_sleep),
        ):
            self.current_module.close(short_api)
        self.assertGreater(len(sleep_calls), 0)
        self.assertLess(sum(sleep_calls), 3.0)
        self.assertEqual(self.source.values["OUTP?"], "0")
        self.assertTrue(
            any(
                kind == "warning" and payload.get("code") == "K6221_AC_RAMP_TRUNCATED"
                for kind, payload in short_events
            )
        )

        # Amplitude readback within 5e-6 of MAX_PEAK must still ramp down cleanly.
        self.source_values.update(max_current="70.71m", ramp_step="35m", ramp_interval=0.1, io_timeout=0.1)
        self.start_current()
        with patch.object(ModuleAPI, "sleep", autospec=True):
            self.current_module.execute_sequence_command(
                "set_current", {"current": "70.71m", "settle_seconds": 0.0}, self.api
            )
        self.source.values["SOUR:WAVE:AMPL?"] = "0.1000001"
        before = len(self.source.writes)
        with patch.object(SourceModule, "_sleep"):
            self.current_module.on_event("run_end", {"reason": "completed"}, self.api)
        self.assertTrue(any(w.startswith("SOUR:WAVE:AMPL ") for w in self.source.writes[before:]))
        self.assertEqual(self.source.values["OUTP?"], "0")

    def test_auto_range_channel_1_unconverged_still_ranges_channel_2(self):
        for channel in ("channel_1", "channel_2"):
            self.values[channel]["time_constant"] = 4  # 1 ms
            self.values[channel]["slope"] = 0
        self.start()
        self.lockin.values["SNAPD? 1,5,6,7,8,4"] = "0,0,0,0,137.77"
        self.lockin.values["SNAPD? 1,9,10,11,12,4"] = "0,0,0,0,137.77"
        self.lockin.values["SNAPD? 2,5,6,7,8,4"] = "1e-5,0,1e-5,0,137.77"
        self.lockin.values["SNAPD? 2,9,10,11,12,4"] = "0,0,0,0,137.77"
        with patch.object(ModuleAPI, "sleep", autospec=True):
            self.module.execute_sequence_command("auto_range", {"auto_reserve": True}, self.api)
        self.assertFalse(self.module.auto_ranged)
        # Channel 2 should still converge from 100 mV (index 24) to 20 uV (index 13).
        self.assertEqual(int(self.lockin.values["SENSD? 2"]), 13)
        self.assertTrue(
            any(
                kind == "warning" and payload.get("code") == "LOCKIN_AUTO_RANGE_UNCONVERGED"
                for kind, payload in self.events
            )
        )
        self.events.clear()
        self.module.on_event("run_start", {}, self.api)
        self.assertTrue(
            any(
                kind == "resolve" and payload.get("code") == "LOCKIN_AUTO_RANGE_UNCONVERGED"
                for kind, payload in self.events
            )
        )

    def test_auto_range_unconverged_read_gain_error_still_zeros_sine_out(self):
        self.values.update(mode="sine_out", sine_channel=1)
        for channel in ("channel_1", "channel_2"):
            self.values[channel]["time_constant"] = 4
            self.values[channel]["slope"] = 0
        self.start()
        self.scan("voltage", "50mV")
        self.lockin.values["SNAPD? 1,5,6,7,8,4"] = "0,0,0,0,137.77"
        self.lockin.values["SNAPD? 1,9,10,11,12,4"] = "0,0,0,0,137.77"
        original_query = self.lockin.query
        rmodd_calls = [0]

        def failing_query(command):
            if command == "RMODD? 1":
                rmodd_calls[0] += 1
                # Call 1: initial read_gain; call 2: verify after set_reserve(2);
                # call 3: recovery read_gain inside unconverged handler.
                if rmodd_calls[0] >= 3:
                    raise ModuleError("Simulated VISA timeout", "LOCKIN_IO_ERROR")
            return original_query(command)

        with (
            patch.object(ModuleAPI, "sleep", autospec=True),
            patch.object(self.lockin, "query", side_effect=failing_query),
        ):
            with self.assertRaisesRegex(ModuleError, "Simulated VISA timeout"):
                self.module.execute_sequence_command("auto_range", {"auto_reserve": True}, self.api)
        self.assertEqual(self.lockin.writes[-2:], ["SVDCD 1,0", "SWVTD 1,3"])
        self.assertIsNone(self.module.point)
