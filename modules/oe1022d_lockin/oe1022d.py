"""OE1022D remote programming, chapters 5 and 9."""

from time import sleep

from labcontrol.module_api import ModuleError

from .visa import Session


CHANNEL_SETTINGS = (
    ("input", "ISRCD", "Input", ("Single-ended voltage", "Differential voltage"), 1),
    ("ground", "IGNDD", "Shield", ("Float", "Ground"), 0),
    ("coupling", "ICPLD", "Coupling", ("AC", "DC"), 1),
    ("notch", "ILIND", "Line notch", ("Off", "50 Hz", "50 + 100 Hz", "100 Hz"), 0),
    ("reserve", "RMODD", "Dynamic reserve", ("Low noise", "Normal", "High reserve"), 1),
    ("sensitivity", "SENSD", "Sensitivity", (
        "1 nV", "2 nV", "5 nV", "10 nV", "20 nV", "50 nV", "100 nV", "200 nV", "500 nV",
        "1 µV", "2 µV", "5 µV", "10 µV", "20 µV", "50 µV", "100 µV", "200 µV", "500 µV",
        "1 mV", "2 mV", "5 mV", "10 mV", "20 mV", "50 mV", "100 mV", "200 mV", "500 mV", "1 V",
    ), 24),
    ("time_constant", "OFLTD", "Time constant", (
        "10 µs", "30 µs", "100 µs", "300 µs", "1 ms", "3 ms", "10 ms", "30 ms", "100 ms",
        "300 ms", "1 s", "3 s", "10 s", "30 s", "100 s", "300 s", "1000 s", "3000 s",
    ), 11),
    ("slope", "OFSLD", "Filter slope", ("6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct"), 2),
    ("sync", "SYNCD", "Synchronous filter", ("Off", "On"), 0),
)

SENSITIVITY_VOLTS = tuple(value * 10 ** exponent for exponent in range(-9, 0) for value in (1, 2, 5)) + (1.0,)
TIME_CONSTANT_SECONDS = tuple(value * 10 ** exponent for exponent in range(-5, 4) for value in (1, 3))
FILTER_WAIT_MULTIPLIERS = (5, 7, 9, 10)


class LockIn(Session):
    def __init__(self, address, timeout, baud):
        super().__init__(address, timeout, baud_rate=baud, read_termination="\r", write_termination="\r")

    def identify(self):
        identity = self.query("*IDND?")
        if "OE1022D" not in identity.split(",", 1)[0].upper():
            raise ModuleError(f"Expected OE1022D, got {identity!r}", "LOCKIN_IDENTITY")
        return identity

    def setting(self, command, channel, value, index=None):
        base = str(channel) if index is None else f"{channel},{index}"
        self.write(f"{command} {base},{value}")
        # The USB firmware can drop a query sent immediately after a setting write.
        sleep(0.1)
        self.verify(f"{command}? {base}", value)

    def configure_channel(self, channel, settings, reference, frequency, checkpoint):
        modes = [("FMODD", reference)]
        if reference == 0:
            modes.append(("RSLPD", 0))
        elif reference == 1:
            modes.append(("FREQD", frequency))
        modes += [(cmd, settings[key]) for key, cmd, *_ in CHANNEL_SETTINGS]
        modes.append(("PHASD", settings["phase"]))
        for command, value in modes:
            checkpoint()
            self.setting(command, channel, value)
        for index, key in enumerate(("primary", "secondary"), 1):
            checkpoint()
            self.setting("HARMD", channel, settings[key], index)

    def zero(self, channel):
        # Program the inactive DC level first to avoid entering DC with a previous bias.
        self.setting("SVDCD", channel, 0)
        self.setting("SWVTD", channel, 3)

    def source_voltage(self, channel, voltage):
        self.setting("SLVLD", channel, voltage)
        self.setting("SWVTD", channel, 0)

    def auto_reserve(self, channel):
        self.write(f"ARSVD {channel}")

    def auto_gain(self, channel):
        self.write(f"AGAND {channel}")

    def set_reserve(self, channel, reserve):
        self.setting("RMODD", channel, reserve)

    def set_sensitivity(self, channel, sensitivity):
        self.setting("SENSD", channel, sensitivity)

    def read_filter(self, channel):
        time_constant = self.numbers(f"OFLTD? {channel}")[0]
        slope = self.numbers(f"OFSLD? {channel}")[0]
        if not time_constant.is_integer() or not 0 <= time_constant < len(TIME_CONSTANT_SECONDS) or not slope.is_integer() or not 0 <= slope < len(FILTER_WAIT_MULTIPLIERS):
            raise ModuleError(f"Channel {channel}: invalid time constant/filter slope {time_constant:g}/{slope:g}", "LOCKIN_BAD_REPLY")
        return int(time_constant), int(slope)

    def read_gain(self, channel):
        reserve = self.numbers(f"RMODD? {channel}")[0]
        sensitivity = self.numbers(f"SENSD? {channel}")[0]
        if not reserve.is_integer() or not 0 <= reserve <= 2 or not sensitivity.is_integer() or not 0 <= sensitivity <= 27:
            raise ModuleError(f"Channel {channel}: invalid reserve/sensitivity {reserve:g}/{sensitivity:g}", "LOCKIN_BAD_REPLY")
        return int(reserve), int(sensitivity)

    def read_harmonic(self, channel, index):
        # SNAPD table (not OUTPD): X/Y/R/θ for harmonic 1 or 2 plus reference frequency.
        first = 5 + 4 * (index - 1)
        return self.numbers(f"SNAPD? {channel},{first},{first+1},{first+2},{first+3},4", 5)

    def status_code(self, channel, external):
        status = 0
        for query, bit, bad in (("INOVD?", 2, 1), ("GNOVD?", 4, 1), ("*PLLD?", 16, 0)):
            if bit == 16 and not external:
                continue
            reply = self.numbers(f"{query} {channel}")[0]
            if reply not in (0, 1):
                raise ModuleError(f"{query} {channel}: expected 0 or 1, got {reply:g}", "LOCKIN_BAD_REPLY", query)
            if reply == bad:
                status |= bit
        return status
