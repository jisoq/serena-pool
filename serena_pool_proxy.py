#!/usr/bin/env python3
"""serena_pool_proxy.py — ленивый project-aware пул Serena для Codex/Kimi/ZCode.

Режим по умолчанию: stdio <-> streamable-http мост.
  - определяет корень проекта по cwd (.serena/project.yml или .git вверх, fallback cwd)
  - детерминированный порт по md5(пути + контекста) + file-lock против двойного запуска
  - лениво поднимает `serena start-mcp-server --transport streamable-http --project <path>`
  - контекст берётся из SERENA_POOL_CONTEXT (перекрывает config.json): один и тот же
    проект может держать отдельные инстансы под разные клиенты (codex, zcode, ...)
  - пишет lease с heartbeat, чтобы reaper не убил живой сервер
  - дедлайн на каждый запрос (request_timeout); зависший Serena -> переподключение
  - при обрыве MCP-сессии переподнимает сервер и реплеит initialize

Режим --reaper: detached watchdog, гасит Serena-инстансы без свежих лизов по TTL.

Все состояния: %USERPROFILE%\\.serena\\pool\\{registry,locks,leases,logs}, config.json.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

POOL_DIR = Path.home() / ".serena" / "pool"
REGISTRY_DIR = POOL_DIR / "registry"
LOCKS_DIR = POOL_DIR / "locks"
LEASES_DIR = POOL_DIR / "leases"
LOGS_DIR = POOL_DIR / "logs"
CONFIG_PATH = POOL_DIR / "config.json"
REAPER_PATH = POOL_DIR / "reaper.json"

DEFAULT_CONFIG = {
    "ttl_seconds": 1200,          # простой Serena без лизов до убийства
    "port_base": 24500,           # диапазон портов пула: [base, base+range)
    "port_range": 200,
    "context": "codex",           # --context для serena
    "serena_exe": "",             # пусто = автодетект ~/.local/bin/serena.exe или PATH
    "extra_args": [],             # доп. аргументы serena start-mcp-server
    "start_timeout": 90,          # ожидание поднятия TCP-порта, сек
    "request_timeout": 600,       # дедлайн одного запроса через мост, сек (0 = без дедлайна)
}

for d in (REGISTRY_DIR, LOCKS_DIR, LEASES_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[serena-pool {ts} pid={os.getpid()}] {msg}", file=sys.stderr, flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            log(f"config.json broken, defaults used: {exc}")
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def serena_exe(cfg: dict) -> str:
    cand = cfg.get("serena_exe") or ""
    if cand and Path(cand).exists():
        return cand
    home_exe = Path.home() / ".local" / "bin" / "serena.exe"
    if home_exe.exists():
        return str(home_exe)
    found = shutil.which("serena")
    if found:
        return found
    raise RuntimeError("serena.exe not found (set serena_exe in pool config.json)")


# --- process utils -----------------------------------------------------------

_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Windows process creation flags
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000  # hide console window for console executables

# STARTUPINFO flags for explicitly hiding the window (prevents brief flash)
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def hidden_startupinfo() -> subprocess.STARTUPINFO:
    """Return STARTUPINFO that forces the new process window to stay hidden."""
    si = subprocess.STARTUPINFO()
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    return si


def pid_alive(pid: int) -> bool:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def kill_tree(pid: int) -> None:
    """taskkill /T /F — убивает serena вместе с LSP-детьми (Roslyn и пр.)."""
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(pid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def tcp_alive(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket_create_connection(port, timeout):
            return True
    except OSError:
        return False


def socket_create_connection(port: int, timeout: float):
    import socket
    return socket.create_connection(("127.0.0.1", port), timeout=timeout)


# --- project / port allocation ----------------------------------------------

def find_project_root(cwd: Path) -> Path:
    """Вверх от cwd: .serena/project.yml сильнее всего, затем .git; fallback — cwd."""
    git_root = None
    for p in (cwd, *cwd.parents):
        if (p / ".serena" / "project.yml").is_file():
            return p
        if git_root is None and (p / ".git").exists():
            git_root = p
    return git_root or cwd


def resolve_context(cfg: dict) -> str:
    """Контекст serena: SERENA_POOL_CONTEXT перекрывает config.json (пул на несколько клиентов)."""
    return (os.environ.get("SERENA_POOL_CONTEXT") or "").strip() or cfg["context"]


def req_timeout(cfg: dict) -> float | None:
    """Дедлайн одного запроса через мост, сек; 0 отключает дедлайн."""
    t = float(cfg.get("request_timeout", 600) or 600)
    return t if t > 0 else None


def norm_path(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def port_candidates(project_path: Path, cfg: dict):
    """Детерминированные (key, port) с линейным пробингом при коллизиях."""
    norm = norm_path(project_path)
    digest = hashlib.md5(f"{norm}::{resolve_context(cfg)}".encode("utf-8")).hexdigest()
    base = cfg["port_base"] + int(digest, 16) % cfg["port_range"]
    for i in range(50):
        key = digest[:8] if i == 0 else f"{digest[:8]}-{i}"
        yield key, base + i


def read_registry(key: str) -> dict | None:
    p = REGISTRY_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_registry(key: str, entry: dict) -> None:
    (REGISTRY_DIR / f"{key}.json").write_text(
        json.dumps(entry, indent=2) + "\n", encoding="utf-8")


def remove_registry(key: str) -> None:
    try:
        (REGISTRY_DIR / f"{key}.json").unlink()
    except FileNotFoundError:
        pass


@contextmanager
def file_lock(key: str, timeout: float = 90.0):
    """Мьютекс на lockfile — не допускает двойного запуска одного проекта."""
    import msvcrt
    f = open(LOCKS_DIR / f"{key}.lock", "a+b")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError(f"lock timeout for {key}")
                time.sleep(0.3)
        yield
    finally:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


# --- server lifecycle --------------------------------------------------------

def _spawn_serena(project_path: Path, key: str, port: int, cfg: dict) -> dict:
    cmd = [
        serena_exe(cfg), "start-mcp-server",
        "--transport", "streamable-http",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--project", str(project_path),
        "--context", resolve_context(cfg),
        *cfg.get("extra_args", []),
    ]
    log_file = open(LOGS_DIR / f"serena-{key}.log", "ab", buffering=0)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        startupinfo=hidden_startupinfo(),
        env=env, close_fds=True,
    )
    entry = {
        "key": key,
        "project_path": str(project_path),
        "context": resolve_context(cfg),
        "port": port,
        "pid": proc.pid,
        "started_at": time.time(),
        "cmd": cmd,
    }
    write_registry(key, entry)
    log(f"spawned serena pid={proc.pid} port={port} project={project_path}")
    return entry


def ensure_server(project_path: Path, cfg: dict, force_restart: bool = False) -> dict:
    """Гарантирует живую Serena для проекта. Возвращает registry entry."""
    for key, port in port_candidates(project_path, cfg):
        existing = read_registry(key)
        if existing and (norm_path(existing.get("project_path", "")) != norm_path(project_path)
                         or existing.get("context", "codex") != resolve_context(cfg)):
            continue  # коллизия хэша — пробуем следующий порт
        if existing is None and tcp_alive(port):
            log(f"port {port} occupied by foreign process, probing next")
            continue
        with file_lock(key):
            entry = read_registry(key)
            if force_restart and entry:
                log(f"force restart: killing pid={entry['pid']}")
                kill_tree(entry["pid"])
                remove_registry(key)
                entry = None
                time.sleep(1.0)
            if entry and pid_alive(entry["pid"]) and tcp_alive(entry["port"]):
                return entry  # переиспользуем
            if entry:
                log(f"stale entry pid={entry['pid']}, cleaning")
                kill_tree(entry["pid"])
                remove_registry(key)
            entry = _spawn_serena(project_path, key, port, cfg)
            deadline = time.time() + cfg["start_timeout"]
            try:
                while time.time() < deadline:
                    if tcp_alive(port):
                        return entry
                    if not pid_alive(entry["pid"]):
                        tail = ""
                        try:
                            tail = (LOGS_DIR / f"serena-{key}.log").read_bytes()[-2000:].decode("utf-8", "replace")
                        except OSError:
                            pass
                        raise RuntimeError(f"serena died at startup, log tail:\n{tail}")
                    time.sleep(1.0)
                raise RuntimeError(f"serena did not open port {port} in {cfg['start_timeout']}s "
                                   f"(see {LOGS_DIR / ('serena-' + key + '.log')})")
            except Exception:
                kill_tree(entry["pid"])  # недоподнявшийся инстанс не оставляем в реестре
                remove_registry(key)
                raise
    raise RuntimeError("no free port candidates for serena pool")


# --- leases ------------------------------------------------------------------

def lease_path_for(key: str, pid: int) -> Path:
    return LEASES_DIR / f"{key}__{pid}.json"


def write_lease(key: str, project_path: Path) -> Path:
    p = lease_path_for(key, os.getpid())
    p.write_text(json.dumps({
        "key": key, "proxy_pid": os.getpid(),
        "project_path": str(project_path), "started_at": time.time(),
    }) + "\n", encoding="utf-8")
    return p


def start_heartbeat(path: Path) -> threading.Event:
    stop = threading.Event()

    def beat():
        while not stop.wait(30):
            try:
                os.utime(path)
            except OSError:
                return

    threading.Thread(target=beat, daemon=True).start()
    return stop


def remove_leases_for_key(key: str) -> None:
    for p in LEASES_DIR.glob(f"{key}__*.json"):
        try:
            p.unlink()
        except OSError:
            pass


def freshest_lease_age(key: str, now: float) -> float | None:
    ages = []
    for p in LEASES_DIR.glob(f"{key}__*.json"):
        try:
            ages.append(now - p.stat().st_mtime)
        except OSError:
            pass
    return min(ages) if ages else None


# --- reaper ------------------------------------------------------------------

def ensure_reaper(cfg: dict) -> None:
    try:
        if REAPER_PATH.exists():
            info = json.loads(REAPER_PATH.read_text(encoding="utf-8"))
            if pid_alive(info.get("pid", -1)):
                return
    except Exception:
        pass
    try:
        log_file = open(LOGS_DIR / "reaper.log", "ab", buffering=0)
        # use pythonw.exe for the reaper to avoid a console window
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if pythonw.exists() else sys.executable
        proc = subprocess.Popen(
            [exe, os.path.abspath(__file__), "--reaper"],
            stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            startupinfo=hidden_startupinfo(),
            close_fds=True,
        )
        log(f"spawned reaper pid={proc.pid}")
    except Exception as exc:
        log(f"reaper spawn failed, continuing without it: {exc}")


def reaper_main() -> int:
    REAPER_PATH.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8")
    log("reaper started")
    while True:
        cfg = load_config()
        ttl = float(cfg["ttl_seconds"])
        now = time.time()
        entries = list(REGISTRY_DIR.glob("*.json"))
        if not entries:
            _clean_orphan_leases(set(), now)
            log("registry empty, reaper exits")
            return 0
        known_keys = set()
        for reg_file in entries:
            try:
                entry = json.loads(reg_file.read_text(encoding="utf-8"))
                key = entry["key"]
            except Exception:
                reg_file.unlink(missing_ok=True)
                continue
            known_keys.add(key)
            age = freshest_lease_age(key, now)
            fresh = age is not None and age <= ttl
            grace = now - float(entry.get("started_at", 0)) <= ttl
            if not pid_alive(entry["pid"]):
                log(f"reaping dead entry {key} (pid={entry['pid']})")
                remove_registry(key)
                remove_leases_for_key(key)
            elif not fresh and not grace:
                log(f"reaping idle {key} (pid={entry['pid']}, port={entry['port']})")
                kill_tree(entry["pid"])
                remove_registry(key)
                remove_leases_for_key(key)
        _clean_orphan_leases(known_keys, now)
        time.sleep(60)


def _clean_orphan_leases(known_keys: set, now: float) -> None:
    for p in LEASES_DIR.glob("*.json"):
        key = p.name.split("__", 1)[0]
        if key in known_keys:
            continue
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


# --- streamable-http client (прозрачный JSON-RPC форвардер) ------------------

class SerenaHttpSession:
    """Минимальный клиент MCP Streamable HTTP: POST + SSE, session-id, 202."""

    def __init__(self, url: str):
        self.url = url
        self.session_id: str | None = None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json"}
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _capture_sid(self, r: httpx.Response) -> None:
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

    async def post_no_reply(self, msg: dict) -> None:
        """Нотификации и ответы на серверные запросы (ожидаем 202)."""
        try:
            r = await self.client.post(self.url, json=msg, headers=self._headers())
            self._capture_sid(r)
            if r.status_code >= 400:
                log(f"notification POST -> HTTP {r.status_code}: {r.text[:300]}")
        except Exception as exc:
            log(f"notification POST failed: {exc}")

    async def post_request(self, msg: dict, on_server_msg) -> dict | None:
        """Запрос клиента -> JSON-RPC ответ. on_server_msg — для чужих сообщений."""
        req_id = msg.get("id")
        async with self.client.stream("POST", self.url, json=msg,
                                      headers=self._headers()) as r:
            self._capture_sid(r)
            if r.status_code == 202:
                return None
            if r.status_code >= 400:
                body = (await r.aread())[:500]
                raise RuntimeError(f"HTTP {r.status_code}: {body!r}")
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                return json.loads((await r.aread()).decode("utf-8"))
            # text/event-stream
            data_lines: list[str] = []
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
                        continue
                    data_lines = []
                    if payload.get("id") == req_id and ("result" in payload or "error" in payload):
                        return payload
                    await on_server_msg(payload)
            raise RuntimeError("SSE stream ended without JSON-RPC response")

    async def close(self) -> None:
        try:
            if self.session_id:
                await self.client.delete(self.url, headers=self._headers())
        except Exception:
            pass
        await self.client.aclose()


class StdoutWriter:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        async with self.lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


async def pump_stdin(queue: asyncio.Queue) -> None:
    loop = asyncio.get_running_loop()

    def reader():
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                break
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    await loop.run_in_executor(None, reader)


# --- bridge ------------------------------------------------------------------

class Bridge:
    def __init__(self, project_path: Path, cfg: dict):
        self.project_path = project_path
        self.cfg = cfg
        self.entry: dict | None = None
        self.session: SerenaHttpSession | None = None
        self.writer = StdoutWriter()
        self.init_msg: dict | None = None
        self.initialized_msg: dict | None = None

    async def connect(self, force_restart: bool = False) -> None:
        self.entry = ensure_server(self.project_path, self.cfg, force_restart=force_restart)
        url = f"http://127.0.0.1:{self.entry['port']}/mcp"
        old = self.session
        self.session = SerenaHttpSession(url)
        if old:
            await old.close()
        if self.init_msg:  # реплей MCP-сессии после рестарта сервера
            log("replaying initialize on fresh serena")
            resp = await asyncio.wait_for(
                self.session.post_request(self.init_msg, self._on_server_msg),
                timeout=req_timeout(self.cfg))
            if resp is None or "error" in resp:
                raise RuntimeError(f"initialize replay failed: {resp}")
            if self.initialized_msg:
                await self.session.post_no_reply(self.initialized_msg)

    async def reconnect(self) -> None:
        try:
            await self.connect(force_restart=False)
        except Exception as exc:
            log(f"soft reconnect failed ({exc}), force restart")
            await self.connect(force_restart=True)

    async def _on_server_msg(self, payload: dict) -> None:
        """Сообщения сервера, пришедшие в чужом SSE-потоке."""
        if "method" in payload and "id" in payload:  # серверный запрос
            if payload["method"] == "ping":
                reply = {"jsonrpc": "2.0", "id": payload["id"], "result": {}}
            else:
                reply = {"jsonrpc": "2.0", "id": payload["id"],
                         "error": {"code": -32601, "message": "method not found (pool proxy)"}}
            await self.session.post_no_reply(reply)
        elif "method" in payload:  # нотификация (progress, logging) — клиенту
            await self.writer.send(payload)

    async def handle_request(self, msg: dict) -> None:
        if msg.get("method") == "initialize":
            self.init_msg = msg
        timeout = req_timeout(self.cfg)
        try:
            resp = await asyncio.wait_for(
                self.session.post_request(msg, self._on_server_msg), timeout=timeout)
        except (httpx.TransportError, RuntimeError, asyncio.TimeoutError) as exc:
            log(f"request {msg.get('method')} failed ({exc!r}), reconnecting")
            try:
                await self.reconnect()
                resp = await asyncio.wait_for(
                    self.session.post_request(msg, self._on_server_msg), timeout=timeout)
            except Exception as exc2:
                resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32000,
                                  "message": f"serena pool proxy: {exc2}"}}
        except Exception as exc:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32000, "message": f"serena pool proxy: {exc}"}}
        if resp is not None:
            await self.writer.send(resp)

    async def handle_notification(self, msg: dict) -> None:
        if msg.get("method") == "notifications/initialized":
            self.initialized_msg = msg
        await self.session.post_no_reply(msg)

    async def run(self) -> None:
        await self.connect()
        ensure_reaper(self.cfg)
        lease = write_lease(self.entry["key"], self.project_path)
        stop_hb = start_heartbeat(lease)
        queue: asyncio.Queue = asyncio.Queue()
        stdin_task = asyncio.create_task(pump_stdin(queue))
        pending: set[asyncio.Task] = set()

        def spawn(coro):
            t = asyncio.create_task(coro)
            pending.add(t)
            t.add_done_callback(pending.discard)

        try:
            while True:
                line = await queue.get()
                if line is None:  # EOF — клиент закрыл сессию
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log(f"bad json from client: {line[:200]!r}")
                    continue
                if "method" in msg and "id" in msg:
                    spawn(self.handle_request(msg))
                elif "method" in msg:
                    spawn(self.handle_notification(msg))
                else:
                    log("dropping client->server response (no forwarded server requests)")
        finally:
            stdin_task.cancel()
            for t in pending:
                t.cancel()
            stop_hb.set()
            try:
                lease.unlink()
            except OSError:
                pass
            if self.session:
                await self.session.close()


def proxy_main() -> int:
    cfg = load_config()
    project_path = find_project_root(Path.cwd())
    log(f"project root: {project_path}")
    bridge = Bridge(project_path, cfg)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log(f"fatal: {exc}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Serena pool proxy / reaper")
    parser.add_argument("--reaper", action="store_true", help="run idle-TTL reaper loop")
    args = parser.parse_args()
    if args.reaper:
        return reaper_main()
    return proxy_main()


if __name__ == "__main__":
    sys.exit(main())
