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
GPIB settings without altering the framework's global defaults:

- `read_termination = None` (end reads on GPIB EOI; no Bytes at Port polling);
- `write_termination = "\n"`;
- `query_delay = 0.1` (`100 ms`, `LR700_QUERY_DELAY_SECONDS`) on `LR700VisaTransport`
  for both `GET` queries and `write()` readbacks (`handle.query()`), leaving the
  framework's shared `VisaSession` defaults unchanged;
- **write-then-readback synchronization**: matching the LR-700 v1.3 manual (p. 3-50),
  every non-`GET` command sent via `LR700VisaTransport.write()` calls `handle.query()`
  (applying the `100 ms` `query_delay` and reading to EOI), expecting an empty line
  (`\n` + EOI), raising `LR700_COMMAND_REJECTED` immediately on `"ERROR"`, and treating
  unexpected non-empty replies or I/O failures as `LR700_WRITE_UNCERTAIN`;
- **5.0 s reconnect delay**: after closing a failed GPIB session, both read-retry and
  safe-state cleanup wait `5.0 s` (`LR700_RECONNECT_DELAY_SECONDS`) before reopening.

## Common configuration and channel table

Four logical slots R1–R4 each select one unique LR-720-16 physical input S1–S16.
The common configuration selects fixed/autorange, digital filter, fixed
range/current, autorange bounds, and maximum excitation current (`current_limit_a`).
A channel row may override only the fixed range and fixed current (`independent`);
all rows share the common digital filter and common current limit.

