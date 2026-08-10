from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DisplayColumnDeclarationTests(unittest.TestCase):
    def test_every_bundled_module_selects_existing_result_columns(self) -> None:
        """本仓库的正式示例都应给主窗口选择少量已有测量列。"""

        module_directories = sorted(
            path
            for path in (ROOT / "modules").iterdir()
            if path.is_dir() and (path / "backend.py").is_file()
        )
        self.assertTrue(module_directories)
        for directory in module_directories:
            with self.subTest(module=directory.name):
                tree = ast.parse(
                    (directory / "backend.py").read_text(
                        encoding="utf-8-sig"
                    )
                )
                declarations: dict[str, object] | None = None
                for node in tree.body:
                    if not isinstance(node, ast.ClassDef):
                        continue
                    values: dict[str, object] = {}
                    for statement in node.body:
                        if (
                            isinstance(statement, ast.Assign)
                            and len(statement.targets) == 1
                            and isinstance(
                                statement.targets[0],
                                ast.Name,
                            )
                            and statement.targets[0].id
                            in {"columns", "display_columns"}
                        ):
                            values[statement.targets[0].id] = (
                                ast.literal_eval(statement.value)
                            )
                    if "columns" in values:
                        declarations = values
                        break
                self.assertIsNotNone(declarations)
                assert declarations is not None
                columns = declarations.get("columns")
                display = declarations.get("display_columns")
                self.assertIsInstance(columns, dict)
                self.assertIsInstance(display, tuple)
                self.assertGreater(len(display), 0)
                self.assertLessEqual(len(display), 8)
                self.assertEqual(len(display), len(set(display)))
                self.assertTrue(set(display).issubset(columns))


if __name__ == "__main__":
    unittest.main()
