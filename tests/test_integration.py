"""Opt-in Windows integration tests against a locally installed Serena.

Run with SERENA_POOL_INTEGRATION=1 and optionally SERENA_TEST_EXE pointing to serena.exe.
All pool state, contexts, registries, leases and test projects use a temporary home.
"""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

if os.name != "nt":
    raise unittest.SkipTest("serena-pool currently supports Windows only")

import serena_pool_proxy as pool


class Client:
    def __init__(self, command, cwd, env, log):
        self.error_log = log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.error_log,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.waiting = {}
        self.next_id = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def begin(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        return self.next_id

    def response(self, request_id, timeout=120):
        deadline = time.monotonic() + timeout
        while request_id not in self.waiting:
            message = self.messages.get(timeout=max(0.01, deadline-time.monotonic()))
            if message is None:
                raise RuntimeError("Proxy closed stdout")
            if "id" in message:
                self.waiting[message["id"]] = message
        message = self.waiting.pop(request_id)
        if "error" in message:
            raise RuntimeError(message["error"])
        return message["result"]

    def request(self, method, params=None):
        return self.response(self.begin(method, params))

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def initialize(self):
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "serena-pool-integration", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = self.call("initial_instructions")
        return re.search(r"Your Serena session id is `([^`]+)`", result["content"][0]["text"]).group(1)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=15)
        self.reader.join(timeout=5)
        self.process.stdout.close()
        self.process.stdin.close()
        self.error_log.close()


