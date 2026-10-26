import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

if os.name != "nt":
    raise unittest.SkipTest("serena-pool currently supports Windows only")

import serena_pool_proxy as pool


class PoolRegressionTests(unittest.TestCase):
    def test_exited_process_with_retained_handle_is_not_alive(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
        process.wait(timeout=10)
        handle = pool._kernel32.OpenProcess(0x1000, False, process.pid)
        self.assertTrue(handle, "The Popen object retains the exited Windows process handle")
        pool._kernel32.CloseHandle(handle)
        self.assertFalse(pool.pid_alive(process.pid))
        self.assertTrue(pool.pid_alive(os.getpid()))

    def test_dead_leases_are_removed_even_when_server_key_still_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            dead = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
            dead.wait(timeout=10)
            for label, pid in [("dead", dead.pid), ("live", os.getpid())]:
                (directory / f"project__{label}.json").write_text(json.dumps({"key": "project", "proxy_pid": pid}), encoding="utf-8")
            with patch.object(pool, "LEASES_DIR", directory):
                pool._clean_orphan_leases({"project"}, 0)
            self.assertFalse((directory / "project__dead.json").exists())
            self.assertTrue((directory / "project__live.json").exists())

    def test_isolated_identity_does_not_reuse_legacy_port_key(self):
        cfg = {**pool.DEFAULT_CONFIG, "port_range": 3}
        with patch.dict(os.environ, {"SERENA_POOL_CONTEXT": "codex"}):
            candidates = list(pool.port_candidates(Path.cwd(), cfg))
        legacy = hashlib.md5(f"{pool.norm_path(Path.cwd())}::codex".encode()).hexdigest()[:8]
        self.assertNotEqual(candidates[0][0], legacy)
        self.assertEqual(len({port for _, port in candidates}), 3)
        self.assertTrue(all(24500 <= port < 24503 for _, port in candidates))

    def test_context_copy_preserves_user_options_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "custom.yml"
            source.write_text("name: custom\nprompt: Keep this prompt\nexcluded_tools: [write_memory]\nsingle_project: false\n", encoding="utf-8")
            before = source.read_bytes()
            with patch.object(pool, "CONTEXTS_DIR", directory), patch.dict(os.environ, {"SERENA_POOL_CONTEXT": ""}):
                target = pool.pinned_context_path({"context": str(source)}, "test")
            copied = pool.yaml.safe_load(target.read_text(encoding="utf-8"))
            self.assertTrue(copied["single_project"])
            self.assertEqual(copied["prompt"], "Keep this prompt")
            self.assertEqual(copied["excluded_tools"], ["write_memory"])
            self.assertEqual(source.read_bytes(), before)

    def test_registered_names_and_relative_paths_resolve_without_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            project = directory / "project"
            (project / ".serena").mkdir(parents=True)
            (project / ".serena/project.yml").write_text("project_name: registered-name\n", encoding="utf-8")
            home = directory / "serena-home"
            home.mkdir()
            (home / "serena_config.yml").write_text(pool.yaml.safe_dump({"projects": [str(project)]}), encoding="utf-8")
            with patch.dict(os.environ, {"SERENA_HOME": str(home)}):
                self.assertEqual(pool.resolve_project("registered-name", directory), project.resolve())
                self.assertEqual(pool.resolve_project(".", project), project.resolve())
                with self.assertRaises(ValueError):
                    pool.resolve_project("missing", directory)

    @unittest.skipUnless(os.environ.get("SERENA_TEST_EXE"), "Set SERENA_TEST_EXE to verify native context export")
    def test_builtin_context_export_is_headless(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cfg = {**pool.DEFAULT_CONFIG, "serena_exe": os.environ["SERENA_TEST_EXE"]}
            with (
                patch.object(pool, "POOL_DIR", directory),
                patch.object(pool, "CONTEXTS_DIR", directory),
                patch.dict(os.environ, {"SERENA_POOL_CONTEXT": "", "SERENA_HOME": str(directory)}),
                patch.object(pool.subprocess, "run", wraps=subprocess.run) as run,
            ):
                target = pool.pinned_context_path(cfg, "builtin")
            self.assertTrue(pool.yaml.safe_load(target.read_text(encoding="utf-8"))["single_project"])
            editor = Path(run.call_args.kwargs["env"]["EDITOR"])
            self.assertEqual(editor.name.lower(), "where.exe")
            self.assertTrue(editor.is_file())
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class BridgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_interface_does_not_require_a_repl_session(self):
        bridge = pool.Bridge(Path.cwd(), pool.DEFAULT_CONFIG)
        bridge.session = Mock(post_request=AsyncMock(return_value={"result": {"content": [
            {"type": "text", "text": "Serena tools instructions without a REPL session"}]}}))
        response = await bridge._initial_instructions(1)
        self.assertIn("content", response["result"])
        self.assertEqual(bridge.session_aliases, {})

    async def test_retry_remaps_session_after_backend_restart(self):
        bridge = pool.Bridge(Path.cwd(), pool.DEFAULT_CONFIG)
        bridge.session_aliases["client-session"] = "old-native-session"
        bridge.session = Mock(post_request=AsyncMock(side_effect=pool.httpx.ConnectError("server exited")))
        replacement = Mock(post_request=AsyncMock(side_effect=[
            {"result": {"content": [{"type": "text", "text": "Your Serena session id is `new-native-session`"}]}},
            {"result": {"content": []}},
        ]))

        async def reconnect():
            bridge.session = replacement
            bridge.session_aliases.clear()

        bridge.reconnect = reconnect
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "serena_repl", "arguments": {"session": "client-session", "code": "1 + 1"}}}
        await bridge._forward(request)
        forwarded = replacement.post_request.call_args.args[0]
        self.assertEqual(forwarded["params"]["arguments"]["session"], "new-native-session")
        self.assertEqual(request["params"]["arguments"]["session"], "client-session")

    async def test_cancellation_does_not_wait_for_running_request(self):
        bridge = pool.Bridge(Path.cwd(), pool.DEFAULT_CONFIG)
        bridge.session = Mock(post_no_reply=AsyncMock())
        notification = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        async with bridge.request_lock:
            await asyncio.wait_for(bridge.handle_notification(notification), timeout=1)
        bridge.session.post_no_reply.assert_awaited_once_with(notification)


if __name__ == "__main__":
    unittest.main()
