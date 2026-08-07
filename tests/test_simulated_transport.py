from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "simulated_transport"
sys.path.insert(0, str(CORE / "src"))

from labcontrol.extensions.loading import load_source_object  # noqa: E402
from module_contract import (  # noqa: E402
    TestModuleAPI,
    measure_module,
    module_slots,
    open_module,
    read_status,
    run_action,
    run_end,
    run_start,
)
SimulatedTransportBackend = load_source_object(
    MODULE,
    "backend:SimulatedTransportBackend",
    "test_simulated_transport_backend",
)


class SimulatedTransportTests(unittest.TestCase):
    def test_lifecycle_emits_four_rows_and_turns_output_off(self) -> None:
        messages: list[tuple[str, dict]] = []
        context = TestModuleAPI(
            {
                "temperature": {"current": 300.0},
                "field": {"current": 0.0},
            },
            lambda kind, values: messages.append((kind, values)),
        )
        backend = SimulatedTransportBackend()
        open_module(backend, context)
        backend.configure(
            {
                "delay_seconds": 0.0,
                "noise_ohm": 0.0,
                "warning_threshold_ohm": 1e9,
            },
            context,
        )
        run_start(backend, context)
        self.assertEqual(
            module_slots(backend),
            (1, 2, 3, 4),
        )
        for slot in range(1, 5):
            measure_module(backend, context, slot)
        final = run_end(backend, "completed", context)

        rows = [payload["values"] for kind, payload in messages if kind == "row"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [next(name for name in row if name.startswith("R")) for row in rows],
            ["R1", "R2", "R3", "R4"],
        )
        self.assertEqual(
            [row["StatusCode"] for row in rows],
            [0, 0, 0, 0],
        )
        self.assertTrue(
            all(
                not isinstance(value, str)
                for row in rows
                for value in row.values()
            )
        )
        self.assertEqual(final["Output"], "Off")

    def test_threshold_warning_uses_module_specific_numeric_status(self) -> None:
        messages: list[tuple[str, dict]] = []
        context = TestModuleAPI(
            {
                "temperature": {"current": 300.0},
                "field": {"current": 0.0},
            },
            lambda kind, values: messages.append((kind, values)),
        )
        backend = SimulatedTransportBackend()
        open_module(backend, context)
        backend.configure(
            {
                "delay_seconds": 0.0,
                "noise_ohm": 0.0,
                "warning_threshold_ohm": 0.0,
            },
            context,
        )

        for slot in range(1, 5):
            measure_module(backend, context, slot)

        rows = [
            payload["values"]
            for kind, payload in messages
            if kind == "row"
        ]
        self.assertEqual(
            [row["StatusCode"] for row in rows],
            [1, 1, 1, 1],
        )
        self.assertTrue(
            all(
                set(row) == {"StatusCode"}
                for row in rows
            )
        )
        self.assertTrue(
            all(
                not isinstance(value, str)
                for row in rows
                for value in row.values()
            )
        )


if __name__ == "__main__":
    unittest.main()
