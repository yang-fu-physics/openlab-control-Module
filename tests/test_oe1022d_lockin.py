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
        self.assertEqual([item.command_id for item in commands], ["scan_voltage"])
        commands = normalize_module_commands("keithley_6221_ac", self.current_module.sequence_commands)
        self.assertEqual([item.command_id for item in commands], ["scan_current", "scan_frequency"])

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
            with self.assertRaisesRegex(ModuleError, "expected.*got"):
                self.scan("current", "200u")
        self.assertNotIn("SOUR:WAVE:INIT", self.source.writes[before:])
        self.assertEqual(self.source.values["OUTP?"], "0")
