from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "lr700"
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from labcontrol.extensions.loading import load_source_object  # noqa: E402
from labcontrol.measurement.api import (  # noqa: E402
    ModuleError,
    ModuleOperationContext,
)
from labcontrol.measurement.frontend_api import (  # noqa: E402
    ModuleFrontendContext,
)
from labcontrol.measurement.manifest import (  # noqa: E402
    load_manifest,
)
from labcontrol.measurement.settings import (  # noqa: E402
    load_settings,
    save_settings,
)

LR700Backend = load_source_object(
    MODULE,
    "backend:LR700Backend",
    "test_lr700_backend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_lr700_constants",
)
LR700Frontend = load_source_object(
    MODULE,
    "frontend:LR700Frontend",
    "test_lr700_frontend",
)


class _FakeVisaState:
    """按手册响应格式模拟 LR-700，而不是绕过后端解析器直接返回数字。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.failures: dict[str, int] = {}
        self.query_overrides: dict[str, str] = {}
        self.range_index = 5
        self.excitation_index = 0
        self.excitation_percent = 5
        self.pending_excitation_percent = 5
        self.filter_index = 1
        self.mode = 0
        self.local_lockout = 0
        self.sensor = 0
        self.autorange = 0
        self.overloads: dict[int, int] = {}

    def factory(
        self,
        resource: str,
        timeout: float,
    ):
        self.opened.append((resource, timeout))
        return _FakeTransport(self)


class _FakeTransport:
    def __init__(self, state: _FakeVisaState) -> None:
        self.state = state

    def write(self, command: str) -> None:
        self.state.commands.append(("write", command))
        self._fail(command)
        upper = command.upper()
        if upper.startswith("AUTORANGE "):
            self.state.autorange = int(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("MODE "):
            self.state.mode = int(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("SELECT S="):
            self.state.sensor = int(
                command.rsplit("=", 1)[1]
            )
        elif upper.startswith("RANGE "):
            self.state.range_index = int(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("EXCITATION "):
            self.state.excitation_index = int(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("FILTER "):
            self.state.filter_index = int(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("VAREXC ="):
            self.state.pending_excitation_percent = (
                int(command.rsplit("=", 1)[1])
            )
        elif upper == "VAREXC 0":
            self.state.excitation_percent = 100
        elif upper == "VAREXC 1":
            self.state.excitation_percent = (
                self.state.pending_excitation_percent
            )
        else:
            raise AssertionError(
                f"Unexpected write: {command}"
            )

    def query(self, command: str) -> str:
        self.state.commands.append(("query", command))
        self._fail(command)
        if command in self.state.query_overrides:
            return self.state.query_overrides[command]
        if command == "GET 6":
            return (
                f"{self.state.range_index}R,"
                f"{self.state.excitation_index}E,"
                f"{self.state.excitation_percent}%,"
                f"{self.state.filter_index}F,"
                f"{self.state.mode}M,"
                f"{self.state.local_lockout}L,"
                f"{self.state.sensor:02d}S"
            )
        if command == "GET 0":
            return (
                f"+{self.state.sensor * 10:.5f}  "
                "OHMR"
            )
        if command == "GET 1":
            return (
                f"+{self.state.sensor:.5f}  "
                "OHMX"
            )
        if command == "GET 7":
            return (
                f"{self.state.overloads.get(self.state.sensor, 0):03d} "
                "OVERLOADS"
            )
        raise AssertionError(
            f"Unexpected query: {command}"
        )

    def _fail(self, command: str) -> None:
        remaining = self.state.failures.get(
            command,
            0,
        )
        if remaining > 0:
            self.state.failures[command] = (
                remaining - 1
            )
            raise OSError(
                f"simulated failure: {command}"
            )

    def close(self) -> None:
        self.state.closed += 1
        self.state.commands.append(("close", ""))


def _two_slot_settings() -> dict:
    settings = default_settings()
    settings["resource"] = "GPIB0::18::INSTR"
    for channel in settings["channels"].values():
        channel["enabled"] = False
    settings["channels"]["r1"].update({
        "input_channel": 5,
        "enabled": True,
        "range_index": 5,
        "excitation_index": 3,
        "excitation_percent": 50,
        "filter_index": 1,
    })
    settings["channels"]["r2"].update({
        "input_channel": 12,
        "enabled": True,
        "range_index": 9,
        "excitation_index": 4,
        "excitation_percent": 100,
        "filter_index": 0,
    })
    return settings


def _system_sample(
    timestamp: float,
    temperature: float,
    field: float,
) -> dict:
    return {
        "temperature": {
            "kind": "temperature",
            "role": "primary",
            "control_enabled": True,
            "connected": True,
            "current": temperature,
            "unit": "K",
            "timestamp": timestamp,
        },
        "field": {
            "kind": "field",
            "role": "primary",
            "control_enabled": True,
            "connected": True,
            "current": field,
            "unit": "Oe",
            "timestamp": timestamp,
        },
    }


def _samples(sensor_count: int) -> list[dict]:
    result: list[dict] = []
    for sensor in range(1, sensor_count + 1):
        result.extend([
            _system_sample(
                float(sensor * 2 - 1),
                float(sensor * 10),
                float(sensor * 100),
            ),
            _system_sample(
                float(sensor * 2),
                float(sensor * 10 + 2),
                float(sensor * 100 + 20),
            ),
        ])
    return result


class LR700BackendTests(unittest.TestCase):
    def _backend(
        self,
        state: _FakeVisaState,
        waits: list[float] | None = None,
    ) -> LR700Backend:
        return LR700Backend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::18::INSTR",
                "GPIB0::7::INSTR",
            ),
            waiter=(
                lambda context, seconds: (
                    waits.append(seconds)
                    if waits is not None
                    else context.checkpoint()
                )
            ),
        )

    @staticmethod
    def _context(
        messages: list[tuple[str, dict]],
        samples: list[dict] | None = None,
    ) -> ModuleOperationContext:
        iterator = iter(samples or [])
        return ModuleOperationContext(
            {},
            lambda kind, values: messages.append(
                (kind, values)
            ),
            (
                (lambda _timeout: next(iterator))
                if samples is not None
                else None
            ),
            lambda _timeout: "running",
            120.0,
        )

    def test_initialize_discovers_without_connecting_or_writing(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = backend.initialize(
            {"resource": "GPIB0::18::INSTR"},
            self._context(messages),
        )

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertEqual(
            status["Applied Settings"],
            "Not applied",
        )
        self.assertEqual(
            status["Available GPIB Resources"],
            [
                "GPIB0::18::INSTR",
                "GPIB0::7::INSTR",
            ],
        )

    def test_four_slot_mapping_emits_two_sparse_rows_and_restores_minimum_excitation(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.overloads[12] = 4
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        settings = _two_slot_settings()
        backend = self._backend(state, waits)
        context = self._context(
            messages,
            _samples(2),
        )

        backend.initialize(settings, context)
        applied = backend.apply_settings(
            settings,
            context,
        )
        backend.begin_sequence(context)
        backend.measure(context)
        ended = backend.end_sequence(
            "completed",
            context,
        )

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            set(rows[0]),
            {
                "TemperatureAverage",
                "FieldAverage",
                "R1",
                "X1",
                "Status1",
            },
        )
        self.assertEqual(
            set(rows[1]),
            {
                "TemperatureAverage",
                "FieldAverage",
                "R2",
                "X2",
                "Status2",
            },
        )
        self.assertAlmostEqual(rows[0]["R1"], 50.0)
        self.assertAlmostEqual(rows[0]["X1"], 5.0)
        self.assertAlmostEqual(
            rows[0]["TemperatureAverage"],
            11.0,
        )
        self.assertAlmostEqual(
            rows[0]["FieldAverage"],
            110.0,
        )
        self.assertEqual(rows[0]["Status1"], "NORMAL")
        self.assertEqual(rows[1]["Status2"], "OVERLOAD")
        self.assertEqual(waits, [2.0, 1.0] * 2)
        self.assertIn(
            ("write", "SELECT S=05"),
            state.commands,
        )
        self.assertIn(
            ("write", "SELECT S=12"),
            state.commands,
        )
        self.assertIn(
            ("write", "VAREXC =50"),
            state.commands,
        )
        self.assertIn(
            ("write", "VAREXC 0"),
            state.commands,
        )
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertIn(
            "1 uV full scale",
            applied["Excitation Safety"],
        )
        self.assertIn(
            "1 uV full scale",
            ended["Excitation Safety"],
        )
        warnings = [
            payload
            for kind, payload in messages
            if kind == "warning"
        ]
        self.assertTrue(
            any(
                payload["code"]
                == "LR700_READING_WARNING"
                and payload["context"]
                == "R2 / sensor 12"
                for payload in warnings
            )
        )

    def test_transient_read_failure_reopens_and_resolves_warning(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["GET 0"] = 1
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state, waits)
        context = self._context(
            messages,
            _samples(1),
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)

        self.assertGreaterEqual(len(state.opened), 2)
        self.assertEqual(
            sum(
                action == "query"
                and command == "GET 0"
                for action, command in state.commands
            ),
            2,
        )
        self.assertIn(0.2, waits)
        warning_codes = [
            payload.get("code")
            for kind, payload in messages
            if kind == "warning"
        ]
        resolve_codes = [
            payload.get("code")
            for kind, payload in messages
            if kind == "resolve"
        ]
        self.assertIn("LR700_IO_RETRY", warning_codes)
        self.assertIn("LR700_IO_RETRY", resolve_codes)

    def test_uncertain_write_is_not_replayed_and_cleanup_reopens(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["SELECT S=01"] = 1
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages, _samples(1))
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        with self.assertRaises(ModuleError) as captured:
            backend.measure(context)

        self.assertEqual(
            captured.exception.code,
            "LR700_WRITE_UNCERTAIN",
        )
        self.assertEqual(
            sum(
                action == "write"
                and command == "SELECT S=01"
                for action, command in state.commands
            ),
            1,
        )
        self.assertGreaterEqual(len(state.opened), 2)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)

    def test_stale_system_snapshot_is_fatal_and_safe_state_is_restored(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(
            messages,
            [
                _system_sample(1.0, 1.0, 10.0),
                _system_sample(1.0, 2.0, 20.0),
            ],
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        with self.assertRaises(ModuleError) as captured:
            backend.measure(context)

        self.assertEqual(
            captured.exception.code,
            "LR700_SYSTEM_SNAPSHOT_NOT_FRESH",
        )
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)

    def test_abort_confirms_minimum_excitation_and_closes_transport(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        state.excitation_index = 6
        state.excitation_percent = 100

        status = backend.abort(context)

        self.assertIsNone(backend.transport)
        self.assertGreaterEqual(state.closed, 1)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertEqual(
            status["Connection"],
            "Disconnected",
        )

    def test_disable_before_apply_does_not_open_or_change_instrument(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(settings, context)

        status = backend.abort(context)

        self.assertEqual(state.opened, [])
        self.assertEqual(state.commands, [])
        self.assertIn(
            "No instrument connection",
            status["Excitation Safety"],
        )

    def test_safe_cleanup_discards_broken_session_and_retries_once(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        state.excitation_index = 6
        state.excitation_percent = 100
        state.failures["AUTORANGE 0"] = 1

        status = backend.end_sequence(
            "stopped",
            context,
        )

        self.assertGreaterEqual(len(state.opened), 2)
        self.assertGreaterEqual(state.closed, 1)
        self.assertEqual(state.excitation_index, 0)
        self.assertEqual(state.excitation_percent, 5)
        self.assertIn(
            "1 uV full scale",
            status["Excitation Safety"],
        )

    def test_apply_fails_when_minimum_excitation_cannot_be_read_back(
        self,
    ) -> None:
        state = _FakeVisaState()
        # 模拟仪表接受写命令但 GET 6 始终报告高激励。后端只能有界重连一次，
        # 不能把本机发出过命令当成仪表已经安全。
        state.query_overrides["GET 6"] = (
            "5R,6E,100%,1F,0M,0L,01S"
        )
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(settings, context)

        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(settings, context)

        self.assertEqual(
            captured.exception.code,
            "LR700_SAFE_STATE_FAILED",
        )
        self.assertIsNone(backend.transport)
        self.assertGreaterEqual(len(state.opened), 2)

    def test_invalid_filter_timing_and_total_duration_fail_closed(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        context = self._context(messages)

        filter_unsafe = default_settings()
        filter_unsafe["resource"] = "GPIB0::18::INSTR"
        filter_unsafe["channels"]["r1"][
            "filter_index"
        ] = 2
        with self.assertRaises(ModuleError) as filter_error:
            backend.apply_settings(
                filter_unsafe,
                context,
            )
        self.assertEqual(
            filter_error.exception.code,
            "LR700_INVALID_SETTINGS",
        )

        duration_unsafe = default_settings()
        duration_unsafe["resource"] = (
            "GPIB0::18::INSTR"
        )
        duration_unsafe[
            "switch_settle_seconds"
        ] = 20.0
        duration_unsafe["dwell_seconds"] = 20.0
        for channel in duration_unsafe[
            "channels"
        ].values():
            channel["enabled"] = True
        with self.assertRaises(
            ModuleError
        ) as duration_error:
            backend.apply_settings(
                duration_unsafe,
                context,
            )
        self.assertEqual(
            duration_error.exception.code,
            "LR700_MEASURE_TIMEOUT_UNSAFE",
        )
        self.assertEqual(state.opened, [])

    def test_duplicate_physical_inputs_fail_before_connect(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::18::INSTR"
        settings["channels"]["r2"][
            "input_channel"
        ] = 1
        backend = self._backend(state)

        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(
                settings,
                self._context(messages),
            )

        self.assertEqual(
            captured.exception.code,
            "LR700_INVALID_SETTINGS",
        )
        self.assertEqual(state.opened, [])

    def test_response_parser_handles_documented_milli_mega_and_status_bits(
        self,
    ) -> None:
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "+1.50000 M OHMR",
                "R",
                0,
                "GET 0",
            ),
            1.5e-3,
        )
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "+1.50000 M OHMR",
                "R",
                9,
                "GET 0",
            ),
            1.5e6,
        )
        self.assertAlmostEqual(
            LR700Backend._parse_measurement(
                "-2.00000 U OHMX",
                "X",
                3,
                "GET 1",
            ),
            -2.0e-6,
        )
        self.assertEqual(
            LR700Backend._status(0),
            ("NORMAL", ""),
        )
        self.assertEqual(
            LR700Backend._status(64)[0],
            "OVER_RANGE",
        )
        self.assertEqual(
            LR700Backend._status(16)[0],
            "OVERLOAD",
        )
        with self.assertRaises(ModuleError):
            LR700Backend._parse_measurement(
                "+1.00000 M OHMR",
                "R",
                5,
                "GET 0",
            )

    def test_connection_test_uses_unsaved_address_and_performs_no_write(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        initial = default_settings()
        initial["resource"] = "GPIB0::18::INSTR"
        current = default_settings()
        current["resource"] = "GPIB0::7::INSTR"
        context = self._context(messages)
        backend.initialize(initial, context)

        backend.manual_action(
            "test_connection",
            {"settings": current},
            context,
        )

        self.assertEqual(
            state.opened[-1][0],
            "GPIB0::7::INSTR",
        )
        self.assertFalse(
            any(
                action == "write"
                for action, _command in state.commands
            )
        )
        self.assertGreaterEqual(state.closed, 1)


class LR700FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance()
            or QApplication([])
        )

    def test_settings_round_trip_resources_and_manual_payload(
        self,
    ) -> None:
        context = ModuleFrontendContext()
        frontend = LR700Frontend(context)
        owner = QWidget()
        settings_page = frontend.create_settings_page(
            owner
        )
        status_page = frontend.create_status_page(owner)
        self.assertGreaterEqual(
            settings_page.sizeHint().width(),
            1180,
        )
        self.assertGreaterEqual(
            settings_page.sizeHint().height(),
            700,
        )
        settings = _two_slot_settings()
        settings["channels"]["r4"].update({
            "input_channel": 16,
            "enabled": True,
            "range_index": 8,
            "excitation_index": 2,
            "excitation_percent": 25,
            "filter_index": 1,
        })
        frontend.load_settings(settings)
        frontend.update_status({
            "Available GPIB Resources": [
                "GPIB0::7::INSTR",
                "GPIB0::18::INSTR",
            ],
            "Connection": "Disconnected",
        })

        self.assertEqual(frontend.settings(), settings)
        self.assertGreaterEqual(
            frontend.resource.count(),
            2,
        )
        self.assertEqual(
            frontend.status_labels[
                "Connection"
            ].text(),
            "Disconnected",
        )
        actions: list[tuple[str, dict]] = []
        context.manualActionRequested.connect(
            lambda action, payload: actions.append(
                (action, payload)
            )
        )
        frontend.resource.setCurrentText(
            "GPIB0::5::INSTR"
        )
        frontend.test_connection_button.click()
        self.application.processEvents()
        self.assertEqual(
            actions[-1][0],
            "test_connection",
        )
        self.assertEqual(
            actions[-1][1]["settings"]["resource"],
            "GPIB0::5::INSTR",
        )
        settings_page.deleteLater()
        status_page.deleteLater()
        owner.deleteLater()
        self.application.processEvents()

    def test_nested_settings_can_be_saved_as_toml(
        self,
    ) -> None:
        settings = _two_slot_settings()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(
                load_settings(path),
                settings,
            )


class LR700ManifestTests(unittest.TestCase):
    def test_manifest_uses_shared_pyvisa_and_all_sparse_columns(
        self,
    ) -> None:
        descriptor = load_manifest(MODULE)
        self.assertTrue(
            descriptor.valid,
            descriptor.error,
        )
        self.assertEqual(descriptor.id, "lr700")
        self.assertEqual(
            descriptor.version,
            "0.1.0b1",
        )
        self.assertEqual(descriptor.dependencies, ())
        self.assertEqual(
            descriptor.framework_dependencies,
            ("PyVISA>=1.16.2,<1.17",),
        )
        names = [
            column.name
            for column in descriptor.columns
        ]
        self.assertEqual(
            names[:5],
            [
                "TemperatureAverage",
                "FieldAverage",
                "R1",
                "X1",
                "Status1",
            ],
        )
        self.assertEqual(
            names[-3:],
            ["R4", "X4", "Status4"],
        )
        self.assertEqual(len(names), 14)
        self.assertFalse(
            (MODULE / "requirements.lock").exists()
        )
        self.assertFalse((MODULE / "wheels").exists())


if __name__ == "__main__":
    unittest.main()
