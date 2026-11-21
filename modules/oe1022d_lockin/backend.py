"""One DAT row per Measure, containing both OE1022D channels and both harmonics."""

import math
import re
from time import monotonic

from labcontrol.formatting import require_precision
from labcontrol.module_api import ModuleError

from .oe1022d import CHANNEL_SETTINGS, FILTER_WAIT_MULTIPLIERS, SENSITIVITY_VOLTS, TIME_CONSTANT_SECONDS, LockIn


EXTERNAL = "external"
SINE_OUT = "sine_out"
CHANNELS = ("channel_1", "channel_2")
_QUANTITY = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnuµμmk]?)\s*V?")
_SCALE = {"": 1, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3}


def voltage(value):
    match = _QUANTITY.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"Expected voltage, for example 10mV; got {value!r}")
    result = float(match[1]) * _SCALE[match[2]]
    if not math.isfinite(result):
        raise ValueError("Expected a finite voltage")
    return result


def default_settings():
    return {
        "mode": EXTERNAL, "lockin_resource": "", "baud": 921600,
        "io_timeout": 0.5, "frequency": 137.77, "sine_channel": 1,
        "amplitude": "0", "max_voltage": "",
        "settle_seconds": 3.0,
        "channel_2_reference": "external",
        **{
            name: {**{key: default for key, _, _, _, default in CHANNEL_SETTINGS}, "phase": 0.0, "primary": 1, "secondary": 2}
            for name in CHANNELS
        },
    }


