# Keithley 6221 + 2182A Delta + 7001

Version `0.1.0b5` is a Beta Measurement Module for a Keithley 6221 current
source, a Keithley 2182A
nanovoltmeter connected to the 6221 by RS-232 and Trigger Link, and an optional
Keithley 7001 switch system controlled by a second GPIB resource.
It requires OpenLab Control `>=0.11.5,<0.12` for API 1.1 logical-slot
scheduling and core-managed rawdata rows.

Before using Apply, manually enable the 2182A RS-232 interface at 19.2 kbaud,
XON/XOFF flow control, and CR termination; configure the 6221 serial side to
match, keep the 6221 computer interface on GPIB, and connect both RS-232 and
Trigger Link cables. The module verifies that the 6221 reports the 2182A and
checks the relayed 2182A identity, but it does not silently change these panel
communication settings.

The module has two modes:

- **Shared configuration / continuous Armed**: `begin_sequence` applies the
  common settings, sends `SOUR:DELT:ARM`, waits at least 3 seconds, verifies the
  Armed state, and remains Armed until the SEQ ends. Each Measure switches the
  enabled channels and starts a finite Delta acquisition by software trigger.
- **Independent configuration / re-arm per channel**: before every route change
  the module aborts Delta and confirms zero/off state. After switching, it applies
  that channel's complete settings, arms, waits 3 seconds, verifies, and triggers.

The module declares `measurement_mode = "aligned_slots"`. The core calls it
once for each enabled logical channel, aligns CH1-CH4 with other scanner
modules, and writes one DAT row per channel. The DAT contains the
numeric channel index (1-4), resistance converted from Delta voltage, effective
reversal current, resistance standard deviation, sample count, and integer
status code. The per-cycle Delta
voltage readings are written without a header to
`rawdata/<data-file>__<path-digest>__keithley_6221_2182a_delta.rawdata`; each
rawdata line corresponds to one formal DAT row. Delta count is limited to
32,768 so the complete raw sequence remains within the core's 1 MiB IPC frame.

`routing.toml` is intentionally not exposed in the UI. Its default Slot 1 routes
are:

- CH1: 1, 11, 5, 15
- CH2: 2, 12, 5, 15
- CH3: 3, 13, 5, 15
- CH4: 4, 14, 5, 15

Edit the full 7001 addresses in that file and restart the application when the
installed switching card or slot differs.

If 7001 is blank or cannot be identified during Enable, the module raises a
deduplicated Warning and works as a direct CH1-only measurement. A 7001 failure
after initialization is fatal: there is no automatic retry because an uncertain
route must not be followed by a current trigger.

`StatusCode` is module-specific and contains no text:

- `0`: normal;
- `1`: a finite 2182A voltage exceeds the supported range;
- `2`: reserved for a positively identified 6221 compliance event;
- `3`: an invalid, non-finite, or incomplete Delta trace.

For simultaneous data faults, an invalid or incomplete trace has priority over
voltage overrange and produces code 3. Code 2 will only take precedence after a
future implementation can positively identify and test a real compliance
condition; it is not currently emitted.

Every nonzero row leaves `Resistance` and `StdDev` empty. Numeric diagnostic
metadata such as `Channel`, requested/effective `Current`, `SampleCount`, and
the raw 2182A sequence remain available; they are not presented as a valid
resistance result.

The beta implementation does not infer compliance from an arbitrary
`SYST:ERR?` string; until exact real-hardware identification is verified, that
remains a system Error instead of being mislabelled as code 2. Data-quality
codes 1 or 3 raise a deduplicated Warning and do not stop the SEQ while route,
trigger, and output state remain known. Communication,
identity, settings-readback, routing, trigger-state, or safe-output failures are
system Errors and stop the SEQ. `Test Connections` always validates the settings
currently shown in the module window; it does not silently use an older saved
or Applied resource address.

The initial current is zero and Apply rejects a zero reversal span. There is no
user-configurable per-channel timeout and no module software current/compliance
cap. Long `*OPC?` waits share the core's total Measure-operation deadline, with
time reserved for safe cleanup. Current and compliance values are still checked
against the 6221's documented command ranges; those device limits are not a DUT
safety certification. This Beta version has protocol-state tests but has not
been validated with the real instruments, switch card, DUT, cabling, or GPIB
controller.

The 2182A digital filter type is fixed to Moving because that is the filter type
defined for Delta measurements. Count and window remain configurable.
