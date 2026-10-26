import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

if os.name != "nt":
    raise unittest.SkipTest("The tray observer requires Windows")

import serena_pool_proxy as pool
import serena_pool_tray as tray


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "pool"
        for name, value in {
            "POOL_DIR": self.root, "REGISTRY_DIR": self.root / "registry",
            "LEASES_DIR": self.root / "leases", "LOCKS_DIR": self.root / "locks",
            "LOGS_DIR": self.root / "logs", "CONTEXTS_DIR": self.root / "contexts",
            "CONFIG_PATH": self.root / "config.json", "REAPER_PATH": self.root / "reaper.json",
        }.items():
            patcher = patch.object(pool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def server(self, key, port, pid=None, project=None):
        return self.write(pool.REGISTRY_DIR / f"{key}.json", {
            "key": key, "project_path": str(project or self.root / "project"),
            "context": "codex", "port": port, "pid": os.getpid() if pid is None else pid,
        })

    def lease(self, filename, key, pid=None, started=1, age=0):
        path = self.write(pool.LEASES_DIR / f"{filename}.json", {
            "key": key, "project_path": str(self.root / "project"),
            "proxy_pid": os.getpid() if pid is None else pid, "started_at": started,
        })
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_import_and_empty_read_do_not_initialize_pool(self):
        snapshot = pool.read_pool_status()
        self.assertEqual((snapshot.server_count, snapshot.proxy_count), (0, 0))
        self.assertEqual(snapshot.reaper, "missing")
        self.assertFalse(snapshot.issues)
        self.assertFalse(self.root.exists())
        result = subprocess.run([sys.executable, "-c",
            "from pathlib import Path; from unittest.mock import patch; "
            f"target=Path({str(self.root)!r}); "
            "p=patch.object(Path, 'home', return_value=target); p.start(); "
            "import serena_pool_proxy; import serena_pool_tray; "
            "serena_pool_proxy.read_pool_status(); assert not target.exists()"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_process_and_port_states_and_no_source_mutation(self):
        with socket.socket() as listener, socket.socket() as unavailable:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            unavailable.bind(("127.0.0.1", 0))
            dead = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(dead.wait, timeout=10)
            dead.wait(timeout=10)
            self.server("running", listener.getsockname()[1])
            self.server("port", unavailable.getsockname()[1])
            self.server("pid", listener.getsockname()[1], dead.pid)
            self.lease("live", "running")
            self.lease("dead", "running", dead.pid)
            self.write(pool.REAPER_PATH, {"pid": os.getpid()})
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
            snapshot = pool.read_pool_status()
            after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(snapshot.server_count, 1)
        self.assertEqual(snapshot.proxy_count, 1)
        self.assertEqual([proxy.pid for proxy in snapshot.proxies], [os.getpid()])
        self.assertEqual({s.key: s.state for s in snapshot.servers}, {
            "running": "running", "port": "port_unavailable", "pid": "pid_unavailable"})
        self.assertTrue(all(s.profile is None for s in snapshot.servers))
        self.assertEqual(snapshot.reaper, "running")
        self.assertFalse(snapshot.issues)

    def test_latest_lease_selects_one_association_even_when_stale(self):
        self.lease("old", "old", started=1)
        self.lease("new", "new", started=2, age=91)
        snapshot = pool.read_pool_status()
        self.assertEqual(len(snapshot.proxies), 1)
        self.assertEqual(snapshot.proxies[0].server_key, "new")
        self.assertEqual(snapshot.proxy_count, 0)
        self.lease("tie-z", "winner", started=2)
        snapshot = pool.read_pool_status()
        self.assertEqual(snapshot.proxies[0].server_key, "winner")
        self.assertEqual(snapshot.proxy_count, 1)

    def test_heartbeat_boundary(self):
        path = self.lease("live", "pending")
        for age, expected in ((90, 1), (90.01, 0)):
            os.utime(path, (1000 - age, 1000 - age))
            with patch.object(pool.time, "time", return_value=1000):
                self.assertEqual(pool.read_pool_status().proxy_count, expected)

    def test_invalid_records_keep_valid_observations(self):
        self.lease("valid", "pending")
        self.lease("invalid-time", "bad", started="later")
        pool.REGISTRY_DIR.mkdir()
        (pool.REGISTRY_DIR / "partial.json").write_text('{"key":', encoding="utf-8")
        self.write(pool.REGISTRY_DIR / "bad-shape.json", [])
        self.write(pool.REAPER_PATH, {"pid": True})
        snapshot = pool.read_pool_status()
        self.assertEqual(snapshot.proxy_count, 1)
        self.assertEqual(snapshot.proxies[0].server_key, "pending")
        self.assertEqual(snapshot.reaper, "unreadable")
        self.assertEqual(len(snapshot.issues), 4)
        self.assertIn("Incomplete observation", tray.summary(snapshot))

    def test_unreadable_directory_is_incomplete(self):
        original = Path.iterdir

        def read(directory):
            if directory == pool.REGISTRY_DIR:
                raise PermissionError("registry access denied")
            return original(directory)

        with patch.object(Path, "iterdir", read):
            snapshot = pool.read_pool_status()
        self.assertIn("registry access denied", snapshot.issues[0])
        self.assertFalse(self.root.exists())

    def test_backend_direct_entry_paths_still_initialize_their_state(self):
        cfg = pool.load_config()
        self.assertEqual(cfg, pool.DEFAULT_CONFIG)
        self.assertTrue(pool.CONFIG_PATH.is_file())
        self.assertTrue(all(path.is_dir() for path in (
            pool.REGISTRY_DIR, pool.LEASES_DIR, pool.LOCKS_DIR, pool.LOGS_DIR, pool.CONTEXTS_DIR)))
        lease = pool.write_lease("direct", self.root)
        self.assertEqual(json.loads(lease.read_text())["proxy_pid"], os.getpid())


class NativeMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name, result, arguments in (
            ("GetMenuItemCount", ctypes.c_int, [w.HMENU]),
            ("GetSubMenu", w.HMENU, [w.HMENU, ctypes.c_int]),
            ("GetMenuItemID", w.UINT, [w.HMENU, ctypes.c_int]),
            ("GetMenuStringW", ctypes.c_int, [w.HMENU, w.UINT, w.LPWSTR, ctypes.c_int, w.UINT]),
            ("IsMenu", w.BOOL, [w.HMENU]),
        ):
            function = getattr(tray.user32, name)
            function.restype, function.argtypes = result, arguments

    def contents(self, menu):
        result = []
        for position in range(tray.user32.GetMenuItemCount(menu)):
            text = ctypes.create_unicode_buffer(4096)
            tray.user32.GetMenuStringW(menu, position, text, len(text), 0x400)
            result.append((text.value, tray.user32.GetMenuItemID(menu, position)))
            child = tray.user32.GetSubMenu(menu, position)
            if child:
                result.extend(self.contents(child))
        return result

    def snapshot(self):
        return pool.PoolSnapshot(time.time(), (
            pool.ServerStatus("first", r"c:\a&b\same", "codex", None, 24500, 111, "running"),
            pool.ServerStatus("second", r"c:\other\same", "zcode", "single-project", 24501, 222, "port_unavailable"),
        ), (
            pool.ProxyStatus("first", r"c:\a&b\same", 333, "connected"),
            pool.ProxyStatus("missing", r"c:\pending", 444, "connected"),
        ), "running", ())

    def test_native_menu_content_and_log_targets_survive_snapshot_refresh(self):
        observer = tray.Tray()
        observer.snapshot = self.snapshot()
        captured = {}

        def select(menu, *args):
            captured["menu"] = menu
            rows = self.contents(menu)
            labels = [label for label, _ in rows]
            self.assertIn(r"c:\a&&b\same (1 connected proxies)", labels)
            self.assertIn(r"c:\other\same (0 connected proxies)", labels)
            self.assertIn("codex / port 24500", labels)
            self.assertIn("Status: port unavailable", labels)
            self.assertIn("Server PID: 111", labels)
            self.assertIn("Proxy PID 333: connected", labels)
            self.assertIn("Pending connections", labels)
            self.assertIn(r"c:\pending / proxy PID 444: connected", labels)
            observer.snapshot = pool.PoolSnapshot(time.time(), (), (), "missing", ())
            return next(command for label, command in rows if label == "Open server log")

        with (patch.object(tray.user32, "TrackPopupMenu", side_effect=select),
              patch.object(observer, "open_path") as opened):
            observer.show_menu()
        opened.assert_called_once_with(pool.LOGS_DIR / "serena-first.log")
        self.assertFalse(tray.user32.IsMenu(captured["menu"]))

    def test_unique_project_name_is_compact_and_full_path_remains_available(self):
        project = r"c:\long\nested\프로젝트&tools"
        snapshot = pool.PoolSnapshot(time.time(), (
            pool.ServerStatus("unique", project, "codex", None, 24500, 111, "running"),
        ), (), "running", ())
        menu, _ = tray.Tray().build_menu(snapshot)
        try:
            labels = [label for label, _ in self.contents(menu)]
            self.assertIn("프로젝트&&tools (0 connected proxies)", labels)
            self.assertIn(project.replace("&", "&&"), labels)
        finally:
            tray.user32.DestroyMenu(menu)

    def test_status_text_distinguishes_idle_and_attention_with_zero_counts(self):
        empty = pool.PoolSnapshot(time.time(), (), (), "missing", ())
        unavailable = pool.PoolSnapshot(time.time(), (
            pool.ServerStatus("unavailable", r"c:\project", "codex", None, 24500, 111, "pid_unavailable"),
        ), (), "missing", ())
        self.assertIn("Idle", tray.summary(empty))
        self.assertIn("Attention", tray.summary(unavailable))
        self.assertEqual((empty.server_count, unavailable.server_count), (0, 0))

    def test_missing_log_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.log"
            with (patch.object(tray.user32, "MessageBoxW") as message,
                  patch.object(tray.logging, "exception"), patch.object(tray.os, "startfile") as opened):
                tray.Tray().open_path(path)
            self.assertFalse(path.exists())
            opened.assert_not_called()
            self.assertIn("does not exist", message.call_args.args[1])

    def test_singleton_is_per_pool_and_released_with_handle(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pool, "POOL_DIR", Path(temp)):
            first = tray.acquire_singleton()
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(tray.acquire_singleton())
                with patch.object(pool, "POOL_DIR", Path(temp) / "other"):
                    other = tray.acquire_singleton()
                    self.assertIsNotNone(other)
                    tray.kernel32.CloseHandle(other)
            finally:
                tray.kernel32.CloseHandle(first)
            replacement = tray.acquire_singleton()
            self.assertIsNotNone(replacement)
            tray.kernel32.CloseHandle(replacement)

    def test_hidden_native_window_exits_and_releases_resources(self):
        observer = tray.Tray()
        handles = []
        tray.user32.IsWindow.argtypes = [w.HWND]
        tray.user32.IsWindow.restype = w.BOOL

        def close_window():
            handles.append(observer.hwnd)
            tray.user32.PostMessageW(observer.hwnd, tray.WM_CLOSE, 0, 0)

        with (patch.object(observer, "sample", side_effect=close_window),
              patch.object(tray.shell32, "Shell_NotifyIconW", return_value=True)):
            observer.run()
        self.assertEqual(len(handles), 1)
        self.assertFalse(tray.user32.IsWindow(handles[0]))
        self.assertFalse(observer.worker.is_alive())
        self.assertTrue(observer.stop.is_set())

    def test_exit_and_explorer_restart_do_not_control_backend(self):
        observer = tray.Tray()
        with (patch.object(observer, "update_icon") as update,
              patch.object(tray.user32, "PostQuitMessage") as quit_message,
              patch.object(pool, "kill_tree") as kill,
              patch.object(pool, "ensure_reaper") as start):
            observer.icon_added = True
            observer.window_proc(None, observer.taskbar_created, 0, 0)
            self.assertFalse(observer.icon_added)
            update.assert_called_once()
            observer.window_proc(None, tray.WM_CLOSE, 0, 0)
            self.assertTrue(observer.stop.is_set())
            quit_message.assert_called_once_with(0)
            kill.assert_not_called()
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
