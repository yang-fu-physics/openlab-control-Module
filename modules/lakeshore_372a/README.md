# Lake Shore 372A Measurement Module

This module controls one Model 372/372A measurement input scanner through a
VISA GPIB resource. It requires a locally installed VISA implementation such
as NI-VISA or Keysight VISA. Python dependencies are pinned with hashes and
must be carried as offline wheels; OpenLab Control never falls back to a
network install.

## Offline dependency status

The complete wheel set is present under `wheels/`. Its SHA-256 values match
`requirements.lock`, and installation through the core
`--no-index --only-binary=:all: --require-hashes` path has been verified:

- `pyvisa-1.16.2-py3-none-any.whl`:
  `54f034adafd3e8d1858d57cdafec64e920444f4b84b31c9fd17487fbad0a197a`
- `typing_extensions-4.16.0-py3-none-any.whl`:
  `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8`

## Measurement order

For each enabled R1-R4 slot, the module:

1. enables/configures the selected physical input (1-16) while keeping its
   excitation shunted;
2. sends and verifies `SCAN <channel>,0`, then unshunts and verifies the
   selected input;
3. waits the configured change-pause time;
4. asks the OpenLab core for a fresh primary temperature/field snapshot;
5. waits the configured dwell time;
6. asks for a second fresh system snapshot and reads `RDGR?`, `QRDG?`,
   `RDGPWR?`, and `RDGST?`;
7. writes one row for that slot, with the other R/phase/current/status slots
   blank;
8. shunts excitation immediately by default, including Stop/Error paths.

Temperature is normalized to kelvin and field to oersted before averaging.
The ordinary OpenLab system columns still contain the live values at row-write
time; `TemperatureAverage(K)` and `FieldAverage(Oe)` contain the requested
two-snapshot averages.

`PhaseN` is `atan2(QRDG, RDGR)` in degrees. In current mode, `CurrentN` is the
configured nominal excitation current. In voltage mode it is estimated from
the reported excitation power and the dissipative resistance.

`StatusN` is one of:

- `NORMAL`;
- `OVER_COMPLIANCE` for the Model 372 `CS OVL` status bit;
- `OVER_RANGE` for any other non-zero `RDGST?` bit.

Over-range and over-compliance are recoverable module Warnings and therefore
do not stop the SEQ. Exhausted GPIB retries, invalid/non-finite replies, stale
system snapshots, or identity mismatch are Errors and stop the SEQ.

## Safety and first hardware test

- Enable loads settings and discovers resources only; it does not connect or
  apply instrument settings.
- Apply Settings verifies `*IDN?`, configures only the four selected physical
  inputs, reads every setting back, and leaves excitation shunted.
- Each scan switch and excitation-shunt change is read back before a value is
  accepted.
- At least one slot must be enabled and all four physical channel selections
  must be unique.
- The estimated scan duration must fit inside the core module operation
  timeout.
- End/Stop/Error shunts all enabled configured inputs. Disable/application
  exit additionally closes the VISA resource manager.

Before connecting a sample, verify the GPIB address and commands with the
instrument disconnected from sensitive sensors, then test the smallest safe
excitation and the physical emergency procedure. This software behavior is
not a substitute for hardware interlocks.

Implementation commands and status bits were checked against the uploaded
Lake Shore Model 372 AC Resistance Bridge and Temperature Controller manual,
interface-command pages 162, 165-168, 172, and 174-175. A real instrument has
not yet been connected, so this module remains beta hardware support.