@unittest.skipUnless(os.environ.get("SERENA_POOL_INTEGRATION") == "1", "Set SERENA_POOL_INTEGRATION=1 for live Serena tests")
class LiveIsolationTests(unittest.TestCase):
    def test_routing_native_pin_reuse_recovery_and_cleanup(self):
        serena = os.environ.get("SERENA_TEST_EXE") or shutil.which("serena")
        self.assertIsNotNone(serena, "Install Serena before running integration tests")
        home = Path(tempfile.mkdtemp(prefix="serena-pool-integration-")).resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        completed = False
        print(f"Integration state: {home}", flush=True)
        pool_dir = home / ".serena/pool"
        pool_dir.mkdir(parents=True)
        config = {**pool.DEFAULT_CONFIG, "serena_exe": serena,
                  "extra_args": ["--enable-web-dashboard=false", "--open-web-dashboard=false"]}
        (pool_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (home / ".serena/serena_config.yml").write_text(pool.yaml.safe_dump({
            "projects": [], "language_backend": "LSP", "agent_interface": "REPL",
            "web_dashboard": False, "gui_log_window": False,
            "base_modes": ["interactive", "editing", "no-memories"],
            "project_serena_folder_location": "$projectDir/.serena",
        }), encoding="utf-8")
        projects = []
        for name in ("alpha", "beta"):
            project = home / name
            (project / ".serena").mkdir(parents=True)
            (project / ".serena/project.yml").write_text(pool.yaml.safe_dump({"project_name": name, "language_servers": ["python"]}), encoding="utf-8")
            (project / "sample.py").write_text(f'def identity():\n    return "{name}"\n', encoding="utf-8")
            projects.append(project)
        # Store Python aliases do not work reliably under a temporary USERPROFILE.
        # Keep uv's managed interpreter and tool cache inside the test home too.
        env = {**os.environ, "USERPROFILE": str(home), "SERENA_HOME": str(home / ".serena"), "PYTHONUTF8": "1",
               "UV_PYTHON_PREFERENCE": "only-managed", "UV_PYTHON_INSTALL_DIR": str(home / "uv-python"),
               "UV_CACHE_DIR": str(home / "uv-cache")}
        env.pop("SERENA_POOL_CONTEXT", None)
        command = [sys.executable, str(Path(pool.__file__).resolve())]
        clients = []

        def entry(project):
            return next(e for p in (pool_dir / "registry").glob("*.json")
                        if (e := json.loads(p.read_text(encoding="utf-8")))["project_path"] == str(project))

        def code(client, session, source):
            result = client.call("serena_repl", {"session": session, "code": source})
            self.assertFalse(result.get("isError"), result)
            return result["content"][0]["text"]

        def wait_for(predicate, timeout):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.2)
            self.fail(f"Condition was not met within {timeout}s; state retained at {home}")

        try:
            for i, project in enumerate((projects[0], projects[0], projects[1])):
                client = Client(command, project, env, home / f"client-{i}.log")
                clients.append(client)
                client.sid = client.initialize()
            first, second, third = clients
            alpha, beta = entry(projects[0]), entry(projects[1])
            self.assertNotEqual(alpha["pid"], beta["pid"])
            self.assertNotEqual(alpha["port"], beta["port"])
            self.assertEqual(len(list((pool_dir / "registry").glob("*.json"))), 2)
            self.assertEqual(code(second, second.sid, 'marker = "alpha-client"\nmarker'), "alpha-client")
            self.assertEqual(code(third, third.sid, 'marker = "beta-client"\nmarker'), "beta-client")
            find = 's.lsp.find_symbol("identity", relative_path="sample.py", include_body=True)'

            # Queue a switch and a lookup together. The latter must use the new route.
            switch = first.begin("tools/call", {"name": "activate_project", "arguments": {"project": "beta", "session": first.sid}})
            lookup = first.begin("tools/call", {"name": "serena_repl", "arguments": {"session": first.sid, "code": find}})
            self.assertFalse(first.response(switch).get("isError"))
            self.assertIn('beta', first.response(lookup)["content"][0]["text"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(code, second, second.sid, find)
                b = executor.submit(code, third, third.sid, find)
                self.assertIn('alpha', a.result())
                self.assertIn('beta', b.result())
            self.assertEqual(code(second, second.sid, "marker"), "alpha-client")
            self.assertEqual(code(third, third.sid, "marker"), "beta-client")
            self.assertEqual(entry(projects[0])["pid"], alpha["pid"])
            self.assertEqual(entry(projects[1])["pid"], beta["pid"])
            self.assertNotIn("marker", code(first, first.sid, "s.vars()"))
            missing = first.call("activate_project", {"project": str(home / "missing"), "session": first.sid})
            self.assertTrue(missing["isError"])
            self.assertIn('beta', code(first, first.sid, find))

            async def native_pin():
                session = pool.SerenaHttpSession(f"http://127.0.0.1:{alpha['port']}/mcp")
                async def ignore(_):
                    pass
                try:
                    await session.post_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pin-test", "version": "1"}}}, ignore)
                    await session.post_no_reply({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    tools = await session.post_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ignore)
                    self.assertNotIn("activate_project", [t["name"] for t in tools["result"]["tools"]])
                    denied = await session.post_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "activate_project", "arguments": {"project": str(projects[1]), "session": second.sid}}}, ignore)
                    self.assertTrue("error" in denied or denied.get("result", {}).get("isError"))
                finally:
                    await session.close()
            asyncio.run(native_pin())
            self.assertIn('alpha', code(second, second.sid, find))
            print("PASS: concurrent isolation, native project pinning, routing, session isolation and reuse", flush=True)

            reaper_path = pool_dir / "reaper.json"
            old_reaper = json.loads(reaper_path.read_text(encoding="utf-8"))["pid"]
            pool.kill_tree(old_reaper)

            def reaper_restarted():
                try:
                    pid = json.loads(reaper_path.read_text(encoding="utf-8"))["pid"]
                except (FileNotFoundError, json.JSONDecodeError):
                    # The reaper publishes this file while clients are polling it.
                    return False
                return pid != old_reaper and pool.pid_alive(pid)

            wait_for(reaper_restarted, 45)
            self.assertEqual(entry(projects[0])["pid"], alpha["pid"])
            self.assertEqual(entry(projects[1])["pid"], beta["pid"])
            self.assertIn('alpha', code(second, second.sid, find))
            print("PASS: heartbeat restarts the reaper without restarting project servers", flush=True)

            # A real backend crash must remap the caller's old REPL session on retry.
            pool.kill_tree(alpha["pid"])
            self.assertIn('alpha', code(second, second.sid, find))
            restarted = entry(projects[0])
            self.assertNotEqual(restarted["pid"], alpha["pid"])
            self.assertEqual(entry(projects[1])["pid"], beta["pid"])
            self.assertEqual(code(third, third.sid, "marker"), "beta-client")
            alpha = restarted
            print("PASS: backend crash recovers the client session without touching the other project", flush=True)

            for client in clients:
                client.close()
            clients.clear()
            config["ttl_seconds"] = 1  # Private test home only; no live clients remain.
            (pool_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            wait_for(lambda: not list((pool_dir / "registry").glob("*.json")), 70)
            self.assertFalse(pool.pid_alive(alpha["pid"]))
            self.assertFalse(pool.pid_alive(beta["pid"]))
            self.assertEqual(list((pool_dir / "leases").glob("*.json")), [])
            print("PASS: idle TTL removes stopped backends and leases", flush=True)

            # The original proxy also supports Serena's default TOOLS interface.
            config["ttl_seconds"] = pool.DEFAULT_CONFIG["ttl_seconds"]
            (pool_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            serena_config = home / ".serena/serena_config.yml"
            settings = pool.yaml.safe_load(serena_config.read_text(encoding="utf-8"))
            settings["agent_interface"] = "TOOLS"
            serena_config.write_text(pool.yaml.safe_dump(settings), encoding="utf-8")
            tools_client = Client(command, projects[0], env, home / "tools-client.log")
            clients.append(tools_client)
            tools_client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "tools-test", "version": "1"}})
            tools_client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            instructions = tools_client.call("initial_instructions")
            self.assertFalse(instructions.get("isError"))
            switched = tools_client.call("activate_project", {"project": "beta"})
            self.assertFalse(switched.get("isError"))
            found = tools_client.call("find_symbol", {"name_path_pattern": "identity", "relative_path": "sample.py", "include_body": True})
            self.assertFalse(found.get("isError"), found)
            self.assertIn('beta', found["content"][0]["text"])
            print("PASS: default TOOLS interface routes projects without a REPL session argument", flush=True)
            completed = True
        finally:
            for client in clients:
                client.close()
            for path in (pool_dir / "registry").glob("*.json"):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(Path(data["project_path"]).is_relative_to(home))
                pool.kill_tree(data["pid"])
            reaper_path = pool_dir / "reaper.json"
            if reaper_path.exists():
                pool.kill_tree(json.loads(reaper_path.read_text(encoding="utf-8"))["pid"])
            # Windows' pythonw launcher can briefly retain the log after its child exits.
            def cleanup():
                try:
                    shutil.rmtree(home)
                    return True
                except PermissionError as exc:
                    if exc.winerror != 32:
                        raise
                    return False
            if completed:
                wait_for(cleanup, 5)
            else:
                print(f"Failed test logs retained at {home}", flush=True)


if __name__ == "__main__":
    unittest.main()
