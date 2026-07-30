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

The current `simulated_transport` 1.0.2 requires OpenLab Control 0.11.0 Beta 2
or newer because it uses the live, interruptible measurement context.
`lakeshore_372a` 0.1.0b8 requires OpenLab Control 0.11.1 or newer because its
PyVISA runtime is supplied and version-checked by the core framework.
`lr700` 0.1.0b4 has the same core requirement and shared PyVISA boundary.
`keithley_6221_2182a_delta` 0.1.0b3 requires the rawdata API introduced in
OpenLab Control 0.11.4 development builds. All hardware modules remain beta
until verified with their real instruments.

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
- Test multi-row output and parallel execution with another module.
- Test bounded driver and framework timeouts plus forced worker cleanup.
- Test the exact offline wheel set on the target Windows/Python architecture.
- Increment `version` whenever shipped content or dependencies change.
