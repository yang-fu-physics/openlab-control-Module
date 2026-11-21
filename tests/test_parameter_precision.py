from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "OpenLabControl" / "src"))

from PySide6.QtWidgets import QApplication
from labcontrol.measurement.frontend_api import ModuleUIAPI
from labcontrol.measurement.settings import load_settings, save_settings
from labcontrol.module_api import ModuleError
from labcontrol.package_support.loading import load_source_object


class ModuleParameterPrecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_si_values_round_trip_without_losing_significant_digits(self) -> None:
        for name, protocol in (
            ("keithley_2400", "keithley_2400"),
            ("keithley_2614b", "keithley_2614b"),
            ("keithley_6517b", "keithley_6517b"),
            ("keithley_6221_2182a_delta", "keithley_6221"),
        ):
            with self.subTest(module=name):
                directory = ROOT / "modules" / name
                parse = load_source_object(directory, "quantities:parse_quantity", "precision_parse_" + name)
                render = load_source_object(directory, "quantities:format_quantity", "precision_render_" + name)
                wire = load_source_object(directory, protocol + ":number", "precision_wire_" + name)
                for original in ("1.234567891234567m", "1.234567891234567p", 0.001234567891234567):
                    number = parse(original, expected_unit="A")
                    displayed = render(original, expected_unit="A")
                    self.assertEqual(parse(displayed, expected_unit="A"), number)
                    self.assertEqual(float(wire(number)), number)

    def test_all_module_default_settings_survive_frontend_and_file_round_trip(self) -> None:
        for directory in sorted((ROOT / "modules").iterdir()):
            if not (directory / "frontend.py").is_file():
                continue
            with self.subTest(module=directory.name):
                frontend_type = load_source_object(directory, "frontend:Frontend", "precision_ui_" + directory.name)
                frontend = frontend_type(ModuleUIAPI(resources={}))
                status_page = frontend.status_widget
                self.addCleanup(frontend.close)
                self.addCleanup(status_page.close)
                original = deepcopy(frontend.dump())
                if directory.name in {"keithley_2400", "keithley_6517b"}:
                    original["source_voltage"] = "1.234567891234567"
                elif directory.name == "keithley_2614b":
                    original["channels"]["ch1"]["source_voltage"] = "1.234567891234567"
                elif directory.name == "keithley_6221_2182a_delta":
                    original["shared"]["high_current"] = "1.234567891234567m"
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "settings.toml"
                    save_settings(path, original)
                    frontend.load(load_settings(path))
                self.assertEqual(frontend.dump(), original)
                if hasattr(frontend, "io_timeout"):
                    self.assertEqual(frontend.io_timeout.decimals(), 1)
                if hasattr(frontend, "settle_seconds"):
                    self.assertEqual(frontend.settle_seconds.decimals(), 1)
                    self.assertEqual(frontend.dump()["settle_seconds"], original["settle_seconds"])
                if directory.name in {"keithley_2400", "keithley_6517b"}:
                    self.assertEqual(frontend.nplc.decimals(), 1)
                    invalid = deepcopy(original)
                    invalid["nplc"] = 1.25
                    with self.assertRaises(ValueError):
                        frontend.load(invalid)
                if directory.name == "keithley_2614b":
                    self.assertEqual(frontend.channel_widgets["ch1"]["nplc"].decimals(), 1)
                if directory.name == "keithley_6221_2182a_delta":
                    self.assertEqual(frontend.switch_settle.decimals(), 1)
                    self.assertEqual(frontend.shared_widgets["digital_filter_window_percent"].decimals(), 3)
                    self.assertEqual(frontend.shared_widgets["delta_delay"].text(), "0.1")

    def test_delta_delay_allows_millisecond_and_rejects_sub_millisecond_precision(self) -> None:
        directory = ROOT / "modules" / "keithley_6221_2182a_delta"
        render = load_source_object(directory, "quantities:format_quantity", "precision_delta_time")
        self.assertEqual(render("100m", expected_unit="s"), "0.1")
        self.assertEqual(render("2m", expected_unit="s"), "0.002")
        with self.assertRaises(ValueError):
            render("500u", expected_unit="s")
        backend_type = load_source_object(directory, "backend:Module", "precision_delta_backend")
        with self.assertRaisesRegex(ModuleError, "decimal places"):
            backend_type._number(0.25, 0.0, 300.0, "switch_settle_seconds")

    def test_smu_backends_reject_extra_nplc_and_time_precision(self) -> None:
        for name, key in (("keithley_2400", "nplc"), ("keithley_6517b", "nplc"), ("keithley_2614b", "ch1.nplc")):
            backend = load_source_object(ROOT / "modules" / name, "backend:Module", "precision_backend_" + name)
            with self.subTest(module=name):
                self.assertEqual(backend._finite_number(1.2, key), 1.2)
                with self.assertRaisesRegex(ModuleError, "decimal places"):
                    backend._finite_number(1.25, key)
                with self.assertRaisesRegex(ModuleError, "decimal places"):
                    backend._finite_number(0.25, "settle_seconds")


if __name__ == "__main__":
    unittest.main()
