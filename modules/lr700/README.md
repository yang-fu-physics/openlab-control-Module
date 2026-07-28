# Linear Research LR-700 + LR-720-16 Measurement Module

This beta module controls one Linear Research LR-700 AC resistance bridge with
one LR-720-16 sixteen-sensor multiplexer through a VISA GPIB resource.

## Framework dependencies

PyVISA is supplied and version-checked by OpenLab Control. This module has no
additional dependency runtime, `requirements.lock`, wheel directory, or
`Install Dependencies` step. A system VISA implementation such as NI-VISA or
Keysight VISA must still be installed and configured on the instrument
computer.

The LR-700 manual requires GPIB to be enabled at the front panel with
`SPECIAL 4 1`. Its factory GPIB address is 18 unless it has been changed with
`SPECIAL 43`.

## Settings and measurement order

The Settings page provides four logical measurement slots, R1-R4, matching
the four-channel workflow used by the Lake Shore 372A module. Every slot
selects one physical LR-720-16 sensor from 1-16 and can independently select:

- physical sensor number;
- enabled/disabled;
- full-scale resistance range;
- full-scale excitation voltage;
- excitation percentage from 5% to 100%;
- built-in digital filter: Off, 1 s, or 10 s.

The LR-700 settings are global rather than stored independently for all
multiplexer channels. Therefore Apply Settings validates the complete desired
scan configuration, connects through GPIB, verifies the documented `GET 6`
protocol response, and leaves the bridge at minimum excitation. During
Measure, the module applies and verifies the corresponding row after selecting
each enabled sensor.

For every enabled sensor, the module:

1. confirms minimum excitation before switching;
2. sends `SELECT S=nn`, disables autorange, selects R/X mode, and writes the
   row's `RANGE`, `EXCITATION`, `VAREXC`, and `FILTER`;
3. verifies sensor and settings through `GET 6`;
4. waits the configured switch/filter settle time;
5. requests a fresh primary temperature/field snapshot from OpenLab Control;
6. waits the configured dwell time and requests a second fresh snapshot;
7. reads R with `GET 0`, X with `GET 1`, and overload state with `GET 7`;
8. verifies `GET 6` again so a front-panel change cannot silently relabel data;
9. emits one sparse DAT row for that sensor and restores minimum excitation.

The fixed output columns are `TemperatureAverage`, `FieldAverage`, and
`R1/X1/Status1` through `R4/X4/Status4`. Only the current logical slot's R, X,
and Status fields are filled in each row. Temperature is normalized to kelvin
and field to oersted before the two snapshots are averaged.

`StatusN` is:

- `NORMAL` when `GET 7` returns zero;
- `OVER_RANGE` for dX, R, or dR overrange bits;
- `OVERLOAD` for common-mode, I-HIGH, or tuned-amplifier overload bits.

Non-zero status is a recoverable Warning and does not stop the SEQ. Invalid
responses, exhausted read retries, ambiguous write failure, stale system
snapshots, settings readback mismatch, or unconfirmed minimum excitation are
Errors and stop the SEQ.

## Excitation safety boundary

The LR-700 command set does not provide a true excitation-off command. The
lowest software-confirmable state is:

```text
EXCITATION 0   -> 20 uV full scale
VAREXC =05
VAREXC 1       -> 5%, or 1 uV full scale
```

The module confirms this state during Apply, before sensor changes, after every
sensor, and on completed/Stop/Error/Disable/exit paths. If it cannot read the
state back, it reports an Error instead of claiming the excitation is safe.
Hardware interlocks and the laboratory's manual emergency procedure remain
necessary.

Enable only discovers resources and loads saved desired settings. Test
Connection uses only `GET 6` and `GET 7`; it does not write settings. Loading a
SEQ imports the module settings but does not Enable, connect, or Apply them.

Before connecting a sensitive sensor, verify commands, multiplexer numbering,
GPIB termination, range, excitation, and the physical emergency procedure
with a noncritical load.

Protocol implementation was checked against the uploaded LR-700 v1.3 manual:
measurement technique page 2-24; LR-720 pages 3-25 to 3-27; command summary
and reference pages 3-30 to 3-49; response pages 3-50 to 3-51; and IEEE-488
page 3-52. Real hardware has not yet been connected, so this module remains
beta hardware support.
