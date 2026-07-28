# Lake Shore 372A Measurement Module

This module controls one Model 372/372A measurement input scanner through a
VISA GPIB resource. It requires a locally installed VISA implementation such
as NI-VISA or Keysight VISA.

## Framework dependency status

PyVISA 1.16.2 and typing_extensions 4.16.0 are shared framework dependencies
provided directly by OpenLab Control. The manifest keeps compatible version
ranges so an incompatible core is rejected before module source is imported,
but this module has no additional dependency runtime, lock file, wheel folder,
or Install Dependencies step.

The VISA vendor implementation itself is a system driver, not a Python wheel.
Install and configure NI-VISA or Keysight VISA separately on the instrument
computer; that operation is outside OpenLab Control.

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
- The Settings page disables resistance ranges that Figure 1-16 marks
  unavailable for the selected current or voltage excitation. A user
  excitation change moves an incompatible old selection to the nearest
  available range. Loaded settings are preserved for review and never block
  Enable or Test Connection. Apply rejects an incompatible Enabled slot before
  opening the instrument; an incompatible Disabled slot is kept disabled and
  shunted with a temporary compatible instrument value without altering its
  saved setting.
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
Figure 1-16, section 2.5.5.1, and interface-command pages 162, 165-168, 172,
and 174-175. A real instrument has not yet been connected, so this module
remains beta hardware support.
