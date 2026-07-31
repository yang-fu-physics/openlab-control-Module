# OpenLab Measurement Modules repository

This is the Git-ready layout for the single shared Measurement Module
repository. Keep every independently installable module under `modules/<id>/`.
The normative lifecycle, safety, data, dependency, UI, testing, and release
rules for new modules are defined in
[MODULE_SPECIFICATION.md](MODULE_SPECIFICATION.md).
The included `simulated_transport` module is hardware-free and serves as the
reference implementation and test fixture. `lakeshore_372a` is the first
hardware module; it scans up to four selectable Model 372/372A inputs over
GPIB and emits one sparse DAT row per enabled slot. `lr700` controls an
LR-700 bridge with one LR-720-16 multiplexer, lets R1-R4 select four physical
sensor inputs, and emits one sparse R/X/status row per enabled slot.
`keithley_6221_2182a_delta` coordinates a 6221, its serial/Trigger-Link 2182A,
and an optional 7001 for four-channel Delta measurements. It converts raw
Delta voltage to resistance and stores the voltage sequence in a core-managed
rawdata sidecar.
`keithley_6221_2182a_delta_3706a` provides the same two Delta operating modes
and data contract with an optional 3706/3706A switch mainframe. Its routing
layer uses the 3700A case-sensitive TSP command set and verifies the complete
closed-channel list before every trigger.
`keithley_2400` uses one Model 2400 as a constant-current or constant-voltage
source and measures two-wire or four-wire resistance. `keithley_6517b` uses
the 6517B voltage source and ammeter for two-wire high-resistance measurements;
it establishes and verifies the required internal METER-CONNECT path before
any voltage is applied. `keithley_2614b` measures one or both SMU channels with
independent constant-current/constant-voltage and two-wire/four-wire settings.

All modules explicitly declare the Measurement Module API 1.1 scheduling mode.
The scanner/switch modules use `aligned_slots`: one `T Measure` produces one DAT
row per logical channel slot, and modules on the same slot run in parallel and
share that row. The 2400, 6517B, and 2614B use `once_per_slot`, so they perform a
fresh measurement in every logical channel row. If no scanner module is enabled,
there is one logical slot and therefore one row. A missing mode declaration is
shown as a compatibility warning and is treated as `once_per_slot`, but is not
accepted for an official module release.

The current `simulated_transport` 1.1.0, `lakeshore_372a` 0.1.0b9, `lr700`
0.1.0b5, `keithley_6221_2182a_delta` 0.1.0b5, and
`keithley_6221_2182a_delta_3706a` 0.1.0b2 require OpenLab Control 0.11.5 or
newer for API 1.1 logical-slot scheduling.
`keithley_2400`, `keithley_6517b`, and `keithley_2614b` 0.1.0b1 require
OpenLab Control 0.11.5 or newer and use the framework-provided PyVISA runtime.
All hardware modules remain beta until verified with their real instruments.

## Manual offline installation

1. Review the module source and `module.toml`. If the module declares
   dependencies not supplied by the OpenLab Control framework, also review
   its `requirements.lock` and wheels.
2. Copy one complete module folder to `OpenLabControl/modules/<id>/`.
3. Framework dependencies such as PySide6, PyVISA, packaging, QtAwesome, and
   typing_extensions use the versions shipped by OpenLab Control. Do not
   duplicate their wheels in each module.
4. Only for additional third-party dependencies, include every required
   Windows wheel under that module's `wheels/` folder (or the application's
   shared `wheels/` folder). Restart OpenLab Control and use
   `Install Dependencies` when the button is shown. Network fallback is
   intentionally unavailable.
5. Enable the module and approve the first-load trust prompt.
6. Verify that saved settings are loaded but not applied until the operator
   chooses `Apply Settings`.

Never commit `module_data`, acquired DAT files, instrument addresses containing
secrets, or generated `plugin_runtime` contents. A module owns its measurement
instruments, runs its backend in one child process, and may only read the
temperature/field/monitor snapshot supplied by the core.

## Required release checks

- Validate manifest ID, API/core range, fixed columns, and source entry points.
- Exercise initialize/apply/begin/measure/end/abort and every error path.
- Verify Warning deduplication and Error termination.
- Test explicit scheduling mode, slot union, one row per logical channel, and
  same-slot parallel execution with another module.
- Test bounded driver and framework timeouts plus forced worker cleanup.
- Test the exact offline wheel set on the target Windows/Python architecture.
- Increment `version` whenever shipped content or dependencies change.
