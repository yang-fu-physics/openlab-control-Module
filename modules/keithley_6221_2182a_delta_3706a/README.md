# Keithley 6221 + 2182A Delta + 3706A

Version `0.1.0b1` is a Beta Measurement Module for a Keithley 6221 current
source, a Keithley 2182A nanovoltmeter connected to the 6221 by RS-232 and
Trigger Link, and an optional Keithley 3706A switch system controlled through a
second GPIB resource. It requires OpenLab Control `>=0.11.4,<0.12` because
every formal DAT row has a core-managed rawdata row.

Before using Apply, manually enable the 2182A RS-232 interface at 19.2 kbaud,
XON/XOFF flow control, and CR termination; configure the 6221 serial side to
match, keep the 6221 computer interface on GPIB, and connect both RS-232 and
Trigger Link cables. The module verifies that the 6221 reports the 2182A and
checks the relayed 2182A identity, but it does not silently change these panel
communication settings.

The 3706A must use its native, case-sensitive TSP command set. The module
identifies it with `print(localnode.model)`, accepts 3706A variants and the
documented `3706` compatibility identity, and never sends Keithley 7001 SCPI
routing commands to it.

The switch implementation follows the *Series 3700A System Switch/Multimeter
User's Manual*, part 3700AS-900-01 Rev. B (July 2016), together with the Series
3700A TSP command reference for identity and closed-channel readback.

## Operating modes

- **Shared configuration / continuous Armed**: `begin_sequence` applies the
  common settings, sends `SOUR:DELT:ARM`, waits at least 3 seconds through the
  interruptible module context, verifies Armed state, and remains Armed until
  the SEQ ends. Each Measure switches enabled channels and starts a finite
  Delta acquisition by software trigger.
- **Independent configuration / re-arm per channel**: before every route
  change, the module aborts Delta and confirms zero/off state. After switching,
  it applies that channel's complete settings, arms, waits 3 seconds, verifies,
  and triggers.

## DAT and rawdata

One Measure writes one DAT row per enabled logical channel. The fixed DAT
columns are `Channel`, `Resistance`, `Current`, `StdDev`, `SampleCount`, and
`StatusCode`. Resistance is each valid 2182A Delta voltage divided by the
effective reversal current. The formal resistance is the arithmetic mean of all
results for that channel, and `StdDev` is the sample standard deviation (`0`
for one result).

The per-cycle Delta voltage readings are written without a header to
`rawdata/<data-file>__<path-digest>__keithley_6221_2182a_delta_3706a.rawdata`.
Each rawdata line corresponds to one formal DAT row. Delta count is limited to
32,768 so the complete raw sequence remains within the core's 1 MiB IPC frame.
The formal DAT intentionally has no voltage column.

## Hidden 3706A routing

`routing.toml` is intentionally not editable in the UI. Its translated default
Slot 1 TSP routes are:

- CH1: `1001`, `1011`, `1005`, `1015`
- CH2: `1002`, `1012`, `1005`, `1015`
- CH3: `1003`, `1013`, `1005`, `1015`
- CH4: `1004`, `1014`, `1005`, `1015`

These defaults are an example topology, not an automatic card definition.
Before connecting a DUT, compare every address with the installed switch-card
manual and physical four-wire connections. Edit the four complete channel
lists and restart OpenLab Control when the card, slot, pole mode, or wiring
differs. The backend validates the four-digit address shape, but only
real-hardware verification can prove that an address exists and controls the
intended relay.

Every route change is break-before-make:

1. Confirm the 6221 current is zero.
2. Send `channel.open("allslots")`.
3. Read `channel.getclose("allslots")` and require no closed relay anywhere in
   the mainframe.
4. Send one `channel.exclusiveclose("<four-channel-list>")`.
5. Read the full closed-channel list again and require an exact match before
   allowing a software trigger.

Four-pole replies such as `1001(1031)` are expanded and checked as two physical
relays. Missing, extra, duplicate, or malformed routes are fatal. A route write
is never automatically replayed because it may already have reached the
instrument.

If the 3706A resource is blank or cannot be identified during Enable, the
module raises a deduplicated Warning and fixes the current Enable session to
direct CH1-only operation. A 3706A failure after successful Enable is fatal;
the module does not silently fall back while a route may be unknown.

## Data status and errors

`StatusCode` is module-specific and contains no text:

- `0`: normal;
- `1`: a finite 2182A voltage exceeds the supported range;
- `2`: reserved for a positively identified 6221 compliance event;
- `3`: an invalid, non-finite, or incomplete Delta trace.

For simultaneous data faults, an invalid or incomplete trace has priority over
voltage overrange and produces code 3. Code 2 is not currently emitted; it is
reserved until a real 6221 compliance condition can be positively identified
and tested.

Every nonzero row leaves `Resistance` and `StdDev` empty. Numeric diagnostic
metadata such as `Channel`, requested/effective `Current`, `SampleCount`, and
the raw 2182A sequence remain available; they are not presented as a valid
resistance result.

The module does not infer compliance from arbitrary `SYST:ERR?` text.
Data-quality codes 1 or 3 raise a deduplicated Warning and do not stop the SEQ
while route, trigger, and output state remain known. Communication, identity,
settings-readback, routing, trigger-state, or safe-output failures are system
Errors and stop the SEQ. `Test Connections` validates the settings currently
shown in the module window rather than silently using an older saved or Applied
resource address.

## Safety state and hardware validation

The initial current is zero and Apply rejects a zero reversal span. There is no
user-configurable per-channel timeout and no module software current/compliance
cap. Long `*OPC?` waits share the core's total Measure-operation deadline, with
time reserved for safe cleanup. Current and compliance values are still checked
against the 6221's documented command ranges; those device limits are not a DUT
safety certification. Apply, Stop, Error, completed SEQ, Disable, and application
shutdown all request `SOUR:SWE:ABOR`, `SOUR:CLE`, then query `OUTP?` and
`SOUR:CURR?`. When the 3706A is connected, they also open all slots and require
an empty `channel.getclose("allslots")` result.

If either instrument has stopped communicating, software cannot prove the
physical output or relay state. Treat that as Safety Unconfirmed: inspect the
6221 output/current and the 3706A closed-channel display at the front panels,
remove the source from the sample if necessary, and do not rely on worker
termination as a hardware interlock.

This Beta version has fake-transport protocol and state-machine tests but has
not been validated with a real 6221, 2182A, 3706/3706A, installed switch card,
DUT, cabling, firmware, or VISA controller. First validation must use a
non-critical load and the lowest practical current, then verify every physical
route and all Stop/Error/Disable paths before connecting a real sample.

The 2182A digital filter type is fixed to Moving because that is the filter type
defined for Delta measurements. Count and window remain configurable.
