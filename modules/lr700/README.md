# Linear Research LR-700 + LR-720-16 Measurement Module

This beta module controls one Linear Research LR-700 AC resistance bridge with
one LR-720-16 sixteen-sensor multiplexer through a VISA GPIB resource.

## Framework dependencies

PyVISA is supplied by OpenLab Control and is not repeated in the manifest. All modules use the
core's locked Python dependencies. A system VISA implementation such as NI-VISA or
Keysight VISA must still be installed and configured on the instrument
computer.

The LR-700 manual requires GPIB to be enabled at the front panel with
`SPECIAL 4 1`. Its factory GPIB address is 18 unless it has been changed with
`SPECIAL 43`.

### LR-700 VISA session timing

`LR700VisaTransport` wraps OpenLab Control's shared `VisaSession` with LR-700-specific
GPIB settings confirmed on real NI GPIB-USB-HS hardware without altering the
framework's global defaults:

- `read_termination = None` (end reads on GPIB EOI);
- `write_termination = "\n"`;
- `query_delay = 0.2 s`.

## Common configuration and channel table

Four logical slots R1–R4 each select one LR720 physical input S1–S16. The common
configuration selects fixed/autorange, digital filter, fixed range/current,
autorange bounds and maximum excitation current. A row may override only the
fixed range and current. All rows use the common filter and current limit.

Currents in settings files are amperes; the UI uses mA. A fixed current is converted
to a hardware-valid EXCITATION and integer VAREXC percentage (5–100%). The closest
value at or below the requested current is used and displayed. An unrepresentable
current or a requested current above the common limit rejects Apply before connecting.

### Generated autorange Trace

Apply computes a ten-entry custom trace from the common current limit. For each
range, it selects the largest legal excitation whose full-scale current at 100%
does not exceed the limit. Ranges whose minimum full-scale excitation exceeds
the limit are excluded by the effective AUTORANGE L bounds. At least two usable
ranges are required. The UI displays effective bounds and each active range's
current. The supported autorange range indices are 1–9 (20 mOhm–2 MOhm).

For the default 3 mA limit, U=0135666666 and L=19: 20 mOhm/60 uV,
200 mOhm/600 uV and 2 Ohm/6 mV give 3 mA; higher ranges use 20 mV and
progressively smaller currents. The excluded 2 mOhm entry is not active.

After lowering excitation, Apply writes AUTORANGE U=<generated digits> and
AUTORANGE T=4 if an enabled channel uses autorange. It leaves AUTORANGE off at
minimum excitation. The applied settings retain the trace, effective bounds,
resolved channel configurations and original overrides. Re-Apply regenerates
the trace and clears cached ranges. GET 6 does not report the trace table;
there is no invented trace query. Range, excitation, percentage, filter and
sensor are checked through documented GET 6 readbacks during operation.

### Channel switching

During Run, autorange remains active between autorange channels. Each physical
sensor remembers its last normally settled range. If the next sensor uses the
current range (or has no cached range) and the current configuration matches,
only SELECT is written: no RANGE or EXCITATION reset. A required range change
first lowers excitation, restores the cached/clamped range and the corresponding
entry from the applied trace, then restores 100% and autorange. The trace itself
is not rewritten during scanning.

For fixed channels with the same range, actual current and common filter,
switching also writes SELECT only. Independently configured different currents
or a change between fixed/autorange require lowering excitation and applying
the target configuration, even when the resistance range is unchanged.

FILTER 0/1/2 mean 1/3/10 seconds; there is no digital-filter Off value.
Switch settle must be at least 3x the filter time (default 9 s for FILTER 1).
Measurements check range settling and read R, X and overload status using
GET 0/1/7. Run end, Stop, Error and Disable restore and readback-confirm
AUTORANGE 0 and the minimum legal excitation at 5%. LR700 has no excitation-off
command. A standalone measurement also restores this minimum state afterwards.
During Run the application owns instrument settings; manual/other-program
changes are not supported. The current limit is implemented by programmed
settings, not an independent hardware protection circuit.

Legacy per-channel files can be loaded into the new UI when their enabled
channels share a filter and autorange bounds. Fixed rows become independent
overrides. Files with conflicting common fields require explicit reconfiguration.

The fixed output columns are `TemperatureAverage`, `FieldAverage`, `R1/X1`
through `R4/X4`, and one common integer `StatusCode` column. Only the current
logical slot's R and X fields are filled in each row; `StatusCode` describes
that row. Temperature is normalized to kelvin and field to oersted before the
two snapshots are averaged.

The LR-700-specific status codes are:

- `0`: normal when `GET 7` returns zero;
- `1`: dX, R, or dR overrange;
- `2`: common-mode, I-HIGH, or tuned-amplifier overload;
- `3`: invalid R/X/overload measurement reply.

When `GET 7` reports positive or negative common-mode overload (bits 4 / 8),
the status and warning text preserve the documented bit names and add a wiring
diagnostic hint (`电流端接触电阻可能过大，请检查 I-LOW 回路及接线。`). Common-mode
overload is a measurement status, not a configuration-command error code.

Non-zero status is a recoverable Warning and does not stop the SEQ. Every
nonzero row retains the temperature/field averages and `StatusCode`, but leaves
the current slot's R/X fields empty; all unmeasured slots are empty as well.

## Verification

The refactor is verified offline with protocol simulation, settings round trips,
generated trace limits, cached range restoration, fixed/auto mixed scans and
SELECT-only switching. Previous real-instrument protocol tests remain the basis
for the LR700 VISA session and command encoding. This refactor has not yet been
retested with connected samples.

## Fixed-reference 10x SEQ commands (0.1.0b9)

**10x Delta R On** captures the settled absolute R of every enabled sensor in 1x.
Each reference is written as OFFSET R=<counts>, checked with GET 4, and retained
with that sensor's settled range, excitation percentage and filter. Capture uses
the normal applied range strategy; autorange is then disabled for the 10x session.
Only after all captures succeed is the reference set activated. No reference is
entered manually. The status page lists the references and their resistance ranges.

During a field sweep, selecting another sensor first writes MODE 0, applies its
frozen channel configuration, restores its saved R Set, then writes MODE 1. The
normal configured settle/dwell precedes the formal reading. References are never
recaptured during the sweep. The hardware 10x mode also affects Delta X; this
feature writes only R Set, not X Set. DAT absolute R/X columns are unchanged.

**10x Delta R Off** writes MODE 0 and leaves the current physical R Set unchanged.
The internal active references are released; subsequent Measure uses the original
applied automatic/fixed range configuration. Run end/Stop/Error/Disable also return
to 1x and confirm minimum excitation. A new On captures new references for the next
temperature point. Unstable ranging/capture or capture overload is a Warning,
not a fatal SEQ error; communication and write/readback failures remain errors.

Capture timeout is checked before writes. With four channels, 3 s filters and
9 s settle, normal capture waits total 45 s (4x9 s plus final 10x settle), excluding
communication. Scanning then waits once per sensor, not two new baseline waits.
The new OFFSET/GET 4 sequence has offline verification only, not a new bench test.
