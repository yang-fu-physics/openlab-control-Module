"""An AC source module that does not own or depend on a lock-in amplifier."""

import math
import re

from labcontrol.formatting import require_precision
from labcontrol.module_api import ModuleError

from .keithley_6221 import CurrentSource, MAX_PEAK, MIN_PEAK, RMS_TO_PEAK


MIN_RMS = MIN_PEAK / RMS_TO_PEAK


def current_amperes(value):
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnuµμm]?)\s*A?",
        str(value).strip(),
    )
    if match is None:
        raise ValueError(f"Expected current, for example 100uA; got {value!r}")
    scale = {"": 1, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}
    result = float(match[1]) * scale[match[2]]
    if not math.isfinite(result):
        raise ValueError("Expected a finite current")
    return result


def frequency_hertz(value):
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(m|k|K)?\s*(?:[hH][zZ])?",
        str(value).strip(),
    )
    if match is None:
        raise ValueError(f"Expected frequency, for example 137.77 or 1kHz; got {value!r}")
    scale = {"m": 0.001, "k": 1000, "K": 1000, None: 1}
    frequency = require_precision(float(match[1]) * scale[match[2]], 3, "Frequency")
    if not 0.001 <= frequency <= 100000:
        raise ValueError("6221 frequency must be 0.001–100000 Hz")
    return frequency


def default_settings():
    return {"resource": "", "io_timeout": 0.5, "frequency": 137.77,
            "marker_line": 1, "max_current": "", "compliance": 1.0,
            "ramp_step": "", "ramp_interval": 0.2}


