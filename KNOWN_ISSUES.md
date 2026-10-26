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

## 2. A freed lower port can shadow an existing warm instance (collision case)

`ensure_server()` iterates candidates from `i = 0` and takes the first one that
is free or already ours. It never checks whether *another* candidate already
holds a live entry for the same `(project, context)`.

Scenario:

1. Projects A and B hash to the same base port; A spawns first and takes `base`,
   B is displaced to `base + 1` (registered under key `<digest>-1`).
2. A dies / is reaped; `base` becomes free, B's warm instance still lives on
   `base + 1`.
3. A new proxy session for B starts scanning at `i = 0`: no registry file under
   B's primary key, `base` does not answer → it cold-spawns a **second** Serena
   instance for B on `base`, instead of reusing the warm one on `base + 1`.

- **Impact:** a duplicate Serena instance (with its language servers) for up to
  `ttl_seconds` (20 min by default) until the reaper kills the lease-less one.
  Requires a hash collision *and* the lower port freeing *and* a reconnect.
- **Possible fix:** before claiming a free candidate, scan all candidates for a
  live registry entry matching `(project, context)` and reuse it.
