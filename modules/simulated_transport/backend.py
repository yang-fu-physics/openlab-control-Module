from __future__ import annotations

"""Hardware-free reference backend for the extension repository template."""

import random
from collections.abc import Mapping
from typing import Any

from labcontrol.measurement.api import ModuleBackend, ModuleOperationContext


class SimulatedTransportBackend(ModuleBackend):
    def __init__(self) -> None:
        self.connected = False
        self.sequence_active = False
        self.output_enabled = False
        self.desired_settings: dict[str, Any] = {}
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

    def initialize(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any]:
        self.desired_settings = {**self._defaults(), **dict(settings)}
        context.interruptible_sleep(0.08)
        self.connected = True
        status = {
            "Connection": "Connected (simulation)",
            "Applied Settings": "Not applied",
            "Sequence": "Idle",
            "Output": "Off",
            "Last Channel": "—",
            "Last Resistance (Ohm)": "—",
        }
        context.update_status(status)
        return status

    def apply_settings(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any]:
        self.desired_settings = {**self._defaults(), **dict(settings)}
        self.applied_settings = dict(self.desired_settings)
        status = {
            "Applied Settings": "Applied",
            "Excitation (mA)": self.applied_settings["excitation_current_mA"],
        }
        context.update_status(status)
        return status

    def begin_sequence(self, context: ModuleOperationContext) -> Mapping[str, Any]:
        self.sequence_active = True
        self.output_enabled = True
        status = {"Sequence": "Running", "Output": "On"}
        context.update_status(status)
        return status

    def _settings(self) -> dict[str, Any]:
        return self.applied_settings or self.desired_settings or self._defaults()

    def _resistance(self, index: int, context: ModuleOperationContext) -> float:
        temperature = float(context.system.get("temperature", {}).get("current") or 300.0)
        field_oe = float(context.system.get("field", {}).get("current") or 0.0)
        settings = self._settings()
        base = 0.05 * index + 0.003 * temperature
        magnetoresistance = 0.01 * index * (field_oe / 10_000.0) ** 2
        return base + magnetoresistance + self.random.gauss(0.0, float(settings["noise_ohm"]))

    def measure(self, context: ModuleOperationContext) -> None:
        settings = self._settings()
        threshold = float(settings["warning_threshold_ohm"])
        delay = max(0.0, float(settings["delay_seconds"]))
        for index in range(1, 5):
            context.interruptible_sleep(delay)
            channel = f"R{index}"
            value = self._resistance(index, context)
            self.last_values[channel] = value
            warning = ""
            if abs(value) > threshold:
                warning = "OVER_RANGE"
                context.warning(
                    f"{channel} exceeded the configured warning threshold",
                    "OVER_RANGE",
                    channel,
                )
            else:
                context.resolve_warning("OVER_RANGE", channel)
            context.emit_row({channel: value, "Status": "OK", "Warning": warning})
            context.update_status({
                "Last Channel": channel,
                "Last Resistance (Ohm)": value,
            })

    def end_sequence(self, reason: str, context: ModuleOperationContext) -> Mapping[str, Any]:
        self.sequence_active = False
        self.output_enabled = False
        status = {"Sequence": reason.title(), "Output": "Off"}
        context.update_status(status)
        return status

    def abort(self, context: ModuleOperationContext) -> Mapping[str, Any]:
        self.sequence_active = False
        self.output_enabled = False
        self.connected = False
        status = {"Connection": "Disconnected", "Sequence": "Idle", "Output": "Off"}
        context.update_status(status)
        return status

    def read_status(self, context: ModuleOperationContext) -> Mapping[str, Any]:
        return {
            "Connection": "Connected (simulation)" if self.connected else "Disconnected",
            "Sequence": "Running" if self.sequence_active else "Idle",
            "Output": "On" if self.output_enabled else "Off",
        }

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any]:
        del payload
        if action == "test_connection":
            status = {"Connection": "Connected (simulation)", "Last Action": "Connection test passed"}
        elif action == "measure_now":
            value = self._resistance(1, context)
            self.last_values["R1"] = value
            status = {
                "Last Action": "Manual R1 read (not written to DAT)",
                "Last Channel": "R1",
                "Last Resistance (Ohm)": value,
            }
        else:
            return super().manual_action(action, {}, context) or {}
        context.update_status(status)
        return status
