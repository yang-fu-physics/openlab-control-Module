"""Bounded VISA session and numeric reply validation for the OE1022D."""

import math

from labcontrol.measurement.visa import VisaSession
from labcontrol.module_api import ModuleError


class Session(VisaSession):
    def query(self, command):
        return super().query(command).strip()

    def numbers(self, command, count=1):
        response = self.query(command)
        try:
            values = tuple(float(item) for item in response.split(","))
        except ValueError as exc:
            raise ModuleError(f"{command}: invalid numeric reply {response!r}", "LOCKIN_BAD_REPLY", command) from exc
        if len(values) != count or not all(math.isfinite(value) for value in values):
            raise ModuleError(f"{command}: expected {count} finite values, got {response!r}", "LOCKIN_BAD_REPLY", command)
        return values

    def verify(self, command, expected, *, relative_tolerance=1e-8):
        if not math.isclose(self.numbers(command)[0], expected, rel_tol=relative_tolerance, abs_tol=0.0):
            raise ModuleError(f"{command}: could not confirm {expected:g}", "LOCKIN_READBACK_MISMATCH", command)
