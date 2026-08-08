from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "modules"

# 一个物理仪表对应一个文件；组合模块仍只有一个负责生命周期的 backend.py。
EXPECTED_INSTRUMENT_FILES = {
    "keithley_2400": ("keithley_2400.py",),
    "keithley_2614b": ("keithley_2614b.py",),
    "keithley_6517b": ("keithley_6517b.py",),
    "lakeshore_372a": ("lakeshore_372a.py",),
    "lr700": ("lr700.py",),
    "keithley_6221_2182a_delta": (
        "keithley_6221.py",
        "keithley_2182a.py",
        "keithley_7001.py",
        "keithley_3706a.py",
    ),
}


def _called_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return ""


def test_each_physical_instrument_has_one_protocol_file() -> None:
    for module_name, filenames in EXPECTED_INSTRUMENT_FILES.items():
        module_path = MODULES / module_name
        assert (module_path / "backend.py").is_file()
        for filename in filenames:
            assert (module_path / filename).is_file(), (
                f"{module_name} is missing protocol file {filename}"
            )

    # 两种切换器已经合并到同一个 Delta 模块，不允许旧实现重新出现。
    assert not (MODULES / "keithley_6221_2182a_delta_3706a").exists()


def test_hardware_backends_do_not_embed_executed_command_literals() -> None:
    """命令文字必须来自仪表文件，backend 只负责调用顺序和故障策略。"""

    offenders: list[str] = []
    for module_name in EXPECTED_INSTRUMENT_FILES:
        backend = MODULES / module_name / "backend.py"
        tree = ast.parse(backend.read_text(encoding="utf-8-sig"), filename=str(backend))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            called = _called_name(node)
            is_io_call = called in {"write", "query"} or called.startswith(
                ("_write", "_query", "_serial_write", "_serial_query")
            )
            if not is_io_call:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                offenders.append(
                    f"{module_name}/backend.py:{node.lineno}: {first.value!r}"
                )
    assert offenders == [], "instrument commands remain in backend:\n" + "\n".join(
        offenders
    )


def test_pyvisa_transport_is_owned_by_protocol_files() -> None:
    for module_name in EXPECTED_INSTRUMENT_FILES:
        backend = (MODULES / module_name / "backend.py").read_text(encoding="utf-8-sig")
        assert "class PyVisaTransport" not in backend
        assert "importlib.import_module(\"pyvisa\")" not in backend
