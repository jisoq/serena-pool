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
- derives a deterministic port from the project path, context and isolation profile, with linear probing on
  collisions and a cross-process file lock against double spawns;
- lazily starts `serena start-mcp-server --transport streamable-http --project <path>`
  as a detached, hidden-window process, using a context with `single_project: true`;
- routes `activate_project` to a separate HTTP server for that project, keeping other
  clients on their existing server and preserving their REPL sessions;
- writes a lease with a heartbeat; a detached reaper kills instances that received no
  fresh leases for `ttl_seconds`;
- bridges MCP JSON-RPC both ways (POST + SSE, session ids, `202` notifications,
  server pings);
- if the backing server dies, restarts it, replays `initialize`, and obtains a new
  Serena session. Variables in the terminated server's REPL are lost;
- enforces a per-request deadline (`request_timeout`): a wedged server triggers
  a reconnect instead of hanging the client forever. A client timeout does not kill a
  shared, running server. If retry also fails, the client receives an error;
- checks Windows process exit status and restarts a stopped reaper from live client
  heartbeats. Dead proxy leases are removed even while their server remains active.

The same project can hold separate instances per client context (`codex`, `zcode`, ...)
— see `SERENA_POOL_CONTEXT` below. State lives in `~/.serena/pool/`:

| dir | contents |
|---|---|
| `registry/` | one JSON per live instance (key, port, pid, context) |
| `locks/` | cross-process spawn locks |
| `leases/` | heartbeat files, one per connected proxy |
| `logs/` | per-instance serena logs + reaper log |
| `contexts/` | copies of the selected Serena contexts with project switching disabled |

Serena owns one active project per HTTP server. The proxy therefore handles project
switches itself: it opens another project's server and remaps only that client's Serena
session ID. Requests from the same client are ordered around a switch. Requests from
different clients can run concurrently. Native server-side project switching is disabled,
including direct calls to its HTTP endpoint.

Builtin contexts are exported from the installed Serena through its public CLI without
opening an editor. Custom context files and registered user contexts are copied without
changing their source. Their options are preserved except for `single_project`.

## Install

Requires Windows, Python 3.10+, and [Serena](https://github.com/oraios/serena) on `PATH`
or at `~/.local/bin/serena.exe`. Project isolation is tested with Serena 2.1.0 using its
REPL and TOOLS interfaces. Dependencies are `httpx` and `PyYAML`.

Run directly from git with [uv](https://docs.astral.sh/uv/):

    uvx --from git+https://github.com/svetkis/serena-pool serena-pool

or clone and install into an independent uv tool environment:

    git clone https://github.com/svetkis/serena-pool
    uv tool install ./serena-pool

## Client configuration

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.serena]
command = "C:\\path\\to\\serena-pool.exe"
args = []
startup_timeout_sec = 120

[mcp_servers.serena.env]
PYTHONUTF8 = "1"
```

Do not set a fixed `cwd`: the client working directory selects the initial project.
On upgrade, reconnect clients to the new proxy when their current work permits it.
The isolation profile uses separate registry keys, so new clients cannot accidentally
reuse older servers that allow project switching. Older servers remain available to
their existing clients and expire after their leases stop and the idle TTL elapses.

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

`extra_args` cannot override the project, context, transport, host, or port: the pool
owns these arguments to maintain project isolation and registry consistency.

Environment:

- `SERENA_POOL_CONTEXT` — overrides `context` for this proxy invocation without touching
  the shared config. Instances are keyed per (project, context) pair, so Codex and ZCode
  opening the same project each get their own Serena instance with the right tool set.

## Smoke test

    python examples/pool_test_client.py <project_dir>

Runs `initialize` → `tools/list` → a tool call through the proxy and prints each step.

Regression tests (PowerShell):

```powershell
python -m unittest discover -s tests -p test_pool.py -v
$env:SERENA_TEST_EXE = 'C:\path\to\serena.exe'
$env:SERENA_POOL_INTEGRATION = '1'
python -m unittest discover -s tests -v
```

The opt-in integration test uses a temporary home and two temporary projects. It checks
concurrent clients, project switching, separate HTTP ports, native project pinning,
REPL session isolation, server reuse, reaper recovery, backend crash recovery and idle
cleanup, plus the default TOOLS interface. It terminates only its own temporary servers
and reaper. It downloads language-server dependencies and a managed Python into its
temporary home, so network access is required. Failed runs retain their logs there.

## Platform status

Windows-first: process control uses `msvcrt` locks, `taskkill /T`, hidden-window creation
flags and `pythonw.exe` for the detached reaper. Linux/macOS support is not implemented
yet; patches welcome.

## License

MIT. This project is an independent process that talks to Serena over its public MCP
interface; it contains no Serena code. Serena itself is GPL-3.0-or-later since v2.0.0
(SolidLSP remains MIT).
