"""An AC source module that does not own or depend on a lock-in amplifier."""

import math
import re
from time import monotonic, sleep

from labcontrol.formatting import require_precision
from labcontrol.module_api import ModuleError

from .keithley_6221 import CurrentSource, MAX_PEAK, MIN_PEAK, RMS_TO_PEAK, fixed_range


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
        "id": "scan_current", "label": "Scan Current", "kind": "scan",
        "points_field": "points", "point_parameter": "current",
        "scan_modes": ["Linear", "Log", "List"],
        "point_unit": "A",
        "point_limits": {
            "setting": "max_current",
            "symmetric": True,
            "minimum": -MAX_PEAK / RMS_TO_PEAK,
            "maximum": MAX_PEAK / RMS_TO_PEAK,
        },
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
    }, {
        "id": "set_current", "label": "Set Current", "kind": "command",
        "description": "Ramp to the RMS current and hold it for subsequent commands. Zero ramps down and turns output off.",
        "point_unit": "A",
        "point_limits": {
            "field": "current",
            "setting": "max_current",
            "symmetric": True,
            "minimum": -MAX_PEAK / RMS_TO_PEAK,
            "maximum": MAX_PEAK / RMS_TO_PEAK,
        },
        "fields": [
            {"name": "current", "label": "RMS current (A)", "type": "text", "default": "100u"},
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
            if not MIN_RMS <= cap <= MAX_PEAK / RMS_TO_PEAK:
                raise ValueError("Enter a positive sample current limit within the 6221 waveform range")
            # Armed amplitude changes are clamped to 0.1% of the fixed range.
            settings["minimum_current"] = fixed_range(cap) * 0.001 / RMS_TO_PEAK
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

    @staticmethod
    def _active_now(api):
        """返回扣除核心已确认 Pause 时长后的活动单调时钟。"""

        paused = getattr(api, "paused_seconds", None)
        if (
            isinstance(paused, (int, float))
            and not isinstance(paused, bool)
            and math.isfinite(paused)
            and paused >= 0.0
        ):
            return monotonic() - float(paused)
        clock = getattr(api, "active_monotonic", None)
        if callable(clock):
            return float(clock())
        return monotonic()

    def close(self, api):
        deadline = self._active_now(api) + api.timeout
        self.active = False
        try:
            self._ramp_off(api, deadline=deadline)
        finally:
            self.settings = None
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
            api.warn("K6221_AC_RAMP_TRUNCATED", None)
        elif event == "run_end":
            deadline = self._active_now(api) + api.timeout
            self.active = False
            self._ramp_off(api, deadline=deadline)
        return self._status()

    def _require_active(self):
        if not self.active:
            raise ModuleError("Start a sequence with applied 6221 settings first", "K6221_AC_NOT_READY")

    def _ramp_steps(self, start, target):
        return math.ceil(abs(target - start) / self.settings["ramp_step"])

    def _check_budget(self, steps, settle, api, *, extra_io=4):
        timeout = self.settings["io_timeout"]
        interval = self.settings["ramp_interval"]
        # Preserve time for readbacks, interrupted ramp-down, and final output-off.
        if settle + steps * (interval + 3 * timeout) + extra_io * timeout + 0.5 >= api.timeout:
            raise ValueError("Ramp and settle exceed the operation budget; use smaller scan increments or a longer operation timeout")

    def _ramp_off(self, api, *, deadline=None):
        truncated = False
        try:
            if self.settings is not None and self.identity:
                timeout = self.settings["io_timeout"]
                off_reserve = 3 * timeout + 0.4
                if deadline is None:
                    deadline = self._active_now(api) + api.timeout
                if self._active_now(api) + 2 * timeout + off_reserve >= deadline:
                    truncated = self.point != 0.0
                else:
                    output = self.source.number("OUTP?")
                    if output not in (0, 1):
                        raise ModuleError("Cannot determine 6221 output state before ramp-down", "K6221_AC_BAD_REPLY")
                    if output:
                        peak = self.source.number("SOUR:WAVE:AMPL?")
                        if peak < MIN_PEAK * (1 - 5e-6) or peak > MAX_PEAK * (1 + 5e-6):
                            raise ModuleError("Invalid 6221 amplitude before ramp-down", "K6221_AC_BAD_REPLY")
                        start = min(max(peak, MIN_PEAK), MAX_PEAK) / RMS_TO_PEAK
                        # Read the actual amplitude: cancellation may interrupt a ramp
                        # between measurement points. Never ramp up during cleanup.
                        if start > self.settings["minimum_current"]:
                            truncated = not self._ramp(
                                start,
                                self.settings["minimum_current"],
                                api,
                                stopping=True,
                                deadline=deadline - off_reserve,
                            )
        finally:
            self._off()
        if truncated:
            api.warn(
                "K6221_AC_RAMP_TRUNCATED",
                "6221 ramp-down was shortened to finish output-off within the operation timeout",
            )

    def _cleanup_after_failure(self, exc, api, deadline):
        try:
            self._ramp_off(api, deadline=deadline)
        except Exception as cleanup_exc:
            is_cancel = (
                not isinstance(exc, Exception)
                or isinstance(exc, InterruptedError)
                or type(exc).__name__ == "_ModuleOperationCancelled"
            )
            is_unconfirmed = (
                isinstance(cleanup_exc, ModuleError)
                and cleanup_exc.code == "K6221_AC_SAFE_STATE_UNCONFIRMED"
            )
            if is_unconfirmed or is_cancel:
                raise cleanup_exc from exc
            if isinstance(exc, ModuleError):
                detail = str(cleanup_exc) or type(cleanup_exc).__name__
                raise ModuleError(
                    f"{exc} (ramp-down skipped: {detail}; output off confirmed)",
                    exc.code,
                    exc.context,
                ) from cleanup_exc
            raise exc from cleanup_exc

    _sleep = staticmethod(sleep)

    def _ramp(self, start, target, api, *, stopping=False, deadline=None):
        count = self._ramp_steps(start, target)
        interval = self.settings["ramp_interval"]
        step_io = 3 * self.settings["io_timeout"]
        for index in range(1, count + 1):
            if stopping:
                if deadline is not None and self._active_now(api) + interval + step_io >= deadline:
                    return False
                # Stop is already requested; another cancellation checkpoint must
                # not interrupt the ramp-down that makes the output safe.
                self._sleep(interval)
            else:
                api.sleep(interval)
            magnitude = target if index == count else start + (target - start) * index / count
            self.source.set_amplitude(magnitude)
        return True

    def execute_sequence_command(self, command_id, parameters, api):
        self._require_active()
        deadline = self._active_now(api) + api.timeout
        if command_id not in {"set_current", "scan_current", "scan_frequency"}:
            raise ModuleError(f"Unknown 6221 command {command_id!r}", "K6221_AC_UNKNOWN_COMMAND", command_id)
        try:
            settle = require_precision(parameters["settle_seconds"], 1, "Settle time")
            if settle < 0:
                raise ValueError("Settle time must be nonnegative")
            if command_id == "scan_frequency":
                frequency = frequency_hertz(parameters["frequency"])
                return_steps = (
                    self._ramp_steps(abs(self.point), self.settings["minimum_current"])
                    if self.point
                    else 0
                )
                self._check_budget(return_steps, settle, api, extra_io=14)
                return self._set_frequency(frequency, settle, api, deadline)
            point = current_amperes(parameters["current"])
            if abs(point) > self.settings["cap"]:
                raise ValueError("Current exceeds the sample limit")
            min_current = self.settings["minimum_current"]
            if point and abs(point) < min_current:
                raise ValueError(
                    f"Current is below the minimum waveform amplitude ({min_current:.4g} A RMS) on the fixed range"
                )
            current = abs(self.point or 0.0)
            target = abs(point)
            if point and not self.point:
                forward_steps = self._ramp_steps(min_current, target)
            elif point and (point < 0) != (self.point < 0):
                forward_steps = self._ramp_steps(current, min_current) + self._ramp_steps(min_current, target)
            elif point:
                forward_steps = self._ramp_steps(current, target)
            elif self.point:
                forward_steps = self._ramp_steps(current, min_current)
            else:
                forward_steps = 0
            # Include both the transition and a full ramp-down if interrupted before completion.
            cleanup_steps = self._ramp_steps(max(current, target), min_current) if point else 0
            self._check_budget(forward_steps + cleanup_steps, settle, api, extra_io=8)
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
                    start = min_current
                else:
                    self.source.read_source(self.point, self.frequency)
                    start = abs(self.point)
                    if (point < 0) != (self.point < 0):
                        self._ramp(start, min_current, api)
                        self.source.set_marker(phase)
                        start = min_current
                self._ramp(start, abs(point), api)
                self.point = point
                self.compliance = None
            elif self.point:
                self._ramp(abs(self.point), min_current, api)
                self._off()
            else:
                self.source.verify("OUTP?", 0)
            api.sleep(settle)
        except BaseException as exc:
            self._cleanup_after_failure(exc, api, deadline)
            raise
        return self._status()

    def _set_frequency(self, frequency, settle, api, deadline):
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
        except BaseException as exc:
            self._cleanup_after_failure(exc, api, deadline)
            raise
        return self._status()

    def measure(self, slot, api):
        self._require_active()
        deadline = self._active_now(api) + api.timeout
        try:
            api.checkpoint()
            magnitude, phase, frequency, compliance = self.source.read_source(self.point, self.frequency)
        except BaseException as exc:
            self._cleanup_after_failure(exc, api, deadline)
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
