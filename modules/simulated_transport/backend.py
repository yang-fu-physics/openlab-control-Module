from __future__ import annotations

"""无需硬件的四通道模块示例。"""

import random
from collections.abc import Mapping
from typing import Any

from labcontrol.module_api import ModuleAPI, ModuleError


STATUS_CODE_NORMAL = 0
STATUS_CODE_OVER_RANGE = 1


class SimulatedTransportBackend:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    slots = 4

    def __init__(self) -> None:
        self.connected = False
        self.sequence_active = False
        self.output_enabled = False
        self.desired_settings: dict[str, Any] = self._defaults()
        self.applied_settings: dict[str, Any] = {}
        self.last_values: dict[str, float] = {}
        self.random = random.Random("openlab-simulated-transport")

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "excitation_current_mA": 1.0,
            "delay_seconds": 0.04,
            "noise_ohm": 0.0005,
            "warning_threshold_ohm": 10.0,
        }

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        api.sleep(0.08)
        self.connected = True
        status = {
            "Connection": "Connected (simulation)",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Output": "Off",
            "Last Channel": "—",
            "Last Resistance (Ohm)": "—",
        }
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        self.desired_settings = {**self._defaults(), **dict(settings)}
        self.applied_settings = dict(self.desired_settings)
        status = {
            "Applied Settings": "Applied",
            "Excitation (mA)": self.applied_settings["excitation_current_mA"],
        }
        api.status(status)
        return status

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        if event == "run_start":
            self.sequence_active = True
            self.output_enabled = True
            status = {"Sequence": "Running", "Output": "On"}
        elif event == "run_end":
            reason = str(data.get("reason", "error"))
            self.sequence_active = False
            self.output_enabled = False
            status = {"Sequence": reason.title(), "Output": "Off"}
        elif event == "status":
            status = self._status()
        elif event == "action":
            status = self._action(
                str(data.get("name", "")),
                dict(data.get("payload", {})),
                api,
            )
        else:
            return {}
        api.status(status)
        return status

    def _settings(self) -> dict[str, Any]:
        return self.applied_settings or self.desired_settings

    def _resistance(self, index: int, api: ModuleAPI) -> float:
        devices = api.devices()
        temperature = float(devices.get("temperature", {}).get("current") or 300.0)
        field_oe = float(devices.get("field", {}).get("current") or 0.0)
        settings = self._settings()
        base = 0.05 * index + 0.003 * temperature
        magnetoresistance = 0.01 * index * (field_oe / 10_000.0) ** 2
        return base + magnetoresistance + self.random.gauss(
            0.0,
            float(settings["noise_ohm"]),
        )

    def measure(self, slot: int, api: ModuleAPI) -> Mapping[str, float | int]:
        if slot not in {1, 2, 3, 4}:
            raise ModuleError(
                "Simulated Transport received an invalid logical slot",
                "SIMULATED_LOGICAL_SLOT_INVALID",
                str(slot),
            )
        settings = self._settings()
        api.sleep(max(0.0, float(settings["delay_seconds"])))
        channel = f"R{slot}"
        value = self._resistance(slot, api)
        self.last_values[channel] = value
        if abs(value) > float(settings["warning_threshold_ohm"]):
            status_code = STATUS_CODE_OVER_RANGE
            api.warn(
                "OVER_RANGE",
                f"{channel} exceeded the configured warning threshold",
                channel,
            )
        else:
            status_code = STATUS_CODE_NORMAL
            api.warn("OVER_RANGE", None, channel)
        row: dict[str, float | int] = {"StatusCode": status_code}
        if status_code == STATUS_CODE_NORMAL:
            row[channel] = value
        api.status({
            "Last Channel": channel,
            "Last Resistance (Ohm)": (
                value if status_code == STATUS_CODE_NORMAL else "—"
            ),
        })
        return row

    def _status(self) -> dict[str, Any]:
        return {
            "Connection": "Connected (simulation)" if self.connected else "Disconnected",
            "Sequence": "Running" if self.sequence_active else "Idle",
            "Output": "On" if self.output_enabled else "Off",
        }

    def _action(
        self,
        name: str,
        _payload: Mapping[str, Any],
        api: ModuleAPI,
    ) -> dict[str, Any]:
        if name == "test_connection":
            return {
                "Connection": "Connected (simulation)",
                "Last Action": "Connection test passed",
            }
        if name == "measure_now":
            value = self._resistance(1, api)
            self.last_values["R1"] = value
            return {
                "Last Action": "Manual R1 read (not written to DAT)",
                "Last Channel": "R1",
                "Last Resistance (Ohm)": value,
            }
        raise ModuleError(
            f"Unsupported action: {name}",
            "UNSUPPORTED_ACTION",
            name,
        )

    def close(self, api: ModuleAPI) -> Mapping[str, Any]:
        self.sequence_active = False
        self.output_enabled = False
        self.connected = False
        status = {
            "Connection": "Disconnected",
            "Sequence": "Idle",
            "Output": "Off",
        }
        api.status(status)
        return status


Module = SimulatedTransportBackend
