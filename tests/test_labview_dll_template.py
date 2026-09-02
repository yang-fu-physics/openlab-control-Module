from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT.parent / "OpenLabControl" / "src"
sys.path.insert(0, str(CORE_SRC))

from labcontrol.module_api import ModuleAPI, ModuleWarning  # noqa: E402
from labcontrol.module_commands import normalize_module_commands  # noqa: E402
from labcontrol.package_support.loading import load_source_object  # noqa: E402


EXAMPLE = ROOT / "examples" / "labview_dll"
LabVIEWDLL = load_source_object(
    EXAMPLE,
    "labview_dll:LabVIEWDLL",
    "test_module_labview_dll_loader",
)
LabVIEWDLLFailure = sys.modules[LabVIEWDLL.__module__].LabVIEWDLLFailure
Module = load_source_object(
    EXAMPLE,
    "backend:Module",
    "test_module_labview_dll_backend",
)
BackendLabVIEWDLLFailure = sys.modules[Module.__module__].LabVIEWDLLFailure


def _envelope(result=None, *, severity="ok", code="", message="", context=""):
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "context": context,
        "result": {} if result is None else result,
    }


class _Function:
    def __init__(self, name, responder) -> None:
        self.name = name
        self.responder = responder
        self.argtypes = None
        self.restype = None

    def __call__(
        self,
        request_pointer,
        request_length,
        response_pointer,
        response_capacity,
        response_length_pointer,
    ):
        request = json.loads(
            ctypes.string_at(request_pointer, request_length).decode("utf-8")
        )
        response = json.dumps(
            self.responder(self.name, request),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(response) > response_capacity:
            return 2
        ctypes.memmove(response_pointer, response, len(response))
        ctypes.cast(
            response_length_pointer,
            ctypes.POINTER(ctypes.c_int32),
        )[0] = len(response)
        return 0


class _Library:
    def __init__(self, responder) -> None:
        for name in ("OLC_Describe", "OLC_Open", "OLC_Invoke", "OLC_Close"):
            setattr(self, name, _Function(name, responder))


class _BackendDriver:
    def __init__(self) -> None:
        self.description = {
            "abi_version": 1,
            "kind": "measurement_module",
            "columns": {"Resistance": "Ohm", "StatusCode": ""},
            "display_columns": ["Resistance"],
            "slots": 2,
        }
        self.calls = []
        self.warning = False

    def open(self, payload):
        self.calls.append(("open", payload))
        return {"Connection": "Open"}

    def invoke(self, operation, payload):
        self.calls.append((operation, payload))
        if self.warning:
            raise BackendLabVIEWDLLFailure(
                "sample is over range",
                "DLL_OVER_RANGE",
                "slot_2",
                "warning",
            )
        if operation == "measure":
            return {
                "row": {"Resistance": 12.5, "StatusCode": 0},
                "rawdata": [0.00124, 0.00126],
                "status": {"Last Slot": payload["slot"]},
            }
        return {"State": operation}

    def close(self):
        self.calls.append(("close", {}))
        return {"Connection": "Closed"}


def _api(messages):
    return ModuleAPI(
        _initial_instruments={},
        _emit=lambda kind, payload: messages.append((kind, payload)),
        _operation_timeout_seconds=30.0,
        _instrument_resources={
            "meter_1": {
                "id": "meter_1",
                "address": "GPIB0::24::INSTR",
                "identity": "Test meter",
                "purpose": "measurement",
            }
        },
    )


class LabVIEWDLLTemplateTests(unittest.TestCase):
    def test_ctypes_loader_round_trips_request_and_result(self) -> None:
        calls = []

        def respond(name, request):
            calls.append((name, request))
            if name == "OLC_Describe":
                return _envelope(
                    {"abi_version": 1, "kind": "measurement_module"}
                )
            return _envelope({"echo": request})

        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "driver.dll"
            dll_path.touch()
            loader_module = sys.modules[LabVIEWDLL.__module__]
            with patch.object(loader_module.ctypes, "CDLL", return_value=_Library(respond)):
                driver = LabVIEWDLL(dll_path, expected_kind="measurement_module")
                result = driver.invoke("measure", {"slot": 2})

        self.assertEqual(result["echo"]["parameters"]["slot"], 2)
        self.assertEqual(calls[0], ("OLC_Describe", {}))
        self.assertEqual(calls[1][1]["operation"], "measure")

    def test_backend_exposes_metadata_and_measurement(self) -> None:
        backend_module = sys.modules[Module.__module__]
        driver = _BackendDriver()
        with patch.object(backend_module, "LabVIEWDLL", return_value=driver):
            backend = Module()
        messages = []
        api = _api(messages)

        self.assertEqual(backend.columns, {"Resistance": "Ohm", "StatusCode": ""})
        self.assertEqual(backend.display_columns, ("Resistance",))
        self.assertEqual(backend.slots, 2)
        commands = normalize_module_commands(
            "labview_dll_example",
            backend.sequence_commands,
        )
        self.assertEqual(
            [(command.command_id, command.kind) for command in commands],
            [("set_value", "command"), ("scan_value", "scan")],
        )
        backend.open(api)
        backend.configure({"resource": "meter_1"}, api)
        row, rawdata = backend.measure(2, api)
        backend.execute_sequence_command("set_value", {"value": 0.25}, api)
        backend.execute_sequence_command(
            "scan_value",
            {
                "points": [0.0, 1.0],
                "settle_seconds": 0.0,
                "value": 1.0,
            },
            api,
        )
        backend.on_event("run_end", {"reason": "completed"}, api)
        backend.close(api)

        self.assertEqual(row, {"Resistance": 12.5, "StatusCode": 0})
        self.assertEqual(rawdata, [0.00124, 0.00126])
        self.assertEqual(driver.calls[0][1]["resources"]["meter_1"]["address"], "GPIB0::24::INSTR")
        self.assertEqual(driver.calls[1][1]["settings"], {"resource": "meter_1"})
        self.assertEqual(
            driver.calls[3][1],
            {
                "command_id": "set_value",
                "parameters": {"value": 0.25},
                "operation_timeout_seconds": 30.0,
            },
        )
        self.assertEqual(driver.calls[4][1]["parameters"]["value"], 1.0)
        self.assertIn(("status", {"values": {"Last Slot": 2}}), messages)
        self.assertEqual(driver.calls[-1][0], "close")

    def test_backend_preserves_structured_warning(self) -> None:
        backend_module = sys.modules[Module.__module__]
        driver = _BackendDriver()
        with patch.object(backend_module, "LabVIEWDLL", return_value=driver):
            backend = Module()
        driver.warning = True

        with self.assertRaises(ModuleWarning) as caught:
            backend.measure(2, _api([]))

        self.assertEqual(caught.exception.code, "DLL_OVER_RANGE")
        self.assertEqual(caught.exception.context, "slot_2")


if __name__ == "__main__":
    unittest.main()
