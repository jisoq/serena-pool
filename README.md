# serena-pool

Project-aware pooling proxy for [Serena](https://github.com/oraios/serena) MCP servers.

Serena has no built-in pooling: a `stdio` server dies with every client session, so each
new session pays a cold start for the language servers behind it. `serena-pool` keeps
warm, per-project Serena instances alive between sessions and shares them across clients
(Codex, ZCode, Claude Code, ...) through a lightweight `stdio <-> streamable-http` bridge.

## How it works

The proxy is a single-file Python program you register as an MCP server (stdio) in your
client:

- detects the project root from the working directory (`.serena/project.yml` or `.git`
  upwards, fallback: cwd);
- derives a deterministic port from `md5(project path + context)`, with linear probing on
  collisions and a cross-process file lock against double spawns;
- lazily starts `serena start-mcp-server --transport streamable-http --project <path>`
  as a detached, hidden-window process;
- writes a lease with a heartbeat; a detached reaper kills instances that received no
  fresh leases for `ttl_seconds`;
- bridges MCP JSON-RPC both ways (POST + SSE, session ids, `202` notifications,
  server pings);
- if the backing server dies, transparently restarts it and replays `initialize` —
  the client never notices;
- enforces a per-request deadline (`request_timeout`): a wedged server triggers
  reconnect/restart instead of hanging the client forever.

The same project can hold separate instances per client context (`codex`, `zcode`, ...)
— see `SERENA_POOL_CONTEXT` below. State lives in `~/.serena/pool/`:

| dir | contents |
|---|---|
| `registry/` | one JSON per live instance (key, port, pid, context) |
| `locks/` | cross-process spawn locks |
| `leases/` | heartbeat files, one per connected proxy |
| `logs/` | per-instance serena logs + reaper log |

## Install

Requires Python 3.10+ and [Serena](https://github.com/oraios/serena) on `PATH` or at
`~/.local/bin/serena.exe`. The only dependency is `httpx`.

Run directly from git with [uv](https://docs.astral.sh/uv/):

    uvx --from git+https://github.com/svetkis/serena-pool serena-pool

or clone and run with any interpreter that has `httpx`:

    git clone https://github.com/svetkis/serena-pool
    pip install httpx
    python serena_pool_proxy.py

## Client configuration

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.serena]
command = "C:\\path\\to\\python.exe"          # any python with httpx
args = ["C:\\path\\to\\serena_pool_proxy.py"]
```

ZCode (`~/.zcode/cli/config.json`) or any Claude-style MCP config:

```json
{
  "mcp": {
    "servers": {
      "serena": {
        "type": "stdio",
        "command": "uvx",
        "args": ["--from", "git+https://github.com/svetkis/serena-pool", "serena-pool"],
        "env": { "SERENA_POOL_CONTEXT": "zcode" }
      }
    }
  }
}
```

## Configuration

Global config: `~/.serena/pool/config.json` (created with defaults on first run).

| key | default | meaning |
|---|---|---|
| `ttl_seconds` | `1200` | idle time without fresh leases before the reaper kills an instance |
| `port_base` / `port_range` | `24500` / `200` | port window for pool instances |
| `context` | `"codex"` | Serena context (`--context`) for spawned servers |
| `serena_exe` | `""` | explicit path to the `serena` executable; empty = autodetect |
| `extra_args` | `[]` | extra args for `serena start-mcp-server` |
| `start_timeout` | `90` | seconds to wait for the server port to open |
| `request_timeout` | `600` | per-request deadline through the bridge, seconds (`0` disables) |

Environment:

- `SERENA_POOL_CONTEXT` — overrides `context` for this proxy invocation without touching
  the shared config. Instances are keyed per (project, context) pair, so Codex and ZCode
  opening the same project each get their own Serena instance with the right tool set.

## Smoke test

    python examples/pool_test_client.py <project_dir>

Runs `initialize` → `tools/list` → a tool call through the proxy and prints each step.

## Platform status

Windows-first: process control uses `msvcrt` locks, `taskkill /T`, hidden-window creation
flags and `pythonw.exe` for the detached reaper. Linux/macOS support is not implemented
yet; patches welcome.

## License

MIT. This project is an independent process that talks to Serena over its public MCP
interface; it contains no Serena code. Serena itself is GPL-3.0-or-later since v2.0.0
(SolidLSP remains MIT).
