from __future__ import annotations

from typing import Any


FREQUENCIES_HZ: tuple[tuple[int, float], ...] = (
    (1, 9.8),
    (2, 13.7),
    (3, 16.2),
    (4, 11.6),
    (5, 18.2),
)

CURRENT_EXCITATIONS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "1.00 pA", 1.00e-12),
    (2, "3.16 pA", 3.16e-12),
    (3, "10.0 pA", 10.0e-12),
    (4, "31.6 pA", 31.6e-12),
    (5, "100 pA", 100e-12),
    (6, "316 pA", 316e-12),
    (7, "1.00 nA", 1.00e-9),
    (8, "3.16 nA", 3.16e-9),
    (9, "10.0 nA", 10.0e-9),
    (10, "31.6 nA", 31.6e-9),
    (11, "100 nA", 100e-9),
    (12, "316 nA", 316e-9),
    (13, "1.00 uA", 1.00e-6),
    (14, "3.16 uA", 3.16e-6),
    (15, "10.0 uA", 10.0e-6),
    (16, "31.6 uA", 31.6e-6),
    (17, "100 uA", 100e-6),
    (18, "316 uA", 316e-6),
    (19, "1.00 mA", 1.00e-3),
    (20, "3.16 mA", 3.16e-3),
    (21, "10.0 mA", 10.0e-3),
    (22, "31.6 mA", 31.6e-3),
)

VOLTAGE_EXCITATIONS: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "2.00 uV", 2.00e-6),
    (2, "6.32 uV", 6.32e-6),
    (3, "20.0 uV", 20.0e-6),
    (4, "63.2 uV", 63.2e-6),
    (5, "200 uV", 200e-6),
    (6, "632 uV", 632e-6),
    (7, "2.00 mV", 2.00e-3),
    (8, "6.32 mV", 6.32e-3),
    (9, "20.0 mV", 20.0e-3),
    (10, "63.2 mV", 63.2e-3),
    (11, "200 mV", 200e-3),
    (12, "632 mV", 632e-3),
)

RESISTANCE_RANGES: tuple[
    tuple[int, str, float],
    ...,
] = (
    (1, "2.00 mOhm", 2.00e-3),
    (2, "6.32 mOhm", 6.32e-3),
    (3, "20.0 mOhm", 20.0e-3),
    (4, "63.2 mOhm", 63.2e-3),
    (5, "200 mOhm", 200e-3),
    (6, "632 mOhm", 632e-3),
    (7, "2.00 Ohm", 2.00),
    (8, "6.32 Ohm", 6.32),
    (9, "20.0 Ohm", 20.0),
    (10, "63.2 Ohm", 63.2),
    (11, "200 Ohm", 200.0),
    (12, "632 Ohm", 632.0),
    (13, "2.00 kOhm", 2.00e3),
    (14, "6.32 kOhm", 6.32e3),
    (15, "20.0 kOhm", 20.0e3),
    (16, "63.2 kOhm", 63.2e3),
    (17, "200 kOhm", 200e3),
    (18, "632 kOhm", 632e3),
    (19, "2.00 MOhm", 2.00e6),
    (20, "6.32 MOhm", 6.32e6),
    (21, "20.0 MOhm", 20.0e6),
    (22, "63.2 MOhm", 63.2e6),
)

STATUS_BITS: tuple[tuple[int, str], ...] = (
    (1, "CS_OVL"),
    (2, "VCM_OVL"),
    (4, "VMIX_OVL"),
    (8, "VDIF_OVL"),
    (16, "R_OVER"),
    (32, "R_UNDER"),
    (64, "T_OVER"),
    (128, "T_UNDER"),
)


def default_channel(slot: int) -> dict[str, Any]:
    return {
        "enabled": slot == 1,
        "input_channel": slot,
        "excitation_mode": "current",
        "excitation_range": 5,
        "autorange": 1,
        "resistance_range": 17,
    }


def default_settings() -> dict[str, Any]:
    return {
        "resource": "",
        "frequency_index": 2,
        "pause_seconds": 3,
        "dwell_seconds": 10,
        "filter_enabled": False,
        "filter_settle_seconds": 10,
        "filter_window_percent": 40,
        "io_timeout_seconds": 3.0,
        "retry_attempts": 2,
        "shunt_after_read": True,
        "channels": {
            f"r{slot}": default_channel(slot)
            for slot in range(1, 5)
        },
    }


__all__ = [
    "CURRENT_EXCITATIONS",
    "FREQUENCIES_HZ",
    "RESISTANCE_RANGES",
    "STATUS_BITS",
    "VOLTAGE_EXCITATIONS",
    "default_channel",
    "default_settings",
]
