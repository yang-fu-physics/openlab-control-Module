from __future__ import annotations

import math
import os
import re
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = (
    ROOT
    / "modules"
    / "keithley_6221_2182a_delta"
)
sys.path.insert(0, str(CORE / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from labcontrol.extensions.loading import (  # noqa: E402
    load_source_object,
)
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


Keithley6221DeltaBackend = load_source_object(
    MODULE,
    "backend:Keithley6221DeltaBackend",
    "test_keithley_delta_backend",
)
Keithley6221DeltaFrontend = load_source_object(
    MODULE,
    "frontend:Keithley6221DeltaFrontend",
    "test_keithley_delta_frontend",
)
default_settings = load_source_object(
    MODULE,
    "constants:default_settings",
    "test_keithley_delta_constants",
)
MODE_INDEPENDENT = load_source_object(
    MODULE,
    "constants:MODE_INDEPENDENT",
    "test_keithley_delta_mode_independent",
)
parse_quantity = load_source_object(
    MODULE,
    "quantities:parse_quantity",
    "test_keithley_delta_parse_quantity",
)
format_quantity = load_source_object(
    MODULE,
    "quantities:format_quantity",
    "test_keithley_delta_format_quantity",
)
load_routing = load_source_object(
    MODULE,
    "routing:load_routing",
    "test_keithley_delta_routing",
)


class _FakeVisaState:
    """模拟两个独立 GPIB 仪表及 6221 转发的 2182A。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str, str]] = []
        self.query_timeouts: list[
            tuple[str, float | None]
        ] = []
        self.opened: list[tuple[str, float]] = []
        self.closed: list[str] = []
        self.failures: dict[tuple[str, str, str], int] = {}
        self.armed = False
        self.output = False
        self.current = 0.0
        self.high = 1.0e-3
        self.low = -1.0e-3
        self.compliance = 10.0
        self.delay = 0.0
        self.count = 1
        self.cold_switch = True
        self.compliance_abort = True
        self.serial_pending = ""
        self.nplc = 1.0
        self.range_auto = True
        self.voltage_range = 0.01
        self.analog_filter = False
        self.digital_filter = False
        self.digital_filter_type = "MOV"
        self.digital_filter_count = 10
        self.digital_filter_window = 0.01
        self.closed_routes: set[str] = set()
        self.trace_by_channel: dict[str, str] = {
            "ch1": "1e-6,3e-6",
            "ch2": "2e-6,4e-6",
            "ch3": "3e-6,5e-6",
            "ch4": "4e-6,6e-6",
        }

    def factory(
        self,
        resource: str,
        timeout: float,
    ):
        self.opened.append((resource, timeout))
        if "::12::" in resource:
            return _Fake6221(self, resource)
        if "::7::" in resource:
            return _Fake7001(self, resource)
        raise OSError(f"unknown fake resource {resource}")

    def fail(
        self,
        resource: str,
        action: str,
        command: str,
        count: int = 1,
    ) -> None:
        self.failures[(resource, action, command)] = count

    def maybe_fail(
        self,
        resource: str,
        action: str,
        command: str,
    ) -> None:
        key = (resource, action, command)
        remaining = self.failures.get(key, 0)
        if remaining:
            self.failures[key] = remaining - 1
            raise OSError(
                f"simulated {resource} {action} failure: "
                f"{command}"
            )

    def active_channel(self) -> str:
        routes = {
            "ch1": {"1!1", "1!11", "1!5", "1!15"},
            "ch2": {"1!2", "1!12", "1!5", "1!15"},
            "ch3": {"1!3", "1!13", "1!5", "1!15"},
            "ch4": {"1!4", "1!14", "1!5", "1!15"},
        }
        for channel, expected in routes.items():
            if self.closed_routes == expected:
                return channel
        return "ch1"


class _Fake6221:
    def __init__(
        self,
        state: _FakeVisaState,
        resource: str,
    ) -> None:
        self.state = state
        self.resource = resource

    def write(self, command: str) -> None:
        self.state.commands.append(
            (self.resource, "write", command)
        )
        self.state.maybe_fail(
            self.resource,
            "write",
            command,
        )
        upper = command.upper()
        if upper == "SOUR:SWE:ABOR":
            self.state.armed = False
            self.state.current = 0.0
            self.state.output = False
        elif upper == "SOUR:CLE":
            self.state.current = 0.0
            self.state.output = False
        elif upper.startswith("SOUR:CURR:COMP "):
            self.state.compliance = float(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("SOUR:DELT:HIGH "):
            self.state.high = float(
                command.split(" ", 1)[1]
            )
            self.state.low = -self.state.high
        elif upper.startswith("SOUR:DELT:LOW "):
            self.state.low = float(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("SOUR:DELT:DEL "):
            self.state.delay = float(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("SOUR:DELT:COUN "):
            self.state.count = int(
                command.split(" ", 1)[1]
            )
        elif upper == "SOUR:DELT:CSW ON":
            self.state.cold_switch = True
        elif upper == "SOUR:DELT:CAB ON":
            self.state.compliance_abort = True
        elif upper == "SOUR:DELT:ARM":
            self.state.armed = True
            self.state.current = 0.0
        elif upper == "INIT:IMM":
            if not self.state.armed:
                raise AssertionError(
                    "INIT:IMM sent while not armed"
                )
            # 有限 count + cold switch 完成后回到零电流，但仍保持 Armed。
            self.state.output = True
            self.state.current = 0.0
        elif upper.startswith(
            'SYST:COMM:SER:SEND "'
        ):
            matched = re.fullmatch(
                r'SYST:COMM:SER:SEND "(.*)"',
                command,
                flags=re.IGNORECASE,
            )
            assert matched is not None
            self._serial_write(
                matched.group(1).replace('""', '"')
            )
        elif upper.startswith(
            (
                "UNIT ",
                "FORM:",
                "SENS:AVER:",
                "SOUR:SWE:COUN ",
                "TRAC:POIN ",
            )
        ):
            pass
        else:
            raise AssertionError(
                f"Unexpected 6221 write: {command}"
            )

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str:
        self.state.query_timeouts.append(
            (command, timeout_seconds)
        )
        self.state.commands.append(
            (self.resource, "query", command)
        )
        self.state.maybe_fail(
            self.resource,
            "query",
            command,
        )
        upper = command.upper()
        replies = {
            "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,1234,C01",
            "SOUR:DELT:NVPRESENT?": "1",
            "SOUR:CURR:COMP?": f"{self.state.compliance:g}",
            "SOUR:DELT:HIGH?": f"{self.state.high:g}",
            "SOUR:DELT:LOW?": f"{self.state.low:g}",
            "SOUR:DELT:DEL?": f"{self.state.delay:g}",
            "SOUR:DELT:COUN?": str(self.state.count),
            "SOUR:DELT:CSW?": (
                "1" if self.state.cold_switch else "0"
            ),
            "SOUR:DELT:CAB?": (
                "1"
                if self.state.compliance_abort
                else "0"
            ),
            "SOUR:DELT:ARM?": (
                "1" if self.state.armed else "0"
            ),
            "OUTP?": "1" if self.state.output else "0",
            "SOUR:CURR?": f"{self.state.current:.12g}",
            "SYST:ERR?": '0,"No error"',
            "*OPC?": "1",
            "TRAC:DATA?": self.state.trace_by_channel[
                self.state.active_channel()
            ],
        }
        if upper == "SYST:COMM:SER:ENT?":
            return self._serial_reply()
        if upper in replies:
            return replies[upper]
        raise AssertionError(
            f"Unexpected 6221 query: {command}"
        )

    def _serial_write(self, command: str) -> None:
        upper = command.upper()
        if upper.endswith("?"):
            self.state.serial_pending = upper
            return
        if upper.startswith("VOLT:RANG:AUTO "):
            self.state.range_auto = (
                upper.rsplit(" ", 1)[1] == "ON"
            )
        elif upper.startswith("VOLT:RANG "):
            self.state.voltage_range = float(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("VOLT:NPLC "):
            self.state.nplc = float(
                command.split(" ", 1)[1]
            )
        elif upper.startswith("VOLT:LPAS "):
            self.state.analog_filter = (
                upper.rsplit(" ", 1)[1] == "ON"
            )
        elif upper.startswith("VOLT:DFIL:TCON "):
            self.state.digital_filter_type = (
                upper.rsplit(" ", 1)[1]
            )
        elif upper.startswith("VOLT:DFIL:COUN "):
            self.state.digital_filter_count = int(
                upper.rsplit(" ", 1)[1]
            )
        elif upper.startswith("VOLT:DFIL:WIND "):
            self.state.digital_filter_window = float(
                upper.rsplit(" ", 1)[1]
            )
        elif upper.startswith("VOLT:DFIL:STAT "):
            self.state.digital_filter = (
                upper.rsplit(" ", 1)[1] == "ON"
            )
        else:
            raise AssertionError(
                f"Unexpected 2182A serial write: {command}"
            )

    def _serial_reply(self) -> str:
        query = self.state.serial_pending
        self.state.serial_pending = ""
        replies = {
            "*IDN?": (
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,"
                "5678,C08"
            ),
            "VOLT:RANG:AUTO?": (
                "1" if self.state.range_auto else "0"
            ),
            "VOLT:RANG?": f"{self.state.voltage_range:g}",
            "VOLT:NPLC?": f"{self.state.nplc:g}",
            "VOLT:LPAS?": (
                "1"
                if self.state.analog_filter
                else "0"
            ),
            "VOLT:DFIL:STAT?": (
                "1"
                if self.state.digital_filter
                else "0"
            ),
        }
        if query not in replies:
            raise AssertionError(
                f"Unexpected 2182A serial query: {query}"
            )
        return replies[query]

    def close(self) -> None:
        self.state.closed.append(self.resource)
        self.state.commands.append(
            (self.resource, "close", "")
        )


class _Fake7001:
    def __init__(
        self,
        state: _FakeVisaState,
        resource: str,
    ) -> None:
        self.state = state
        self.resource = resource

    @staticmethod
    def _routes(command: str) -> list[str]:
        matched = re.search(r"\(@(.*)\)", command)
        if matched is None:
            return []
        return [
            item.strip()
            for item in matched.group(1).split(",")
        ]

    def write(self, command: str) -> None:
        self.state.commands.append(
            (self.resource, "write", command)
        )
        self.state.maybe_fail(
            self.resource,
            "write",
            command,
        )
        upper = command.upper()
        if upper == "ROUT:OPEN ALL":
            self.state.closed_routes.clear()
        elif upper.startswith("ROUT:CLOS "):
            self.state.closed_routes.update(
                self._routes(command)
            )
        else:
            raise AssertionError(
                f"Unexpected 7001 write: {command}"
            )

    def query(
        self,
        command: str,
        timeout_seconds: float | None = None,
    ) -> str:
        del timeout_seconds
        self.state.commands.append(
            (self.resource, "query", command)
        )
        self.state.maybe_fail(
            self.resource,
            "query",
            command,
        )
        upper = command.upper()
        if upper == "*IDN?":
            return (
                "KEITHLEY INSTRUMENTS INC.,MODEL 7001,"
                "9876,A01"
            )
        routes = self._routes(command)
        if upper.startswith("ROUT:OPEN? "):
            return ",".join(
                "1"
                if route not in self.state.closed_routes
                else "0"
                for route in routes
            )
        if upper.startswith("ROUT:CLOS? "):
            return ",".join(
                "1"
                if route in self.state.closed_routes
                else "0"
                for route in routes
            )
        raise AssertionError(
            f"Unexpected 7001 query: {command}"
        )

    def close(self) -> None:
        self.state.closed.append(self.resource)
        self.state.commands.append(
            (self.resource, "close", "")
        )


def _settings(
    *,
    channels: int = 2,
    independent: bool = False,
) -> dict:
    settings = default_settings()
    settings["resource_6221"] = "GPIB0::12::INSTR"
    settings["resource_7001"] = "GPIB0::7::INSTR"
    settings["switch_settle_seconds"] = 0.0
    for index, channel in enumerate(
        settings["channels"].values(),
        start=1,
    ):
        channel["enabled"] = index <= channels
    settings["shared"].update(
        {
            "high_current": "10u",
            "low_current": "-10u",
            "count": 2,
        }
    )
    for index in range(1, 5):
        settings["independent"][f"ch{index}"].update(
            {
                "high_current": f"{index * 10}u",
                "low_current": f"-{index * 10}u",
                "count": 2,
            }
        )
    if independent:
        settings["mode"] = MODE_INDEPENDENT
    return settings


def _context(
    messages: list[tuple[str, dict]],
) -> ModuleOperationContext:
    return ModuleOperationContext(
        {},
        lambda kind, values: messages.append(
            (kind, values)
        ),
        None,
        lambda _timeout: "running",
        300.0,
    )


class QuantityTests(unittest.TestCase):
    def test_si_prefixes_parse_and_format_without_long_decimals(
        self,
    ) -> None:
        self.assertEqual(
            parse_quantity("1m", expected_unit="A"),
            1.0e-3,
        )
        self.assertEqual(
            parse_quantity("1pA", expected_unit="A"),
            1.0e-12,
        )
        self.assertEqual(
            parse_quantity("2ms", expected_unit="s"),
            2.0e-3,
        )
        self.assertEqual(
            format_quantity(1.0e-3, expected_unit="A"),
            "1m",
        )
        self.assertEqual(
            format_quantity(1.0e-12, expected_unit="A"),
            "1p",
        )
        with self.assertRaises(ValueError):
            parse_quantity("1mV", expected_unit="A")


class RoutingTests(unittest.TestCase):
    def test_default_hidden_routes_match_requested_four_channels(
        self,
    ) -> None:
        routing = load_routing()
        self.assertEqual(
            routing.channels["ch1"],
            ("1!1", "1!11", "1!5", "1!15"),
        )
        self.assertEqual(
            routing.channels["ch4"],
            ("1!4", "1!14", "1!5", "1!15"),
        )
        self.assertEqual(
            routing.list_text("ch2"),
            "(@1!2,1!12,1!5,1!15)",
        )


class BackendTests(unittest.TestCase):
    def _backend(
        self,
        state: _FakeVisaState,
        waits: list[float] | None = None,
    ) -> Keithley6221DeltaBackend:
        def wait(
            context: ModuleOperationContext,
            seconds: float,
        ) -> None:
            # 把等待放进同一命令时间线，才能断言 ARM 查询确实发生在 3 秒等待之后，
            # 而不是仅仅断言“某处调用过一次 sleep”。
            state.commands.append(
                ("<module>", "wait", f"{seconds:g}")
            )
            if waits is not None:
                waits.append(seconds)
            else:
                context.checkpoint()

        return Keithley6221DeltaBackend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::12::INSTR",
                "GPIB0::7::INSTR",
            ),
            waiter=wait,
        )

    def test_enable_probes_7001_but_does_not_touch_6221(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        status = backend.initialize(
            _settings(),
            _context(messages),
        )
        self.assertTrue(backend.switcher_available)
        self.assertIn("MODEL 7001", status["7001"])
        self.assertFalse(
            any(
                resource == "GPIB0::12::INSTR"
                for resource, _timeout in state.opened
            )
        )
        self.assertEqual(
            [
                command
                for resource, action, command
                in state.commands
                if resource == "GPIB0::7::INSTR"
                and action == "query"
            ],
            ["*IDN?"],
        )

    def test_connection_test_uses_current_ui_settings_payload(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state)
        saved = _settings()
        saved["resource_6221"] = "GPIB0::99::INSTR"
        context = _context(messages)
        backend.initialize(saved, context)
        current = _settings()

        backend.manual_action(
            "test_connection",
            {"settings": current},
            context,
        )

        self.assertIn(
            ("GPIB0::12::INSTR", 3.0),
            state.opened,
        )
        self.assertFalse(
            any(
                resource == "GPIB0::99::INSTR"
                for resource, _timeout in state.opened
            )
        )

    def test_lifecycle_timeouts_are_validated_separately(
        self,
    ) -> None:
        state = _FakeVisaState()
        backend = self._backend(state)
        settings = _settings(channels=1)
        # 旧版持久设置可以继续读取，但已删除的限制/超时键必须被忽略且不再输出。
        settings["absolute_current_limit"] = "1n"
        settings["absolute_compliance_limit"] = "100m"
        settings["shared"][
            "measurement_timeout_seconds"
        ] = 1.0

        # 共享模式的 3 秒 ARM 只属于 Begin；Measure 使用设置推导出的采集时间，
        # 不再读取或要求一个用户配置的单通道超时。29 秒的每调用上限足够。
        normalized = backend._normalized_settings(
            settings,
            require_6221=True,
            require_measurement=True,
            operation_timeout_seconds=29.0,
        )
        self.assertEqual(
            normalized["mode"],
            settings["mode"],
        )
        self.assertNotIn(
            "absolute_current_limit",
            normalized,
        )
        self.assertNotIn(
            "absolute_compliance_limit",
            normalized,
        )
        self.assertNotIn(
            "measurement_timeout_seconds",
            normalized["shared"],
        )

        with self.assertRaises(ModuleError) as captured:
            backend._normalized_settings(
                settings,
                require_6221=True,
                require_measurement=True,
                operation_timeout_seconds=9.5,
            )
        self.assertEqual(
            captured.exception.code,
            "K6221_BEGIN_TIMEOUT_UNSAFE",
        )

        slow = _settings(channels=1)
        slow["shared"]["delta_delay"] = "10"
        with self.assertRaises(ModuleError) as captured:
            backend._normalized_settings(
                slow,
                require_6221=True,
                require_measurement=True,
                operation_timeout_seconds=20.0,
            )
        self.assertEqual(
            captured.exception.code,
            "K6221_MEASURE_TIMEOUT_UNSAFE",
        )

    def test_missing_7001_falls_back_to_ch1_only(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.fail(
            "GPIB0::7::INSTR",
            "query",
            "*IDN?",
        )
        messages: list[tuple[str, dict]] = []
        waits: list[float] = []
        backend = self._backend(state, waits)
        settings = _settings(channels=4)
        context = _context(messages)
        backend.initialize(settings, context)
        self.assertFalse(backend.switcher_available)
        self.assertTrue(
            any(
                kind == "warning"
                and payload["code"]
                == "K6221_SWITCHER_UNAVAILABLE"
                for kind, payload in messages
            )
        )
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        backend.measure(context)
        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(
            [row["Channel"] for row in rows],
            [1],
        )
        self.assertEqual(waits, [3.0])
        # Enable 探测失败后，Apply/Measure 不再尝试 7001，也不会在未知路由上触发。
        self.assertEqual(
            sum(
                action == "write"
                and command.startswith("ROUT:")
                for resource, action, command
                in state.commands
                if resource == "GPIB0::7::INSTR"
            ),
            0,
        )

    def test_shared_mode_arms_once_at_sequence_start_and_emits_raw_rows(
        self,
    ) -> None:
        state = _FakeVisaState()
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waits)
        settings = _settings(channels=2)
        context = _context(messages)

        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        self.assertFalse(state.armed)
        backend.begin_sequence(context)
        self.assertTrue(state.armed)
        backend.measure(context)

        completion_timeouts = [
            timeout
            for command, timeout in state.query_timeouts
            if command == "*OPC?"
        ]
        self.assertEqual(len(completion_timeouts), 2)
        self.assertTrue(
            all(
                timeout is not None
                and 250.0 < timeout < 300.0
                for timeout in completion_timeouts
            )
        )

        rows = [
            payload
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [payload["values"]["Channel"] for payload in rows],
            [1, 2],
        )
        self.assertEqual(
            rows[0]["raw_values"],
            [1.0e-6, 3.0e-6],
        )
        self.assertAlmostEqual(
            rows[0]["values"]["Resistance"],
            0.2,
        )
        self.assertAlmostEqual(
            rows[0]["values"]["Current"],
            10.0e-6,
        )
        self.assertAlmostEqual(
            rows[0]["values"]["StdDev"],
            math.sqrt(0.02),
        )
        self.assertEqual(
            rows[0]["values"]["StatusCode"],
            0,
        )
        arm_commands = [
            command
            for resource, action, command
            in state.commands
            if resource == "GPIB0::12::INSTR"
            and action == "write"
            and command == "SOUR:DELT:ARM"
        ]
        self.assertEqual(arm_commands, ["SOUR:DELT:ARM"])
        self.assertEqual(waits, [3.0, 0.0, 0.0])
        arm_index = state.commands.index(
            (
                "GPIB0::12::INSTR",
                "write",
                "SOUR:DELT:ARM",
            )
        )
        wait_index = state.commands.index(
            ("<module>", "wait", "3"),
            arm_index,
        )
        verify_index = state.commands.index(
            (
                "GPIB0::12::INSTR",
                "query",
                "SOUR:DELT:ARM?",
            ),
            arm_index,
        )
        self.assertLess(arm_index, wait_index)
        self.assertLess(wait_index, verify_index)

        backend.end_sequence("completed", context)
        self.assertFalse(state.armed)
        self.assertFalse(state.output)
        self.assertEqual(state.closed_routes, set())

    def test_independent_mode_rearms_after_every_switch(
        self,
    ) -> None:
        state = _FakeVisaState()
        waits: list[float] = []
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, waits)
        settings = _settings(
            channels=2,
            independent=True,
        )
        context = _context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        self.assertFalse(state.armed)
        backend.measure(context)

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        for actual, expected in zip(
            [row["Current"] for row in rows],
            [10.0e-6, 20.0e-6],
            strict=True,
        ):
            self.assertAlmostEqual(actual, expected)
        arm_count = sum(
            resource == "GPIB0::12::INSTR"
            and action == "write"
            and command == "SOUR:DELT:ARM"
            for resource, action, command
            in state.commands
        )
        self.assertEqual(arm_count, 2)
        self.assertEqual(
            waits,
            [0.0, 3.0, 0.0, 3.0],
        )
        arm_indices = [
            index
            for index, item in enumerate(state.commands)
            if item
            == (
                "GPIB0::12::INSTR",
                "write",
                "SOUR:DELT:ARM",
            )
        ]
        wait_indices = [
            index
            for index, item in enumerate(state.commands)
            if item == ("<module>", "wait", "3")
        ]
        self.assertEqual(len(arm_indices), 2)
        self.assertEqual(len(wait_indices), 2)
        for arm_index, wait_index in zip(
            arm_indices,
            wait_indices,
            strict=True,
        ):
            verify_index = next(
                index
                for index in range(
                    wait_index + 1,
                    len(state.commands),
                )
                if state.commands[index]
                == (
                    "GPIB0::12::INSTR",
                    "query",
                    "SOUR:DELT:ARM?",
                )
            )
            self.assertLess(arm_index, wait_index)
            self.assertLess(wait_index, verify_index)
        abort_count = sum(
            resource == "GPIB0::12::INSTR"
            and action == "write"
            and command == "SOUR:SWE:ABOR"
            for resource, action, command
            in state.commands
        )
        self.assertGreaterEqual(abort_count, 4)

    def test_bad_reading_marks_channel_error_and_warns_without_raising(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.trace_by_channel["ch1"] = (
            "1e-6,1e200,BAD"
        )
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        settings = _settings(channels=1)
        settings["shared"]["count"] = 3
        context = _context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)

        row = next(
            payload
            for kind, payload in messages
            if kind == "row"
        )
        self.assertEqual(
            row["values"]["StatusCode"],
            3,
        )
        self.assertNotIn(
            "Resistance",
            row["values"],
        )
        self.assertNotIn("StdDev", row["values"])
        self.assertEqual(
            row["raw_values"],
            [1.0e-6, 1.0e200],
        )
        self.assertTrue(
            any(
                kind == "warning"
                and payload["code"]
                == "K6221_READING_WARNING"
                for kind, payload in messages
            )
        )

    def test_voltage_overrange_has_its_own_numeric_status(
        self,
    ) -> None:
        state = _FakeVisaState()
        state.trace_by_channel["ch1"] = (
            "1e200,3e-6"
        )
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        settings = _settings(channels=1)
        context = _context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)

        backend.measure(context)

        row = next(
            payload
            for kind, payload in messages
            if kind == "row"
        )
        self.assertEqual(
            row["values"]["StatusCode"],
            1,
        )
        self.assertEqual(row["values"]["Channel"], 1)
        self.assertNotIn("Resistance", row["values"])

    def test_7001_runtime_failure_has_no_retry_and_is_fatal(
        self,
    ) -> None:
        state = _FakeVisaState()
        messages: list[tuple[str, dict]] = []
        backend = self._backend(state, [])
        settings = _settings(channels=1)
        context = _context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)
        backend.begin_sequence(context)
        command = (
            "ROUT:CLOS (@1!1,1!11,1!5,1!15)"
        )
        state.fail(
            "GPIB0::7::INSTR",
            "write",
            command,
        )

        with self.assertRaises(ModuleError) as captured:
            backend.measure(context)

        self.assertEqual(
            captured.exception.code,
            "K6221_SWITCHER_COMMUNICATION_FAILED",
        )
        attempts = sum(
            resource == "GPIB0::7::INSTR"
            and action == "write"
            and sent == command
            for resource, action, sent in state.commands
        )
        self.assertEqual(attempts, 1)
        # 不等待核心稍后调用 end_sequence：失败返回 worker 前已经直接请求
        # Abort/zero-current/open-all，尽可能缩短不确定路由状态的持续时间。
        self.assertFalse(backend.sequence_active)
        self.assertFalse(state.armed)
        self.assertFalse(state.output)
        self.assertEqual(state.closed_routes, set())
        backend.end_sequence("error", context)
        self.assertFalse(state.output)
        self.assertEqual(state.closed_routes, set())

    def test_stop_checkpoint_requests_immediate_safe_state(
        self,
    ) -> None:
        state = _FakeVisaState()
        backend = self._backend(state, [])
        settings = _settings(channels=1)
        messages: list[tuple[str, dict]] = []
        running = _context(messages)
        backend.initialize(settings, running)
        backend.apply_settings(settings, running)
        backend.begin_sequence(running)
        self.assertTrue(state.armed)
        stopping = ModuleOperationContext(
            {},
            lambda kind, values: messages.append(
                (kind, values)
            ),
            None,
            lambda _timeout: "stopping",
            300.0,
        )

        with self.assertRaises(ModuleOperationCancelled):
            backend.measure(stopping)

        self.assertFalse(backend.sequence_active)
        self.assertFalse(state.armed)
        self.assertFalse(state.output)
        self.assertEqual(state.closed_routes, set())

    def test_stop_during_arm_wait_aborts_before_begin_returns(
        self,
    ) -> None:
        state = _FakeVisaState()

        def cancel_during_arm(
            _context: ModuleOperationContext,
            seconds: float,
        ) -> None:
            self.assertEqual(seconds, 3.0)
            # waiter 只会在 SOUR:DELT:ARM 写入之后调用。
            self.assertTrue(state.armed)
            raise ModuleOperationCancelled(
                "stop during ARM wait"
            )

        backend = Keithley6221DeltaBackend(
            transport_factory=state.factory,
            resource_lister=lambda: (
                "GPIB0::12::INSTR",
                "GPIB0::7::INSTR",
            ),
            waiter=cancel_during_arm,
        )
        settings = _settings(channels=1)
        messages: list[tuple[str, dict]] = []
        context = _context(messages)
        backend.initialize(settings, context)
        backend.apply_settings(settings, context)

        with self.assertRaises(ModuleOperationCancelled):
            backend.begin_sequence(context)

        self.assertFalse(backend.sequence_active)
        self.assertFalse(state.armed)
        self.assertFalse(state.output)
        self.assertEqual(state.closed_routes, set())

    def test_zero_default_and_device_command_ranges_block_apply(
        self,
    ) -> None:
        state = _FakeVisaState()
        backend = self._backend(state)
        settings = default_settings()
        settings["resource_6221"] = (
            "GPIB0::12::INSTR"
        )
        messages: list[tuple[str, dict]] = []
        context = _context(messages)
        backend.initialize(settings, context)
        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(settings, context)
        self.assertEqual(
            captured.exception.code,
            "K6221_INVALID_SETTINGS",
        )

        unsafe = _settings(channels=1)
        unsafe["shared"]["high_current"] = "200m"
        with self.assertRaises(ModuleError) as captured:
            backend.apply_settings(unsafe, context)
        self.assertEqual(
            captured.exception.code,
            "K6221_INVALID_SETTINGS",
        )


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = (
            QApplication.instance() or QApplication([])
        )

    def test_si_text_mode_pages_and_ch1_only_status(
        self,
    ) -> None:
        context = ModuleFrontendContext()
        frontend = Keithley6221DeltaFrontend(context)
        page = frontend.create_settings_page()
        # 实际主框架会持有两个页面；测试也必须保留状态页，避免 Qt 在状态刷新
        # 前回收其子控件，造成与真实窗口生命周期不一致的假失败。
        status_page = frontend.create_status_page()
        settings = _settings(
            channels=4,
            independent=True,
        )
        settings["shared"]["high_current"] = "1mA"
        settings["independent"]["ch2"][
            "low_current"
        ] = "-1pA"
        frontend.load_settings(settings)

        self.assertEqual(
            frontend.shared_widgets[
                "high_current"
            ].text(),
            "1m",
        )
        self.assertEqual(
            frontend.independent_widgets["ch2"][
                "low_current"
            ].text(),
            "-1p",
        )
        self.assertEqual(
            frontend.configuration_stack.currentIndex(),
            1,
        )
        self.assertEqual(
            frontend.shared_widgets[
                "digital_filter_type"
            ].count(),
            1,
        )
        self.assertEqual(
            frontend.shared_widgets[
                "digital_filter_type"
            ].currentData(),
            "moving",
        )
        saved = frontend.settings()
        self.assertEqual(
            saved["shared"]["high_current"],
            "1m",
        )
        self.assertEqual(
            saved["independent"]["ch2"][
                "low_current"
            ],
            "-1p",
        )
        actions: list[tuple[str, dict]] = []
        context.manualActionRequested.connect(
            lambda action, payload: actions.append(
                (action, payload)
            )
        )
        frontend.resource_6221.setCurrentText(
            "GPIB0::22::INSTR"
        )
        frontend.test_connection_button.click()
        self.application.processEvents()
        self.assertEqual(
            actions[-1][0],
            "test_connection",
        )
        self.assertEqual(
            actions[-1][1]["settings"][
                "resource_6221"
            ],
            "GPIB0::22::INSTR",
        )

        frontend.update_status(
            {
                "7001": "Unavailable - CH1 only",
                "State": "Initialized",
            }
        )
        self.assertTrue(
            frontend.channel_enabled["ch1"].isEnabled()
        )
        self.assertFalse(
            frontend.channel_enabled["ch2"].isEnabled()
        )
        self.assertGreaterEqual(
            page.sizeHint().width(),
            1200,
        )
        current = frontend.settings()
        self.assertNotIn("absolute_current_limit", current)
        self.assertNotIn("absolute_compliance_limit", current)
        self.assertNotIn(
            "measurement_timeout_seconds",
            current["shared"],
        )
        status_page.deleteLater()
        page.deleteLater()


class ManifestTests(unittest.TestCase):
    def test_manifest_loads_with_expected_schema(
        self,
    ) -> None:
        descriptor = load_manifest(MODULE)
        self.assertTrue(
            descriptor.valid,
            descriptor.error,
        )
        self.assertEqual(
            descriptor.id,
            "keithley_6221_2182a_delta",
        )
        self.assertEqual(
            descriptor.version,
            "0.1.0b4",
        )
        self.assertEqual(
            [column.name for column in descriptor.columns],
            [
                "Channel",
                "Resistance",
                "Current",
                "StdDev",
                "SampleCount",
                "StatusCode",
            ],
        )


if __name__ == "__main__":
    unittest.main()
