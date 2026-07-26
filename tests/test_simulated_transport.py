from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "simulated_transport"
sys.path.insert(0, str(MODULE))

from backend import SimulatedTransportBackend  # noqa: E402
from labcontrol.measurement.api import ModuleOperationContext  # noqa: E402


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
        self.assertEqual(final["Output"], "Off")


if __name__ == "__main__":
    unittest.main()
