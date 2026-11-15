"""Keithley 6221 sine-wave and reference-marker operations (not Delta mode)."""

import re
from math import isclose, isfinite, sqrt

from labcontrol.measurement.visa import VisaSession
from labcontrol.module_api import ModuleError


MIN_PEAK = 2e-12
FIXED_RANGES = (2e-9, 20e-9, 200e-9, 2e-6, 20e-6, 200e-6, 2e-3, 20e-3, 0.1)
MAX_PEAK = FIXED_RANGES[-1]
RMS_TO_PEAK = sqrt(2)


class CurrentSource(VisaSession):
    def query(self, command):
        return super().query(command).strip()

    def number(self, command):
        response = self.query(command)
        try:
            value = float(response)
        except ValueError as exc:
            raise ModuleError(f"{command}: invalid numeric reply {response!r}", "K6221_AC_BAD_REPLY", command) from exc
        if not isfinite(value):
            raise ModuleError(f"{command}: expected a finite value, got {response!r}", "K6221_AC_BAD_REPLY", command)
        return value

    def verify(self, command, expected, *, relative_tolerance=1e-8):
        return self._verify_number(command, self.number(command), expected, relative_tolerance)

    @staticmethod
    def _verify_number(command, value, expected, relative_tolerance):
        if not isclose(value, expected, rel_tol=relative_tolerance):
            raise ModuleError(f"{command}: expected {expected:g}, got {value:g}", "K6221_AC_READBACK_MISMATCH", command)
        return value

    def identify(self):
        identity = self.query("*IDN?")
        if not re.search(r"(?:^|,)\s*(?:MODEL\s+)?6221\s*,", identity, re.I):
            raise ModuleError(f"Expected Keithley 6221, got {identity!r}", "K6221_AC_IDENTITY")
        return identity

    def output_off(self):
        self.write("SOUR:WAVE:ABOR")
        self.write("OUTP OFF")
        self.verify("OUTP?", 0)

    def start_wave(self, frequency, compliance, marker_line, phase, current_cap, checkpoint):
        self.verify("OUTP?", 0)
        source_range = next(value for value in FIXED_RANGES if value >= current_cap * RMS_TO_PEAK)
        self.write(f"SOUR:CURR:RANG {source_range:.12g}")
        self.verify("SOUR:CURR:RANG?", source_range)
        fixed = (
            ("FUNC", "SIN", ("SIN", "SINUSOID")), ("OFFS", 0, 0),
            ("RANG", "FIX", ("FIX", "FIXED")), ("DUR:TIME", "INF", None), ("EXTR", 0, 0),
            ("FREQ", frequency, frequency), ("AMPL", MIN_PEAK, MIN_PEAK),
            ("PMAR", phase, phase), ("PMAR:OLIN", marker_line, marker_line), ("PMAR:STAT", 1, 1),
        )
        self.write(f"SOUR:CURR:COMP {compliance:.12g}")
        self.verify("SOUR:CURR:COMP?", compliance)
        for suffix, value, expected in fixed:
            checkpoint()
            command = f"SOUR:WAVE:{suffix}"
            self.write(f"{command} {value:.12g}" if isinstance(value, float) else f"{command} {value}")
            if isinstance(expected, tuple):
                if self.query(f"{command}?").upper() not in expected:
                    raise ModuleError(f"Could not confirm {command} {value}", "K6221_AC_READBACK_MISMATCH", command)
            elif expected is not None:
                # Peak conversion is irrational; the SCPI readback may have fewer significant digits.
                self.verify(f"{command}?", expected, relative_tolerance=5e-6 if suffix == "AMPL" else 1e-8)
        self.write("SOUR:WAVE:ARM")
        checkpoint()
        self.write("SOUR:WAVE:INIT")
        self.verify("OUTP?", 1)

    def set_amplitude(self, magnitude):
        peak = magnitude * RMS_TO_PEAK
        self.write(f"SOUR:WAVE:AMPL {peak:.12g}")
        self.verify("SOUR:WAVE:AMPL?", peak, relative_tolerance=5e-6)
        self.verify("OUTP?", 1)

    def set_marker(self, phase):
        self.write(f"SOUR:WAVE:PMAR {phase}")
        self.verify("SOUR:WAVE:PMAR?", phase)
        self.verify("OUTP?", 1)

    def set_frequency(self, frequency):
        self.write(f"SOUR:WAVE:FREQ {frequency:.12g}")
        self.verify("SOUR:WAVE:FREQ?", frequency)
        self.verify("OUTP?", 1)

    def read_source(self, current, frequency):
        self.verify("OUTP?", int(current != 0))
        if not current:
            return 0.0, None, None, 0
        phase = 180 if current < 0 else 0
        magnitude = self.verify("SOUR:WAVE:AMPL?", abs(current) * RMS_TO_PEAK, relative_tolerance=5e-6) / RMS_TO_PEAK
        self.verify("SOUR:WAVE:PMAR:STAT?", 1)
        self.verify("SOUR:WAVE:PMAR?", phase)
        measured_frequency = self.verify("SOUR:WAVE:FREQ?", frequency)
        # 6221 Measurement Condition Register B3: compliance.
        value = self.number("STAT:MEAS:COND?")
        if not value.is_integer() or not 0 <= value < 32768:
            raise ModuleError(f"Invalid 6221 measurement condition {value:g}", "K6221_AC_BAD_REPLY")
        return magnitude, phase, measured_frequency, int(bool(int(value) & 8))