class Module:
    columns = {
        "Source Current": "A", "Current Magnitude": "A", "Marker Phase": "degree",
        "Source Current Squared": "A^2", "Source Frequency": "Hz", "6221 Compliance": "",
    }
    display_columns = ("Source Current", "6221 Compliance")
    sequence_commands = [{
        "id": "scan_current", "label": "Scan Current", "kind": "scan", "points_field": "points", "point_parameter": "current",
        "fields": [
            {"name": "points", "label": "RMS points (A)", "type": "list", "default": ["100u", "200u", "500u"]},
            {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
        ],
    }, {
        "id": "scan_frequency", "label": "Scan Frequency", "kind": "scan",
        "points_field": "points", "point_parameter": "frequency",
        "fields": [
            {"name": "points", "label": "Frequency points (Hz)", "type": "list", "default": [17, 137.77, "1kHz"]},
            {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
        ],
    }]

    def __init__(self, source_factory=CurrentSource):
        self.source_factory = source_factory
        self.source = None
        self.settings = None
        self.identity = ""
        self.point = None
        self.frequency = None
        self.active = False
        self.compliance = None

    def open(self, api):
        api.resources()
        return self._status()

    def configure(self, settings, api):
        settings = default_settings() | dict(settings)
        try:
            timeout = settings["io_timeout"] = require_precision(settings["io_timeout"], 1, "I/O timeout")
            if not 0.1 <= timeout <= 1 or timeout * 4 >= api.timeout:
                raise ValueError("I/O timeout must be 0.1–1.0 s and leave time for cleanup")
            settings["frequency"] = frequency_hertz(settings["frequency"])
            compliance = settings["compliance"] = require_precision(settings["compliance"], 2, "Voltage compliance")
            if not 0.1 <= compliance <= 105:
                raise ValueError("6221 voltage compliance must be 0.1–105 V")
            line = settings["marker_line"]
            if type(line) is not int or not 1 <= line <= 6:
                raise ValueError("Phase Marker line must be 1–6")
            cap = settings["cap"] = current_amperes(settings["max_current"])
            if not 0 < cap <= MAX_PEAK / RMS_TO_PEAK:
                raise ValueError("Enter a positive sample current limit within the 6221 waveform range")
            step = settings["ramp_step"] = current_amperes(settings["ramp_step"])
            if not MIN_RMS <= step <= cap:
                raise ValueError("Enter a ramp step between the minimum waveform amplitude and the sample limit")
            interval = settings["ramp_interval"] = require_precision(settings["ramp_interval"], 1, "Ramp interval")
            if interval < 0.1:
                raise ValueError("Ramp interval must be at least 0.1 s")
            address = api.resource_address(settings["resource"])
        except (TypeError, ValueError) as exc:
            raise ModuleError(str(exc), "K6221_AC_INVALID_SETTINGS") from exc

        self.close(api)
        try:
            self.source = self.source_factory(address, timeout)
            self.identity = self.source.identify()
            self._off()
        except BaseException:
            self.close(api)
            raise
        self.settings = settings
        self.frequency = settings["frequency"]
        return self._status()

    def _off(self):
        self.point = self.compliance = None
        if self.identity:
            try:
                self.source.output_off()
            except Exception as exc:
                raise ModuleError(f"6221 output off could not be confirmed: {exc}", "K6221_AC_SAFE_STATE_UNCONFIRMED") from exc
            self.point = 0.0

    def close(self, api):
        self.settings = None
        self.active = False
        try:
            self._off()
        finally:
            source = self.source
            self.source = self.point = None
            self.frequency = None
            self.identity = ""
            if source is not None:
                source.close()

    def on_event(self, event, data, api):
        if event == "run_start":
            if self.settings is None:
                raise ModuleError("Apply 6221 settings before starting SEQ", "K6221_AC_NOT_CONFIGURED")
            self.source.verify("OUTP?", 0)
            self.point = 0.0
            self.frequency = self.settings["frequency"]
            self.active = True
        elif event == "run_end":
            self.active = False
            try:
                if data["reason"] != "error" and self.point:
                    self._check_budget(abs(self.point), 0, api)
                    self._ramp(abs(self.point), MIN_RMS, api)
            finally:
                self._off()
        return self._status()

    def _require_active(self):
        if not self.active:
            raise ModuleError("Start a sequence with applied 6221 settings first", "K6221_AC_NOT_READY")

    def _check_budget(self, span, settle, api):
        timeout = self.settings["io_timeout"]
        interval = self.settings["ramp_interval"]
        steps = math.ceil(span / self.settings["ramp_step"])
        # Preserve time for readbacks and a final output-off if interrupted.
        if settle + steps * (interval + 3 * timeout) + 4 * timeout >= api.timeout:
            raise ValueError("Ramp and settle exceed the operation budget; use smaller scan increments or a longer operation timeout")

    def _ramp(self, start, target, api):
        count = math.ceil(abs(target - start) / self.settings["ramp_step"])
        for index in range(1, count + 1):
            api.sleep(self.settings["ramp_interval"])
            magnitude = target if index == count else start + (target - start) * index / count
            self.source.set_amplitude(magnitude)

    def execute_sequence_command(self, command_id, parameters, api):
        self._require_active()
        if command_id not in {"scan_current", "scan_frequency"}:
            raise ModuleError(f"Unknown 6221 command {command_id!r}", "K6221_AC_UNKNOWN_COMMAND", command_id)
        try:
            settle = require_precision(parameters["settle_seconds"], 1, "Settle time")
            if settle < 0:
                raise ValueError("Settle time must be nonnegative")
            if command_id == "scan_frequency":
                frequency = frequency_hertz(parameters["frequency"])
                self._check_budget(abs(self.point), settle + 10 * self.settings["io_timeout"], api)
                return self._set_frequency(frequency, settle, api)
            point = current_amperes(parameters["current"])
            if abs(point) > self.settings["cap"]:
                raise ValueError("Current exceeds the sample limit")
            if point and abs(point) < MIN_RMS:
                raise ValueError("Current is below the minimum 6221 waveform amplitude")
            # Check the transition and the eventual return from the requested point.
            span = abs(point - self.point)
            self._check_budget(max(span, abs(point)), settle, api)
        except (TypeError, ValueError) as exc:
            raise ModuleError(str(exc), "K6221_AC_INVALID_SCAN_POINT", command_id) from exc
        try:
            api.checkpoint()
            if point:
                phase = 180 if point < 0 else 0
                if not self.point:
                    self.source.start_wave(
                        self.frequency, self.settings["compliance"],
                        self.settings["marker_line"], phase, self.settings["cap"], api.checkpoint,
                    )
                    start = MIN_RMS
                else:
                    self.source.read_source(self.point, self.frequency)
                    start = abs(self.point)
                    if (point < 0) != (self.point < 0):
                        self._ramp(start, MIN_RMS, api)
                        self.source.set_marker(phase)
                        start = MIN_RMS
                self._ramp(start, abs(point), api)
                self.point = point
                self.compliance = None
            elif self.point:
                self._ramp(abs(self.point), MIN_RMS, api)
                self._off()
            else:
                self.source.verify("OUTP?", 0)
            api.sleep(settle)
        except BaseException:
            self._off()
            raise
        return self._status()

    def _set_frequency(self, frequency, settle, api):
        try:
            api.checkpoint()
            if self.point:
                self.source.read_source(self.point, self.frequency)
                self.source.set_frequency(frequency)
                self.source.read_source(self.point, frequency)
            else:
                self.source.verify("OUTP?", 0)
            self.frequency = frequency
            api.sleep(settle)
        except BaseException:
            self._off()
            raise
        return self._status()

    def measure(self, slot, api):
        self._require_active()
        try:
            api.checkpoint()
            magnitude, phase, frequency, compliance = self.source.read_source(self.point, self.frequency)
        except BaseException:
            self._off()
            raise
        self.compliance = compliance
        api.warn("K6221_AC_COMPLIANCE", "6221 in voltage compliance" if compliance else None)
        api.status(self._status())
        return {
            "Source Current": self.point, "Current Magnitude": magnitude, "Marker Phase": phase,
            "Source Current Squared": self.point ** 2, "Source Frequency": frequency, "6221 Compliance": compliance,
        }

    def _status(self):
        return {"State": "Running" if self.active else "Ready" if self.settings else "Not applied",
                "6221": self.identity, "Set RMS Current": self.point,
                "Set Frequency": self.frequency, "Compliance": self.compliance}