class Module:
    columns = {
        "X1p": "V", "Y1p": "V", "R1p": "V", "Theta1p": "degree",
        "Harmonic1p": "", "Frequency1p": "Hz",
        "X1s": "V", "Y1s": "V", "R1s": "V", "Theta1s": "degree",
        "Harmonic1s": "", "Frequency1s": "Hz",
        "Status1": "", "Y1s/X1p": "", "Y1s/X1p^2": "1/V",
        "X2p": "V", "Y2p": "V", "R2p": "V", "Theta2p": "degree",
        "Harmonic2p": "", "Frequency2p": "Hz",
        "X2s": "V", "Y2s": "V", "R2s": "V", "Theta2s": "degree",
        "Harmonic2s": "", "Frequency2s": "Hz",
        "Status2": "", "Y2s/X1p": "", "Y2s/X1p^2": "1/V",
        "Source Voltage": "V", "X1p Squared": "V^2", "X2p Squared": "V^2",
    }
    display_columns = ("X1p", "Y2s", "Source Voltage")
    sequence_commands = [{
        "id": "scan_voltage", "label": "Scan Voltage", "kind": "scan", "points_field": "points", "point_parameter": "voltage",
        "fields": [
            {"name": "points", "label": "RMS points (V)", "type": "list", "default": ["10m", "20m", "50m"]},
            {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 3.0, "minimum": 0.0, "unit": "s"},
        ],
    }, {
        "id": "auto_range", "label": "Auto Range", "kind": "command",
        "description": "Search sensitivity in software at the unchanged time constant and slope, then optionally lower dynamic reserve. Keep selected settings for subsequent measurements.",
        "fields": [
            {"name": "auto_reserve", "label": "Optimize dynamic reserve after ranging", "type": "bool", "default": True},
        ],
    }]

    def __init__(self, lockin_factory=LockIn):
        self.lockin_factory = lockin_factory
        self.lockin = None
        self.voltage_channel = None
        self.settings = None
        self.point = None
        self.active = False
        self.identity = ""
        self.latest = {}
        self.auto_ranged = False

    def open(self, api):
        api.resources()
        return self._status()

    def configure(self, settings, api):
        settings = default_settings() | dict(settings)
        try:
            mode = settings["mode"]
            if mode not in (EXTERNAL, SINE_OUT):
                raise ValueError("Select external reference or OE1022D Sine Out")
            timeout = require_precision(settings["io_timeout"], 1, "I/O timeout")
            if not 0.1 <= timeout <= 1 or timeout * 4 >= api.timeout:
                raise ValueError("I/O timeout must be 0.1–1.0 s and leave time for cleanup")
            settings["io_timeout"] = timeout
            frequency = require_precision(settings["frequency"], 3, "Frequency")
            if not 0.001 <= frequency <= 102000:
                raise ValueError("Reference frequency is out of range")
            settings["frequency"] = frequency
            if mode == SINE_OUT and (type(settings["sine_channel"]) is not int or settings["sine_channel"] not in (1, 2)):
                raise ValueError("Select Sine Out A or B")
            if settings["channel_2_reference"] not in ("external", "channel_1"):
                raise ValueError("Select external or Channel-A reference for channel 2")
            if mode == SINE_OUT and settings["sine_channel"] == 2 and settings["channel_2_reference"] == "channel_1":
                raise ValueError("Use Sine Out A when channel 2 follows Channel-A reference")
            if settings["baud"] not in (921600, 460800, 230400, 115200, 57600, 38400, 19200, 9600):
                raise ValueError("Select a supported baud rate")
            if mode == SINE_OUT:
                cap = voltage(settings["max_voltage"])
                if not 0 < cap <= 5:
                    raise ValueError("Enter a positive sample voltage limit up to 5 V RMS")
                settings["cap"] = cap
                settings["amplitude"] = self._validate_point(settings, settings["amplitude"])
                settings["settle_seconds"] = self._settle(settings["settle_seconds"], timeout, api)
            for name in CHANNELS:
                channel = settings[name] = dict(settings[name])
                for key, _, label, options, _ in CHANNEL_SETTINGS:
                    if type(channel[key]) is not int or not 0 <= channel[key] < len(options):
                        raise ValueError(f"Invalid {label}")
                phase = require_precision(channel["phase"], 2, "Reference phase")
                if not -180 <= phase <= 180:
                    raise ValueError("Reference phase must be between −180° and 180°")
                channel["phase"] = phase
                if channel["sync"] and (frequency >= 200 or channel["slope"] < 2):
                    raise ValueError("Synchronous filtering requires <200 Hz and an 18 or 24 dB/oct slope")
                for key in ("primary", "secondary"):
                    order = channel[key]
                    if type(order) is not int or not 1 <= order <= 32767 or order * frequency > 102000:
                        raise ValueError("Harmonic order must be 1–32767 and harmonic frequency ≤102 kHz")
            lockin_address = api.resource_address(settings["lockin_resource"])
            if not lockin_address.upper().startswith("ASRL"):
                raise ValueError("Select the OE1022D USB virtual COM or RS-232 VISA resource (ASRL)")
        except (KeyError, TypeError, ValueError) as exc:
            raise ModuleError(str(exc), "LOCKIN_INVALID_SETTINGS") from exc

        self._release()
        try:
            self.lockin = self.lockin_factory(lockin_address, timeout, settings["baud"])
            self.identity = self.lockin.identify()
            if mode == SINE_OUT:
                self.voltage_channel = settings["sine_channel"]
                self.lockin.zero(self.voltage_channel)
            for i, name in enumerate(CHANNELS, 1):
                reference = 3 if i == 2 and settings["channel_2_reference"] == "channel_1" else 1 if i == self.voltage_channel else 0
                self.lockin.configure_channel(i, settings[name], reference, frequency, api.checkpoint)
        except BaseException:
            # Cancellation must also leave any identified source in a confirmed safe state.
            self._release()
            raise
        self.settings = settings
        return self._status()

    @staticmethod
    def _validate_point(settings, value):
        point = voltage(value)
        if not 0 <= point <= settings["cap"]:
            raise ValueError("Voltage exceeds the configured sample limit or is negative")
        return require_precision(point, 3, "OE1022D RMS voltage")

    @staticmethod
    def _settle(value, io_timeout, api):
        seconds = require_precision(value, 1, "Settle time")
        if not 0 <= seconds < api.timeout - 4 * io_timeout:
            raise ValueError("Settle time leaves insufficient time for source cleanup")
        return seconds

    def on_event(self, event, data, api):
        if event == "run_start":
            if self.settings is None:
                raise ModuleError("Apply instrument settings before starting SEQ", "LOCKIN_NOT_CONFIGURED")
            self.auto_ranged = False
            self.active = True
        elif event == "run_end":
            self.active = False
            self.auto_ranged = False
            self._zero()
        return self._status()

    def _zero(self):
        self.point = None
        try:
            if self.voltage_channel is not None:
                self.lockin.zero(self.voltage_channel)
        except Exception as exc:
            raise ModuleError(f"Source zero/off could not be confirmed: {exc}", "LOCKIN_SAFE_STATE_UNCONFIRMED") from exc

    def _release(self):
        self.settings = None
        self.active = False
        self.auto_ranged = False
        try:
            self._zero()
        finally:
            lockin = self.lockin
            self.lockin = self.voltage_channel = None
            self.identity = ""
            self.latest.clear()
            if lockin is not None:
                lockin.close()

    def close(self, api):
        self._release()

    def _set_point(self, point, settle, api):
        try:
            api.checkpoint()
            if point == 0:
                self._zero()
            else:
                self.lockin.source_voltage(self.voltage_channel, point)
            self.point = point
            api.sleep(settle)
        except BaseException:
            self._zero()
            raise

    def _instrument_auto_range(self, parameters, api):
        """Preserved native implementation; the SEQ dispatcher does not call it."""
        self._require_active()
        try:
            reserve_first = parameters["auto_reserve"]
            if type(reserve_first) is not bool:
                raise ValueError("Auto Reserve first must be a boolean")
            wait = require_precision(parameters["settle_seconds"], 1, "Auto Range wait")
            operations = 2 * (1 + reserve_first)
            timeout = self.settings["io_timeout"]
            if wait < 5 or operations * (wait + 3 * timeout) + 14 * timeout >= api.timeout:
                raise ValueError("Auto Range needs at least 5 s per operation and must leave time for readback and cleanup")
        except (TypeError, ValueError) as exc:
            raise ModuleError(str(exc), "LOCKIN_INVALID_AUTO_RANGE") from exc
        options = {key: values for key, _, _, values, _ in CHANNEL_SETTINGS}
        self.auto_ranged = False
        try:
            reference_channels = (1,) if self.settings["channel_2_reference"] == "channel_1" else (1, 2)
            for channel in reference_channels:
                if self.lockin.status_code(channel, channel != self.voltage_channel) & 16:
                    raise ModuleError(f"Channel {channel}: Auto Range requires a locked reference", "LOCKIN_AUTO_RANGE_UNLOCKED")
            for channel, name in enumerate(CHANNELS, 1):
                if reserve_first:
                    api.checkpoint()
                    self.lockin.auto_reserve(channel)
                    api.sleep(wait)
                    self.lockin.read_gain(channel)
                api.checkpoint()
                self.lockin.auto_gain(channel)
                api.sleep(wait)
                reserve, sensitivity = self.lockin.read_gain(channel)
                self.settings[name].update(reserve=reserve, sensitivity=sensitivity)
                self.latest.update({
                    f"Dynamic Reserve {channel}": options["reserve"][reserve],
                    f"Sensitivity {channel}": options["sensitivity"][sensitivity],
                })
            for channel, name in enumerate(CHANNELS, 1):
                if self.lockin.read_gain(channel) != (self.settings[name]["reserve"], self.settings[name]["sensitivity"]):
                    raise ModuleError("Automatic settings are still changing; extend the wait before scanning", "LOCKIN_AUTO_RANGE_UNSETTLED")
            self.auto_ranged = True
        except BaseException:
            self._zero()
            raise
        return self._status()

    def _check_range(self, channel, wait, deadline, api):
        if monotonic() + wait + 8 * self.settings["io_timeout"] >= deadline:
            raise ModuleError(
                f"Channel {channel}: insufficient operation time for the unchanged "
                f"{wait:.1f} s filter settling wait; increase [modules] "
                "operation_timeout_seconds in configs/general.toml",
                "LOCKIN_AUTO_RANGE_TIMEOUT",
            )
        api.sleep(wait)
        shared = channel == 2 and self.settings["channel_2_reference"] == "channel_1"
        status = self.lockin.status_code(channel, channel != self.voltage_channel and not shared)
        if shared:
            status |= self.lockin.status_code(1, self.voltage_channel != 1) & 16
        if status & 16:
            raise ModuleError(f"Channel {channel}: Auto Range requires a locked reference", "LOCKIN_AUTO_RANGE_UNLOCKED")
        if status & 2:
            raise ModuleError(f"Channel {channel}: input overload; reduce the input signal", "LOCKIN_AUTO_RANGE_INPUT_OVERLOAD")
        if status & 4:
            return None
        magnitudes = [self.lockin.read_harmonic(channel, index)[2] for index in (1, 2)]
        if any(value < 0 for value in magnitudes):
            raise ModuleError(f"Channel {channel}: negative harmonic magnitude", "LOCKIN_BAD_REPLY")
        return max(magnitudes)

    def execute_sequence_command(self, command_id, parameters, api):
        self._require_active()
        if command_id == "auto_range":
            optimize_reserve = parameters["auto_reserve"]
            if type(optimize_reserve) is not bool:
                raise ModuleError("Optimize dynamic reserve must be a boolean", "LOCKIN_INVALID_AUTO_RANGE")
            self.auto_ranged = False
            deadline = monotonic() + api.timeout - 4 * self.settings["io_timeout"] - 0.4
            options = {key: values for key, _, _, values, _ in CHANNEL_SETTINGS}
            filters = {}
            waits = {}
            try:
                reference_channels = (1,) if self.settings["channel_2_reference"] == "channel_1" else (1, 2)
                for channel in reference_channels:
                    api.checkpoint()
                    if self.lockin.status_code(channel, channel != self.voltage_channel) & 16:
                        raise ModuleError(f"Channel {channel}: Auto Range requires a locked reference", "LOCKIN_AUTO_RANGE_UNLOCKED")
                for channel in (1, 2):
                    time_constant, slope = filters[channel] = self.lockin.read_filter(channel)
                    waits[channel] = TIME_CONSTANT_SECONDS[time_constant] * FILTER_WAIT_MULTIPLIERS[slope]
                min_checks_per_channel = 3 + (2 if optimize_reserve else 0)
                min_required_wait = sum(waits[channel] * min_checks_per_channel for channel in (1, 2))
                available_wait = deadline - monotonic() - 8 * self.settings["io_timeout"]
                if min_required_wait >= available_wait:
                    recommended_timeout = math.ceil(
                        min_required_wait + 12 * self.settings["io_timeout"] + 5.0
                    )
                    raise ModuleError(
                        f"Auto Range needs at least {min_required_wait:.1f} s of unchanged "
                        f"filter settling (Ch1 {waits[1]:.1f} s/check, Ch2 {waits[2]:.1f} s/check), "
                        f"exceeding operation_timeout_seconds ({api.timeout:.1f} s); increase "
                        f"[modules] operation_timeout_seconds to at least {recommended_timeout} s "
                        "in configs/general.toml",
                        "LOCKIN_AUTO_RANGE_TIMEOUT",
                    )
                for channel, name in enumerate(CHANNELS, 1):
                    wait = waits[channel]
                    _, sensitivity = self.lockin.read_gain(channel)
                    api.checkpoint()
                    self.lockin.set_reserve(channel, 2)
                    floor = 0
                    last_safe = None
                    confirmed = None
                    for _ in range(10):
                        magnitude = self._check_range(channel, wait, deadline, api)
                        if magnitude is None:
                            floor = max(floor, sensitivity + 1)
                            if sensitivity == len(SENSITIVITY_VOLTS) - 1:
                                raise ModuleError(f"Channel {channel}: gain overload at maximum range", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                            if last_safe is None:
                                target = next((index for index in range(sensitivity + 1, len(SENSITIVITY_VOLTS)) if SENSITIVITY_VOLTS[index] >= 10 * SENSITIVITY_VOLTS[sensitivity]), len(SENSITIVITY_VOLTS) - 1)
                            else:
                                target = sensitivity + 1
                        else:
                            last_safe = sensitivity
                            occupancy = magnitude / SENSITIVITY_VOLTS[sensitivity]
                            if 0.2 <= occupancy <= 0.8:
                                if confirmed == sensitivity:
                                    break
                                confirmed = sensitivity
                                continue
                            if occupancy > 0.8:
                                if sensitivity == len(SENSITIVITY_VOLTS) - 1:
                                    raise ModuleError(f"Channel {channel}: signal exceeds 80% of maximum range", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                                target = sensitivity + 1
                            else:
                                candidates = [index for index in range(floor, len(SENSITIVITY_VOLTS)) if 0.2 <= magnitude / SENSITIVITY_VOLTS[index] <= 0.8]
                                if not candidates:
                                    raise ModuleError(f"Channel {channel}: no safe range meets 20–80% occupancy", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                                target = min(candidates, key=lambda index: abs(magnitude / SENSITIVITY_VOLTS[index] - 0.55))
                        confirmed = None
                        api.checkpoint()
                        self.lockin.set_sensitivity(channel, target)
                        sensitivity = target
                    else:
                        raise ModuleError(f"Channel {channel}: range search exceeded 10 checks", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                    reserve = 2
                    if optimize_reserve:
                        for candidate in (1, 0):
                            api.checkpoint()
                            self.lockin.set_reserve(channel, candidate)
                            magnitude = self._check_range(channel, wait, deadline, api)
                            if magnitude is None or not 0.2 <= magnitude / SENSITIVITY_VOLTS[sensitivity] <= 0.8:
                                self.lockin.set_reserve(channel, reserve)
                                magnitude = self._check_range(channel, wait, deadline, api)
                                if magnitude is None or not 0.2 <= magnitude / SENSITIVITY_VOLTS[sensitivity] <= 0.8:
                                    raise ModuleError(f"Channel {channel}: previous safe reserve no longer passes", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                                break
                            reserve = candidate
                    self.settings[name].update(reserve=reserve, sensitivity=sensitivity)
                    self.latest.update({
                        f"Dynamic Reserve {channel}": options["reserve"][reserve],
                        f"Sensitivity {channel}": options["sensitivity"][sensitivity],
                    })
                    api.status(self._status())
                for channel, name in enumerate(CHANNELS, 1):
                    time_constant, slope = filters[channel]
                    if self.lockin.read_filter(channel) != filters[channel]:
                        raise ModuleError(f"Channel {channel}: time constant or slope changed during range search", "LOCKIN_AUTO_RANGE_FILTER_CHANGED")
                    sensitivity = self.settings[name]["sensitivity"]
                    expected = (self.settings[name]["reserve"], sensitivity)
                    if self.lockin.read_gain(channel) != expected:
                        raise ModuleError(f"Channel {channel}: range settings changed during search", "LOCKIN_RANGE_CHANGED_AFTER_AUTO")
                    magnitude = self._check_range(channel, waits[channel], deadline, api)
                    if magnitude is None or not 0.2 <= magnitude / SENSITIVITY_VOLTS[sensitivity] <= 0.8:
                        raise ModuleError(f"Channel {channel}: final range verification failed", "LOCKIN_AUTO_RANGE_UNCONVERGED")
                self.auto_ranged = True
            except BaseException:
                self._zero()
                raise
            return self._status()
        if command_id != "scan_voltage" or self.voltage_channel is None:
            raise ModuleError("Scan Voltage requires OE1022D Sine Out mode", "LOCKIN_WRONG_SOURCE", command_id)
        try:
            point = self._validate_point(self.settings, parameters["voltage"])
            settle = self._settle(parameters["settle_seconds"], self.settings["io_timeout"], api)
        except (TypeError, ValueError) as exc:
            raise ModuleError(str(exc), "LOCKIN_INVALID_SCAN_POINT", command_id) from exc
        self._set_point(point, settle, api)
        return self._status()

    def _require_active(self):
        if not self.active:
            raise ModuleError("Start a sequence with applied settings first", "LOCKIN_NOT_READY")

    def measure(self, slot, api):
        self._require_active()
        try:
            return self._measure(api)
        except BaseException:
            if self.point is not None:
                self._zero()
            raise

    def _measure(self, api):
        if self.voltage_channel is not None and self.point is None:
            self._set_point(self.settings["amplitude"], self.settings["settle_seconds"], api)
        row = {"Source Voltage": self.point}
        nominal = {}
        for channel, name in enumerate(CHANNELS, 1):
            settings = self.settings[name]
            api.checkpoint()
            if self.auto_ranged and self.lockin.read_gain(channel) != (settings["reserve"], settings["sensitivity"]):
                raise ModuleError(f"Channel {channel}: settings changed after Auto Range", "LOCKIN_RANGE_CHANGED_AFTER_AUTO")
            shared_reference = channel == 2 and self.settings["channel_2_reference"] == "channel_1"
            status = self.lockin.status_code(channel, channel != self.voltage_channel and not shared_reference)
            if shared_reference:
                status |= row["Status1"] & 16
            if self.auto_ranged and status:
                raise ModuleError(f"Channel {channel}: overload or reference loss after Auto Range (status {status})", "LOCKIN_RANGE_STATUS_CHANGED")
            row[f"Status{channel}"] = status
            api.warn("LOCKIN_CHANNEL_STATUS", f"Channel {channel}: status {status}" if status else None, str(channel))
            for harmonic, (role, key) in enumerate((("p", "primary"), ("s", "secondary")), 1):
                api.checkpoint()
                command = f"HARMD? {channel},{harmonic}"
                actual = self.lockin.numbers(command)[0]
                if not actual.is_integer() or not 1 <= actual <= 32767:
                    raise ModuleError(f"{command}: invalid harmonic order {actual:g}", "LOCKIN_BAD_REPLY", command)
                x, y, r, theta, base = self.lockin.read_harmonic(channel, harmonic)
                suffix = f"{channel}{role}"
                nominal[suffix] = actual == settings[key]
                api.warn(
                    "LOCKIN_HARMONIC_CHANGED",
                    None if nominal[suffix] or status else (
                        f"Channel {channel} {key}: requested harmonic {settings[key]}, instrument reports {int(actual)}"
                    ),
                    suffix,
                )
                row.update({
                    f"X{suffix}": x, f"Y{suffix}": y, f"R{suffix}": r, f"Theta{suffix}": theta,
                    f"Harmonic{suffix}": int(actual), f"Frequency{suffix}": base * actual if not status else None,
                })
            if not status and nominal[f"{channel}p"]:
                squared = row[f"X{channel}p"] ** 2
                if math.isfinite(squared):
                    row[f"X{channel}p Squared"] = squared
        if row["X1p"] and not row["Status1"] and nominal["1p"]:
            base = row["X1p"]
            base_sq = base * base
            for channel in (1, 2):
                if not row[f"Status{channel}"] and nominal[f"{channel}s"]:
                    ratio_1 = row[f"Y{channel}s"] / base
                    if math.isfinite(ratio_1):
                        row[f"Y{channel}s/X1p"] = ratio_1
                    if base_sq:
                        ratio_2 = row[f"Y{channel}s"] / base_sq
                        if math.isfinite(ratio_2):
                            row[f"Y{channel}s/X1p^2"] = ratio_2
        self.latest.update({key: row[key] for key in ("X1p", "Y2s", "Status1", "Status2")})
        api.status(self._status())
        return row

    def _status(self):
        return {
            "State": "Running" if self.active else "Ready" if self.settings else "Not applied",
            "Set RMS Voltage": self.point, "OE1022D": self.identity, **self.latest,
        }
