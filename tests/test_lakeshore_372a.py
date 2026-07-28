from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "lakeshore_372a"
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
from labcontrol.measurement.manifest import (  # noqa: E402
    load_manifest,
)
from labcontrol.measurement.settings import (  # noqa: E402
    load_settings,
    save_settings,
)

LakeShore372ABackend = load_source_object(
    MODULE,
    "backend:LakeShore372ABackend",
    "test_lakeshore_372a_backend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_lakeshore_372a_constants",
)
compatible_resistance_range_indices = (
    load_source_object(
        MODULE,
        "constants:compatible_resistance_range_indices",
        "test_lakeshore_372a_compatibility",
    )
)
LakeShore372AFrontend = load_source_object(
    MODULE,
    "frontend:LakeShore372AFrontend",
    "test_lakeshore_372a_frontend",
)


class _FakeVisaState:
    def __init__(self) -> None:
        self.identity = "LSCI,MODEL372,3720001,1.3"
        self.commands: list[tuple[str, str]] = []
        self.opened: list[tuple[str, float]] = []
        self.closed = 0
        self.failures: dict[str, int] = {}
        self.query_overrides: dict[str, str] = {}
        self.frequency = 2
        self.filters: dict[int, tuple[int, int, int]] = {}
        self.insets: dict[
            int,
            tuple[int, int, int, int, int],
        ] = {}
        self.intypes: dict[
            int,
            tuple[int, int, int, int, int, int],
        ] = {}
        self.scan = (1, 0)
        self.status = {
            1: 0,
            2: 1,
            3: 16,
            4: 3,
        }

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
        name, values = command.split(" ", 1)
        parts = tuple(
            int(part.strip(), 10)
            for part in values.split(",")
        )
        if name == "FREQ":
            self.state.frequency = parts[1]
        elif name == "FILTER":
            self.state.filters[parts[0]] = parts[1:]
        elif name == "INSET":
            self.state.insets[parts[0]] = parts[1:]
        elif name == "INTYPE":
            self.state.intypes[parts[0]] = parts[1:]
        elif name == "SCAN":
            self.state.scan = parts
        else:
            raise AssertionError(
                f"Unexpected write: {command}"
            )

    def query(self, command: str) -> str:
        self.state.commands.append(("query", command))
        self._fail(command)
        if command in self.state.query_overrides:
            return self.state.query_overrides[command]
        if command == "*IDN?":
            return self.state.identity
        if command == "FREQ? 0":
            return str(self.state.frequency)
        if command == "SCAN?":
            return ",".join(
                str(item)
                for item in self.state.scan
            )
        input_channel = int(command.rsplit(" ", 1)[1])
        if command.startswith("FILTER?"):
            return ",".join(
                str(item)
                for item in self.state.filters[
                    input_channel
                ]
            )
        if command.startswith("INSET?"):
            return ",".join(
                str(item)
                for item in self.state.insets[
                    input_channel
                ]
            )
        if command.startswith("INTYPE?"):
            return ",".join(
                str(item)
                for item in self.state.intypes[
                    input_channel
                ]
            )
        if command.startswith("RDGR?"):
            return str(3.0 * input_channel)
        if command.startswith("QRDG?"):
            return str(4.0 * input_channel)
        if command.startswith("RDGPWR?"):
            return str(
                3.0
                * input_channel
                * (100e-12 ** 2)
            )
        if command.startswith("RDGST?"):
            return str(self.state.status[input_channel])
        raise AssertionError(f"Unexpected query: {command}")

    def _fail(self, command: str) -> None:
        remaining = self.state.failures.get(command, 0)
        if remaining > 0:
            self.state.failures[command] = remaining - 1
            raise OSError(f"simulated failure: {command}")

    def close(self) -> None:
        self.state.closed += 1
        self.state.commands.append(("close", ""))


def _all_channels_settings() -> dict:
    settings = default_settings()
    settings["resource"] = "GPIB0::12::INSTR"
    for channel in settings["channels"].values():
        channel["enabled"] = True
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