Currents in settings files are amperes; the UI uses mA. A fixed current is
converted to a hardware-valid `EXCITATION` and integer `VAREXC` percentage
(`5%–100%`), selecting the closest representable value at or below the requested
current and displaying the actual value in the channel table (if the requested
current exceeds the chosen range's hardware maximum, e.g., `3 mA` on `2 MOhm`,
it clamps down to that range's maximum). A current below the range minimum or
above `current_limit_a` rejects Apply before opening VISA.

### Generated Non-Standard Autorange Trace (`AUTORANGE T=4`)

> **Important (Non-Standard Trace)**: This module does **not** use the LR-700's
> common factory autorange traces (`T=1` Default / `T=2` Minimum Power / `T=3`
> Maximum Power in LR-700 v1.3 manual p. 2-18). Instead, Apply dynamically
> computes a 10-entry User Defined trace (`AUTORANGE U=<10 digits>`,
> `AUTORANGE T=4`) from the common maximum excitation current limit over
> **`20 mOhm` (`1R`)–`2 MOhm` (`9R`)** (`2 mOhm` / `0R` is always excluded from
> autorange).

For each range `0–9`, Apply selects the largest hardware-valid excitation whose
full-scale current at `100%` does not exceed `current_limit_a`. Ranges whose
minimum full-scale excitation exceeds `current_limit_a` are automatically
excluded by tightening the effective `AUTORANGE L=<min><max>` bounds (at least
two usable ranges are required). The UI previews the effective bounds, the
generated `U=` string, and each active range's full-scale current.

For the default **`3 mA`** current limit, the generated trace is
`AUTORANGE U=0135666666` with effective bounds `L=19`:

| Range index | Resistance range | Excitation (`V_fs`) | Excitation index | Full-scale current (`100%`) | Notes |
| --- | --- | --- | --- | --- | --- |
| `0` | `2 mOhm` | `20 uV` *(placeholder)* | `0` | — | **Excluded from Autorange** (`L` lower limit `>= 1`); index `0` is filled only because `AUTORANGE U=` requires all 10 digits (manual p. 3-32). |
| `1` | `20 mOhm` | `60 uV` | `1` | `3 mA` | Active `T=4` range |
| `2` | `200 mOhm` | `600 uV` | `3` | `3 mA` | Active `T=4` range |
| `3` | `2 Ohm` | `6 mV` | `5` | `3 mA` | Active `T=4` range |
| `4` | `20 Ohm` | `20 mV` | `6` | `1 mA` | Active `T=4` range |
| `5` | `200 Ohm` | `20 mV` | `6` | `100 uA` | Active `T=4` range |
| `6` | `2 kOhm` | `20 mV` | `6` | `10 uA` | Active `T=4` range |
| `7` | `20 kOhm` | `20 mV` | `6` | `1 uA` | Active `T=4` range |
| `8` | `200 kOhm` | `20 mV` | `6` | `100 nA` | Active `T=4` range |
| `9` | `2 MOhm` | `20 mV` | `6` | `10 nA` | Active `T=4` range |

Because the default `3 mA` custom trace applies up to `3 mA` (on `20 mOhm`–`2 Ohm`)
and `20 mV` (on `20 Ohm`–`2 MOhm`) — higher than the factory `Default` (`T=1`) and
`Min Power` (`T=2`) traces — sensitive low-temperature samples that require lower
excitation should lower the common **Maximum excitation current** limit, raise
`autorange_min_range`, or use **Fixed** range mode.

After lowering excitation to the safe state, Apply writes
`AUTORANGE U=<generated digits>` and `AUTORANGE T=4` when using the common
configuration with at least one enabled autorange channel, and leaves `AUTORANGE`
off at minimum `5%` excitation. The applied settings retain the generated trace,
effective bounds, resolved channel configurations, and original overrides.
Re-Apply regenerates the trace and clears cached sensor ranges. `GET 6` does not
report the trace table; there is no invented trace query. Range, excitation,
percentage, filter, mode, and sensor are checked through documented `GET 6`
readbacks during operation.

### Channel switching

During Run, autorange remains active between autorange channels. Each physical
sensor caches its last normally settled range index (cleared on re-Apply/Close).
If the next sensor uses the current range (or has no cached range yet), stays
within effective bounds, and has the matching applied-trace excitation and filter,
only `SELECT S=nn` is written — no `RANGE` or `EXCITATION` reset. When a range
or excitation restore is required, the module first lowers excitation to `5%`,
restores the cached/clamped range and **updates `EXCITATION` to the corresponding
entry from the applied trace** (so the excitation current matches the active
trace table), then restores `100%` (`VAREXC 0`) and `AUTORANGE 1`. The trace
table itself is not rewritten during scanning.

For fixed channels with the same range, actual current, mode, and common filter
during a Run, switching also writes `SELECT S=nn` only. Independently configured
different currents or switching between fixed and autorange lowers excitation
first and applies the target configuration (raising `RANGE` before `SELECT` when
moving to a higher resistance range, and stepping `2 MOhm → 200 kOhm / 200 uV →
20 kOhm / 20 uV` when descending from `2 MOhm` so transient current stays
`<= 500 pA`), even when the resistance range is unchanged.

> **Hardware transition limit below `500 pA`**: Because LR-700 hardware requires
> `EXCITATION >= 4` (`2 mV`) on `2 MOhm` and `>= 2` (`200 uV`) on `200 kOhm`
> with minimum `VAREXC = 5%`, any downward range switch `2 MOhm → 200 kOhm`,
> `2 MOhm → 20 kOhm`, or `200 kOhm → 20 kOhm` unavoidably passes through a
> `500 pA` intermediate state. When `current_limit_a < 500 pA`, Apply and
> runtime channel transitions reject any configuration or live bridge-range
> transition that would exceed `current_limit_a` (`LR700_TRANSITION_OVER_LIMIT`)
> before sending `RANGE`.

`FILTER 0/1/2` select `1/3/10 s`; fixed digital filtering has no Off setting.
`switch_settle_seconds` must be at least **3x** the filter time (default `9.0 s`
for `FILTER 1`). Measurements check range settling (`GET 6` before/after) and
read R, X, and overload status using `GET 0/1/7`. Continuous range/excitation
oscillation during autorange measurement (`LR700_AUTORANGE_UNSTABLE`) is
reported as a recoverable Warning rather than stopping the SEQ, and during an
active Run it does not exit autorange or send extra safe-state cleanup commands
so subsequent slots continue smoothly. A standalone measurement restores the
minimum `5%` state afterwards. During Run the application
owns instrument settings; manual or third-party changes are not supported. The
current limit is enforced by programmed settings, not an independent hardware
protection circuit.

Legacy per-channel files can be loaded into the new UI when their enabled
channels share a filter and autorange bounds. Fixed rows become independent
overrides and `current_limit_a` is derived from enabled channels only (so
low-current fixed configs are not raised to `3 mA`). Files with conflicting
common fields are rejected without partially modifying UI widgets.

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

## Excitation safety boundary

The LR-700 command set does not provide a true excitation-off command. The
lowest software-confirmable state depends on the active resistance range
(LR-700 v1.3 manual p. 4-12 and hardware-verified):

| Resistance range | Minimum allowable excitation | Confirmed safe state (`5%`) |
| --- | --- | --- |
| `0–7` (`2 mOhm`–`20 kOhm`) | `EXCITATION 0` (`20 uV`) | `20 uV x 5%` |
| `8` (`200 kOhm`) | `EXCITATION 2` (`200 uV`) | `200 uV x 5%` (`50 pA`) |
| `9` (`2 MOhm`) | `EXCITATION 4` (`2 mV`) | `2 mV x 5%` (`50 pA`) |

During Apply, configuration transitions, standalone measurements, and every
completed/Stop/Error `run_end` or `close(api)`, the module reads `GET 6`, sets
`AUTORANGE 0`, lowers `VAREXC` to `5%`, selects the minimum allowable
`EXCITATION` for the active range, and confirms the result via `GET 6` (`run_end`
and `close` also confirm `MODE 0`). If the safe state cannot be read back, the
module reports an Error rather than claiming the bridge is safe. Hardware
interlocks and the laboratory's manual emergency procedure remain necessary.

## Fixed-reference 10x SEQ commands (0.1.0b9)

`LR700Backend.sequence_commands` exposes two parameterless SEQ commands:
`x10_on` (**10x Delta R On**) and `x10_off` (**10x Delta R Off**).

- **`x10_on` (10x Delta R On)**: confirms `MODE 0`, clears both `RSET` and any
  stale `XSET` via `OFFSET =0` before switching/autorange capture, captures the
  settled absolute R of every enabled sensor in 1x (using the applied
  fixed/autorange strategy), writes `OFFSET R=<counts>` checked via `GET 4`,
  disables autorange, and freezes each sensor's settled range, excitation,
  percentage, and filter. Only after all enabled sensors succeed are the
  references activated and `MODE 1` confirmed. Any capture warning, error, or
  cancellation clears the partial cache and restores `MODE 0` + minimum `5%`
  excitation. No reference is entered manually; the Status page lists the
  captured references and ranges.
- **Scanning in 10x mode**: switching to another sensor writes and confirms
  `MODE 0`, applies that sensor's frozen channel configuration, restores its
  saved `R Set` via `OFFSET R=` / `GET 4`, then writes and confirms `MODE 1`
  and waits the configured `switch_settle_seconds` + `dwell_seconds` before
  reading. References are never recaptured during the sweep. DAT absolute
  `R`/`X` columns are unchanged.
  - **Readback & `10ΔX` notes**: Measure intentionally queries `GET 0` (`R`)
    and `GET 1` (`X`) directly (keeping standard 5.5-digit absolute `R`/`X`
    readback while `MODE 1` engages the 10x analog difference gain). Hardware
    `MODE 1` magnifies **both `ΔR` and `ΔX` by 10x** (limiting the window to
    ±10% of full scale around `RSET`/`XSET`), while this command only writes
    `R Set` (not `X Set`). If a sensor's reactance exceeds ±10% of full scale,
    `GET 7` reports `±ΔX overrange` (bits 1/2 → `StatusCode = 1`), leaving that
    row's `R` and `X` blank.
- **`x10_off` (10x Delta R Off)**: checks its settle timeout budget, writes and
  confirms `MODE 0`, waits `switch_settle_seconds`, leaves the physical `R Set`
  unchanged, and releases the active reference cache so subsequent `Measure`
  resumes the applied autorange/fixed settings. Re-Apply (`configure`),
  failed/cancelled `Measure` during 10x, `run_end` (completed/Stop/Error), and
  `close` also return to `MODE 0`, reset Status `"10x References"` to
  `"Inactive"`, and confirm minimum 5% excitation. A new `x10_on` captures
  fresh references (e.g., at the next temperature point). Unstable ranging,
  overload, or out-of-range counts (`|counts| > 199999`) during capture raises
  a recoverable Warning; communication and readback failures remain Errors.
- **Timeout budget**: `x10_on` checks worst-case capture duration against
  `api.timeout - 2.0 s` before writing. With four fixed channels, `3 s` filter,
  and `9 s` settle, normal capture waits total `45 s` (`4 x 9 s` plus the final
  `9 s` 10x settle), excluding GPIB I/O; autorange channels reserve extra settle
  attempts in the worst-case estimate (`attempts * (settle + 2) + settle +
  io_retry_and_cleanup_margin`, where `io_retry_and_cleanup_margin` covers
  `retry` read timeouts, `(retry - 1) x 5 s` read reconnect waits, and `2 x
  (io_timeout + 5 s)` post-error safe-state cleanup attempts plus `5 s` slack).
  Subsequent scanning waits once per sensor.

## Verification boundaries

Protocol details were checked against the LR-700 v1.3 manual (pp. 2-3 to 2-10,
2-18 to 2-25, 3-25 to 3-56, 4-3 to 4-16) and against real LR-700 / LR-720-16 bench
tests (no-load command checks, S1–S4 loaded fixed-range reads, S1 `AUTORANGE L=19 / T=2`,
and `GPIB0::18::INSTR` EOI query-delay tests). On `2026-11-25`, `20 ms` `query_delay`
passed `600/600` long `GET 6` reads (`FILTER 3`, 30 bytes) but returned empty `b'\n'`
replies in `3/3` mixed `GET 6/0/1/4/7` runs (both LF and CR), whereas `100 ms`
(`query_delay = 0.1`) passed `300/300` long `GET 6` reads and `250/250` mixed `GET`
queries. `100 ms` is still a bounded-sample verification and has not yet been long-run
verified across full measurement and channel-switching workflows. The common-configuration
refactor, generated `T=4` trace table, fixed/auto mixed switching, and
`OFFSET`/`GET 4`/`MODE 1` 10x commands are verified **offline only** via protocol
simulation and have not yet been retested on connected samples. Verify with a noncritical
dummy load before connecting sensitive samples.
