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

## Settings and measurement order

The Settings page provides four logical measurement slots, R1-R4, matching
the four-channel workflow used by the Lake Shore 372A module. Every slot
selects one physical LR-720-16 sensor from 1-16 and can independently select:

- physical sensor number (`1–16`);
- enabled/disabled;
- range mode:
  - **Fixed**: resistance range (`2 mOhm`–`2 MOhm`), hardware-valid full-scale excitation voltage (`20 uV`–`20 mV`, restricted by the LR-700's `1 nA`–`30 mA` current-generator range per manual p. 4-12), and excitation percentage (`5%–100%`);
  - **Autorange** (`LR-700-AR`): lower/upper range limits `AUTORANGE L=<min><max>` (`min < max`). The module does **not** send `AUTORANGE T=` or `AUTORANGE U=`, preserving whatever trace (including a front-panel `T=4` user-defined trace) is configured on the LR-700;
- built-in fixed digital filter (`FILTER 0`: `1 s`, `FILTER 1`: `3 s`, `FILTER 2`: `10 s` — fixed digital filtering has no Off setting).

The shared switch/filter settle time (`switch_settle_seconds`, default `9.0 s`)
must be at least **3× the longest enabled digital filter** (`1 s → 3 s`,
`3 s → 9 s`, `10 s → 30 s`).

The LR-700 settings are global rather than stored independently for all
multiplexer channels. Therefore Apply Settings validates the complete desired
scan configuration (including range/excitation compatibility before opening
VISA), connects through GPIB, verifies the documented `GET 6` protocol
response, and leaves the bridge at the minimum allowable excitation for the
current resistance range. During Measure, this module's dynamic `slots`
property makes it run once for the current enabled logical slot and applies and
verifies only that corresponding sensor.

For every enabled sensor:

- **Fixed-range slots**:
  1. confirm minimum excitation (at 5%) for the current resistance range before switching;
  2. send `AUTORANGE 0`, `MODE 0`, `SELECT S=nn`, step through `RANGE 7` (`20 kOhm`) / `EXCITATION 0` if descending from `200 kOhm` / `2 MOhm` to `< 20 kOhm`, then write `RANGE`, `EXCITATION`, `FILTER`, and ramp `VAREXC` last;
  3. verify sensor and settings through `GET 6`, wait `switch_settle_seconds`, take two fresh temperature/field snapshots across `dwell_seconds`, read `GET 0`/`GET 1`/`GET 7`, verify `GET 6` again, emit one sparse DAT row, and restore minimum 5% excitation.
- **Autorange slots (continuous Run mode)**:
  1. Apply (`configure`) leaves the bridge at minimum 5% excitation; when the Run starts (`run_start`), if the first enabled slot is Autorange, the module turns on `AUTORANGE 1` **once** (keeping the current range unless outside `[min, max]`, never jumping to `max_range` and never writing `AUTORANGE T=` or `AUTORANGE U=`);
  2. while the Run stays in Autorange mode, each physical sensor caches its last normal settled `(range, excitation)` (cleared on re-Apply/Close). If the bridge's current range is already inside the next slot's `[min, max]` and matches that sensor's cached range (or on first visit), the module sends only `SELECT S=nn` (preceded by `AUTORANGE L=` only if limits changed, plus `FILTER` if changed) — no `RANGE` reset and no 5% drop. If the current range is outside the next slot's `[min, max]` or differs from its cached range, the module first restores 5% minimum excitation, switches `SELECT S=nn`, moves safely to the cached/clamped range at 5% (stepping via `RANGE 7` / `EXCITATION 0` when descending from `200 kOhm` / `2 MOhm`), and then re-enables `VAREXC 0`, `AUTORANGE L=`, and `AUTORANGE 1`;
  3. for each slot, the module checks `GET 6` at `1/4` and at the end of `switch_settle_seconds` (restarting the settle wait if range/excitation changed), takes two fresh temperature/field snapshots across `dwell_seconds`, reads `GET 0`/`GET 1`/`GET 7` with before/after `GET 6` verification, and emits one sparse DAT row;
  4. on Run end (`completed`/`stopped`/`error`), `Disable` (`close`), switching into a Fixed-range slot, or any error/cancellation during `Measure`, the module restores and readback-confirms `AUTORANGE 0` + minimum 5% excitation and refreshes the Status page's current range/excitation/current fields.

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
| `0–7` (`2 mOhm`–`20 kOhm`) | `EXCITATION 0` (`20 uV`) | `20 uV × 5%` |
| `8` (`200 kOhm`) | `EXCITATION 2` (`200 uV`) | `200 uV × 5%` (`50 pA`) |
| `9` (`2 MOhm`) | `EXCITATION 4` (`2 mV`) | `2 mV × 5%` (`50 pA`) |

The module reads `GET 6`, sets `AUTORANGE 0`, lowers `VAREXC` to `5%`, selects
the minimum allowable `EXCITATION` for the active range, and confirms the
result via `GET 6` during Apply, before sensor changes, after every sensor, on
every completed/Stop/Error `run_end`, and in `close(api)`. If it cannot read
the state back, it reports an Error instead of claiming the excitation is safe.
Hardware interlocks and the laboratory's manual emergency procedure remain
necessary.

## Verification boundaries

Protocol details were checked against the LR-700 v1.3 manual (pages 2-3 to 2-6,
2-18 to 2-24, 3-25 to 3-56, 4-4 to 4-16) and against real LR-700/LR-720 bench
tests (no-load command checks, S1–S4 loaded fixed-range reads, and S1
`AUTORANGE L=19 / T=2` check). Full OpenLab loaded end-to-end sequences
(including all autorange traces/boundaries, 1 s and 10 s filter settling, and
Stop/Error/reconnect recovery under load) should still be verified on hardware
with a noncritical load before connecting sensitive samples.
