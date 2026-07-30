from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
MODULE = ROOT / "modules" / "simulated_transport"
sys.path.insert(0, str(CORE / "src"))

from labcontrol.extensions.loading import load_source_object  # noqa: E402
from labcontrol.measurement.api import ModuleOperationContext  # noqa: E402

SimulatedTransportBackend = load_source_object(
    MODULE,
    "backend:SimulatedTransportBackend",
    "test_simulated_transport_backend",
)


class SimulatedTransportTests(unittest.TestCase):
    def test_lifecycle_emits_four_rows_and_turns_output_off(self) -> None:
        messages: list[tuple[str, dict]] = []
        context = ModuleOperationContext(
            {
                "temperature": {"current": 300.0},
                "field": {"current": 0.0},
            },
            lambda kind, values: messages.append((kind, values)),
        )
        backend = SimulatedTransportBackend()
        backend.initialize({"delay_seconds": 0.0}, context)
        backend.apply_settings(
            {
                "delay_seconds": 0.0,
                "noise_ohm": 0.0,
                "warning_threshold_ohm": 1e9,
            },
            context,
        )
        backend.begin_sequence(context)
        backend.measure(context)
        final = backend.end_sequence("completed", context)

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
        context = ModuleOperationContext(
            {
                "temperature": {"current": 300.0},
                "field": {"current": 0.0},
            },
            lambda kind, values: messages.append((kind, values)),
        )
        backend = SimulatedTransportBackend()
        backend.initialize({"delay_seconds": 0.0}, context)
        backend.apply_settings(
            {
                "delay_seconds": 0.0,
                "noise_ohm": 0.0,
                "warning_threshold_ohm": 0.0,
            },
            context,
        )

        backend.measure(context)

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
