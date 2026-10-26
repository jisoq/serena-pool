# Known issues

Small rough edges found during review (2026-10-25), with resolution status below.
Recorded here so they are not rediscovered from scratch.

## 1. Port probing can step outside the declared port window (fixed)

Previously, `port_candidates()` computed `base = port_base + int(md5, 16) % port_range`
(max. `port_base + port_range - 1`, i.e. 24699 with defaults) and then probed
`base + i` for `i` in `0..49`. In a collision chain the actual port can reach
`port_base + port_range + 48` (24748 with defaults), i.e. outside the nominal
`[24500, 24700)` window documented in the README.

- **Impact:** cosmetic / documentation mismatch. Needs ~17+ project+context pairs
  (birthday bound on 200 ports) before collisions even start.
- **Resolution:** probes now wrap into the configured window and stop before
  repeating candidates when `port_range` is smaller than 50. Covered by
  `tests/test_pool.py`.

## 2. A freed earlier candidate can shadow an existing warm instance (fixed)

Previously, `ensure_server()` allocated the first free candidate before checking
whether a later candidate held a live server for the same project and context.

Scenario:

1. Another process occupies a project's first candidate port. The pool starts
   Serena on a later candidate, such as the one registered under `<digest>-1`.
2. The other process exits while the warm Serena server stays alive.
3. A new proxy session previously started a second Serena server on the freed
   port instead of reusing the warm server. A collision between projects can
   produce the same sequence.

- **Impact:** the duplicate server consumed memory and ran its own language
  servers. Existing duplicates still follow normal idle cleanup. The reaper
  checks every 60 seconds and removes a live server once its startup grace has
  expired and it has no fresh lease. Both periods use `ttl_seconds`, which
  defaults to 20 minutes.
- **Resolution:** `ensure_server()` checks all candidates for a matching live
  server before allocating a port. Each reuse check holds the candidate lock.
  Explicit force restarts keep the existing replacement behavior.
- **Coverage:** `tests/test_pool.py` checks reuse, project and context mismatches,
  stale servers, and force restarts. The opt-in real stdio test in
  `tests/test_integration.py` reserves the first candidate, frees it, and checks
  that reconnecting keeps the same server PID, port, and server count.
