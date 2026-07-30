# Simulated Transport

Hardware-free reference Measurement Module. One Measure emits four sparse rows,
one for each of R1-R4.

The DAT values are numeric. Each row contains the current resistance and one
module-specific integer `StatusCode`:

- `0`: normal;
- `1`: the simulated resistance exceeded `warning_threshold_ohm`.

Code 1 also raises a deduplicated, human-readable Warning, but the Warning text
is written to `events.dat`, not the experiment DAT. A code 1 row leaves the
corresponding R1-R4 value empty. This module defines no other nonzero status
codes.