def _samples_for_four_channels() -> list[dict]:
    samples: list[dict] = []
    for slot in range(1, 5):
        samples.extend([
            _system_sample(
                float(slot * 2 - 1),
                float(10 * slot),
                float(100 * slot),
            ),
            _system_sample(
                float(slot * 2),
                float(10 * slot + 2),
                float(100 * slot + 20),
            ),
        ])
    return samples


class LakeShore372AConstantsTests(unittest.TestCase):
    def test_manual_figure_1_16_compatibility_boundaries(
        self,
    ) -> None:
        self.assertEqual(
            compatible_resistance_range_indices(
                "current",
                22,
            ),
            tuple(range(1, 10)),
        )
        self.assertEqual(
            compatible_resistance_range_indices(
                "current",
                11,
            ),
            tuple(range(9, 21)),
        )
        self.assertEqual(
            compatible_resistance_range_indices(
                "current",
                1,
            ),
            tuple(range(19, 23)),
        )
        self.assertEqual(
            compatible_resistance_range_indices(
                "voltage",
                1,
            ),
            tuple(range(1, 20)),
        )
        self.assertEqual(
            compatible_resistance_range_indices(
                "voltage",
                12,
            ),
            tuple(range(9, 23)),
        )


class LakeShore372ABackendTests(unittest.TestCase):
    def _backend(
        self,
        state: _FakeVisaState,
        waits: list[float] | None = None,
    ) -> LakeShore372ABackend:
        return LakeShore372ABackend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::12::INSTR",
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

    def test_initialize_discovers_but_does_not_connect_or_apply(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)

        status = backend.initialize(
            {"resource": "GPIB0::12::INSTR"},
            self._context(messages),
        )

        self.assertEqual(state.opened, [])
        self.assertEqual(
            status["Applied Settings"],
            "Not applied",
        )
        self.assertEqual(
            status["Available GPIB Resources"],
            [
                "GPIB0::12::INSTR",
                "GPIB0::7::INSTR",
            ],
        )

    def test_apply_measure_four_rows_and_shunt_each_channel(
        self,
    ) -> None:
        state = _FakeVisaState()
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waits)
        settings = _all_channels_settings()
        context = self._context(
            messages,
            _samples_for_four_channels(),
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
        self.assertEqual(len(rows), 4)
        for slot, row in enumerate(rows, start=1):
            self.assertEqual(
                sorted(
                    key
                    for key in row
                    if key.startswith("R")
                ),
                [f"R{slot}"],
            )
            self.assertNotIn(
                f"R{(slot % 4) + 1}",
                row,
            )
            self.assertAlmostEqual(
                row["TemperatureAverage"],
                10 * slot + 1,
            )
            self.assertAlmostEqual(
                row["FieldAverage"],
                100 * slot + 10,
            )
            self.assertAlmostEqual(
                row[f"Phase{slot}"],
                math.degrees(math.atan2(4.0, 3.0)),
            )
            self.assertAlmostEqual(
                row[f"Current{slot}"],
                100e-12,
            )
        self.assertEqual(
            [row[f"Status{slot}"] for slot, row in enumerate(rows, 1)],
            [
                "NORMAL",
                "OVER_COMPLIANCE",
                "OVER_RANGE",
                "OVER_COMPLIANCE",
            ],
        )
        self.assertEqual(
            waits,
            [3.0, 10.0] * 4,
        )
        self.assertIn(
            ("write", "FREQ 0,2"),
            state.commands,
        )
        for input_channel in range(1, 5):
            scan_index = state.commands.index(
                ("write", f"SCAN {input_channel},0")
            )
            prior = state.commands[:scan_index]
            self.assertTrue(
                any(
                    command.startswith(
                        f"INTYPE {input_channel},"
                    )
                    and command.endswith(",1,2")
                    for action, command in prior
                    if action == "write"
                )
            )
            unshunt_index = next(
                index
                for index, (action, command)
                in enumerate(
                    state.commands[
                        scan_index + 1 :
                    ],
                    start=scan_index + 1,
                )
                if (
                    action == "write"
                    and command.startswith(
                        f"INTYPE {input_channel},"
                    )
                    and command.endswith(",0,2")
                )
            )
            self.assertTrue(
                any(
                    command.startswith(
                        f"INTYPE {input_channel},"
                    )
                    and command.endswith(",1,2")
                    for action, command
                    in state.commands[unshunt_index + 1 :]
                    if action == "write"
                )
            )
        self.assertEqual(
            applied["Excitation"],
            "Shunted",
        )
        self.assertEqual(
            ended["Excitation"],
            "Shunted",
        )

    def test_transient_read_failure_reopens_and_retries(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["RDGR? 1"] = 1
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        backend = self._backend(state, [])
        context = self._context(
            messages,
            [
                _system_sample(1.0, 1.0, 10.0),
                _system_sample(2.0, 3.0, 30.0),
            ],
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)

        self.assertGreaterEqual(len(state.opened), 2)
        self.assertEqual(
            sum(
                action == "query"
                and command == "RDGR? 1"
                for action, command in state.commands
            ),
            2,
        )
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
        self.assertIn("LS372_IO_RETRY", warning_codes)
        self.assertIn("LS372_IO_RETRY", resolve_codes)

    def test_exhausted_read_retry_is_fatal_and_shunted(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.failures["RDGR? 1"] = 2
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        backend = self._backend(state, [])
        context = self._context(
            messages,
            [
                _system_sample(1.0, 1.0, 10.0),
                _system_sample(2.0, 3.0, 30.0),
            ],
        )
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        with self.assertRaises(ModuleError) as captured:
            backend.measure(context)

        self.assertEqual(
            captured.exception.code,
            "LS372_COMMUNICATION_FAILED",
        )
        self.assertEqual(
            state.intypes[1][4],
            1,
        )

    def test_cancel_during_channel_wait_shunts_before_exit(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"

        def cancel_wait(
            _context: ModuleOperationContext,
            _seconds: float,
        ) -> None:
            raise ModuleOperationCancelled(
                "simulated stop"
            )

        backend = LakeShore372ABackend(
            transport_factory=state.factory,
            resource_lister=lambda: (),
            waiter=cancel_wait,
        )
        context = self._context(messages, [])
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        with self.assertRaises(
            ModuleOperationCancelled
        ):
            backend.measure(context)

        self.assertEqual(
            state.intypes[1][4],
            1,
        )

    def test_stale_second_system_snapshot_is_fatal_and_shunted(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        backend = self._backend(state, [])
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
            "LS372_SYSTEM_SNAPSHOT_NOT_FRESH",
        )
        self.assertTrue(
            any(
                action == "write"
                and command.startswith("INTYPE 1,")
                and command.endswith(",1,2")
                for action, command in state.commands
            )
        )

    def test_invalid_identity_and_unsafe_duration_fail_closed(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.identity = "OTHER,MODEL123,1,1"
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        context = self._context(messages)
        backend.initialize(settings, context)
        with self.assertRaises(ModuleError) as identity:
            backend.apply_settings(settings, context)
        self.assertEqual(
            identity.exception.code,
            "LS372_CONNECTION_FAILED",
        )
        self.assertGreaterEqual(state.closed, 1)

        unsafe = deepcopy(settings)
        unsafe["pause_seconds"] = 200
        unsafe["dwell_seconds"] = 200
        state.identity = "LSCI,MODEL372,1,1"
        with self.assertRaises(ModuleError) as duration:
            backend.apply_settings(unsafe, context)
        self.assertEqual(
            duration.exception.code,
            "LS372_MEASURE_TIMEOUT_UNSAFE",
        )

    def test_settings_readback_mismatch_fails_apply(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.query_overrides["INTYPE? 1"] = (
            "1,5,1,17,0,2"
        )
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        backend = self._backend(state)
        context = self._context(messages)
        backend.initialize(settings, context)

        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(settings, context)

        self.assertEqual(
            captured.exception.code,
            "LS372_SETTINGS_VERIFY_FAILED",
        )
        self.assertIsNone(backend.transport)

    def test_incompatible_excitation_and_resistance_fail_before_connect(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        settings["channels"]["r1"].update({
            "excitation_mode": "current",
            "excitation_range": 22,
            "resistance_range": 17,
        })
        backend = self._backend(state)

        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(
                settings,
                self._context(messages),
            )

        self.assertEqual(
            captured.exception.code,
            "LS372_INVALID_SETTINGS",
        )
        self.assertEqual(
            captured.exception.context,
            "r1.resistance_range",
        )
        self.assertEqual(state.opened, [])

    def test_test_connection_uses_unsaved_ui_settings_payload(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        initial = default_settings()
        initial["resource"] = "GPIB0::12::INSTR"
        current = deepcopy(initial)
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

    def test_abort_shunts_and_releases_transport(self) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        context = self._context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)

        status = backend.abort(context)

        self.assertIsNone(backend.transport)
        self.assertGreaterEqual(state.closed, 1)
        self.assertEqual(
            status["Connection"],
            "Disconnected",
        )

    def test_abort_reports_unconfirmed_shunt_after_disconnect(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        context = self._context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend._close_transport()

        with self.assertRaises(ModuleError) as captured:
            backend.abort(context)

        self.assertEqual(
            captured.exception.code,
            "LS372_SHUNT_FAILED",
        )
        statuses = [
            payload["values"]
            for kind, payload in messages
            if kind == "status"
        ]
        self.assertEqual(
            statuses[-1]["Excitation"],
            "Shunt unconfirmed",
        )

    def test_end_sequence_failure_clears_running_state(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        settings = default_settings()
        settings["resource"] = "GPIB0::12::INSTR"
        context = self._context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        state.failures["INTYPE? 1"] = 2

        with self.assertRaises(ModuleError):
            backend.end_sequence("stopped", context)

        self.assertFalse(backend.sequence_active)
        statuses = [
            payload["values"]
            for kind, payload in messages
            if kind == "status"
        ]
        self.assertEqual(
            statuses[-1]["Excitation"],
            "Shunt unconfirmed",
        )

    def test_unit_conversion_and_voltage_mode_current(
        self,
    ) -> None:
        self.assertAlmostEqual(
            LakeShore372ABackend._temperature_kelvin(
                25.0,
                "°C",
            ),
            298.15,
        )
        self.assertAlmostEqual(
            LakeShore372ABackend._temperature_kelvin(
                250.0,
                "mK",
            ),
            0.25,
        )
        self.assertAlmostEqual(
            LakeShore372ABackend._field_oersted(
                1.25,
                "T",
            ),
            12_500.0,
        )
        self.assertAlmostEqual(
            LakeShore372ABackend._excitation_current(
                {
                    "excitation_mode": "voltage",
                    "excitation_range": 1,
                },
                resistance=3.0,
                quadrature=4.0,
                power=12.0,
            ),
            2.0,
        )


class LakeShore372AFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance()
            or QApplication([])
        )

    def test_settings_round_trip_and_resource_dropdown(
        self,
    ) -> None:
        context = ModuleFrontendContext()
        frontend = LakeShore372AFrontend(context)
        owner = QWidget()
        settings_page = frontend.create_settings_page(
            owner
        )
        status_page = frontend.create_status_page(owner)
        self.assertGreaterEqual(
            settings_page.sizeHint().width(),
            980,
        )
        self.assertGreaterEqual(
            settings_page.sizeHint().height(),
            600,
        )
        settings = _all_channels_settings()
        settings["frequency_index"] = 5
        settings["channels"]["r3"][
            "excitation_mode"
        ] = "voltage"
        settings["channels"]["r3"][
            "excitation_range"
        ] = 7

        frontend.load_settings(settings)
        frontend.update_status({
            "Available GPIB Resources": [
                "GPIB0::7::INSTR",
                "GPIB0::12::INSTR",
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

    def test_excitation_disables_incompatible_resistance_ranges(
        self,
    ) -> None:
        frontend = LakeShore372AFrontend(
            ModuleFrontendContext()
        )
        owner = QWidget()
        settings_page = frontend.create_settings_page(
            owner
        )
        frontend.load_settings(default_settings())
        widgets = frontend.channel_widgets["r1"]
        mode = widgets["excitation_mode"]
        excitation = widgets["excitation_range"]
        resistance = widgets["resistance_range"]
        changes: list[None] = []
        frontend.settingsChanged.connect(
            lambda: changes.append(None)
        )

        mode.setCurrentIndex(
            mode.findData("current")
        )
        excitation.setCurrentIndex(
            excitation.findData(22)
        )
        self.application.processEvents()
        current_enabled = {
            int(resistance.itemData(row))
            for row in range(resistance.count())
            if resistance.model().item(row).isEnabled()
        }
        self.assertEqual(
            current_enabled,
            set(range(1, 10)),
        )
        self.assertEqual(
            int(resistance.currentData()),
            9,
        )
        self.assertEqual(len(changes), 1)

        mode.setCurrentIndex(
            mode.findData("voltage")
        )
        excitation.setCurrentIndex(
            excitation.findData(1)
        )
        self.application.processEvents()
        voltage_enabled = {
            int(resistance.itemData(row))
            for row in range(resistance.count())
            if resistance.model().item(row).isEnabled()
        }
        self.assertEqual(
            voltage_enabled,
            set(range(1, 20)),
        )
        self.assertFalse(
            resistance.model().item(
                resistance.findData(20)
            ).isEnabled()
        )
        self.assertEqual(len(changes), 3)

        invalid_loaded = default_settings()
        invalid_loaded["channels"]["r1"].update({
            "excitation_mode": "current",
            "excitation_range": 22,
            "resistance_range": 17,
        })
        frontend.load_settings(invalid_loaded)
        self.assertEqual(
            int(resistance.currentData()),
            17,
        )
        self.assertFalse(
            resistance.model().item(
                resistance.findData(17)
            ).isEnabled()
        )
        self.assertEqual(
            frontend.settings(),
            invalid_loaded,
        )
        self.assertEqual(len(changes), 3)

        settings_page.deleteLater()
        owner.deleteLater()
        self.application.processEvents()

    def test_nested_settings_can_be_saved_as_toml(
        self,
    ) -> None:
        settings = _all_channels_settings()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.toml"
            save_settings(path, settings)
            self.assertEqual(
                load_settings(path),
                settings,
            )


class LakeShore372AManifestTests(unittest.TestCase):
    def test_manifest_uses_framework_dependencies_and_fixed_columns(
        self,
    ) -> None:
        descriptor = load_manifest(MODULE)
        self.assertTrue(
            descriptor.valid,
            descriptor.error,
        )
        self.assertEqual(
            descriptor.id,
            "lakeshore_372a",
        )
        self.assertEqual(
            descriptor.version,
            "0.1.0b4",
        )
        self.assertEqual(descriptor.dependencies, ())
        self.assertEqual(
            descriptor.framework_dependencies,
            (
                "PyVISA>=1.16.2,<1.17",
                "typing_extensions>=4.16,<5",
            ),
        )
        self.assertEqual(
            [column.name for column in descriptor.columns],
            [
                "TemperatureAverage",
                "FieldAverage",
                "R1",
                "Phase1",
                "Current1",
                "Status1",
                "R2",
                "Phase2",
                "Current2",
                "Status2",
                "R3",
                "Phase3",
                "Current3",
                "Status3",
                "R4",
                "Phase4",
                "Current4",
                "Status4",
            ],
        )
        self.assertFalse(
            (MODULE / "requirements.lock").exists()
        )
        self.assertFalse((MODULE / "wheels").exists())


if __name__ == "__main__":
    unittest.main()
