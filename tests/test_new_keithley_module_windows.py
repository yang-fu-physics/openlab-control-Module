from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication, QScrollArea, QWidget  # noqa: E402

from labcontrol.measurement.manifest import load_manifest  # noqa: E402
from labcontrol.ui.measurement_modules import ModuleWindow  # noqa: E402


class NewKeithleyModuleWindowTests(unittest.TestCase):
    """用核心真实 ModuleWindow 验证首次布局，而不是只测独立 Settings 页。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_first_show_has_no_horizontal_settings_scrollbar(self) -> None:
        for module_id in (
            "keithley_2400",
            "keithley_6517b",
            "keithley_2614b",
        ):
            with self.subTest(module_id=module_id):
                descriptor = load_manifest(ROOT / "modules" / module_id)
                self.assertTrue(descriptor.valid, descriptor.error)
                owner = QWidget()
                window = ModuleWindow(descriptor, owner)
                window.show()
                for _ in range(12):
                    self.application.processEvents()

                settings_scroll_areas = window.settings_content.findChildren(
                    QScrollArea
                )
                self.assertEqual(len(settings_scroll_areas), 1)
                self.assertFalse(
                    settings_scroll_areas[0].horizontalScrollBar().isVisible(),
                    f"{module_id} first show contains a horizontal scrollbar",
                )

                window.allow_application_close()
                window.close()
                owner.close()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
