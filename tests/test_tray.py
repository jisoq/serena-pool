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
        self.assertIn("일부 상태 확인 불가", tray.summary(snapshot))

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
            ("IsWindowEnabled", w.BOOL, [w.HWND]),
            ("GetWindowLongW", w.LONG, [w.HWND, ctypes.c_int]),
            ("GetWindowRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)]),
            ("MapWindowPoints", ctypes.c_int, [w.HWND, w.HWND, ctypes.POINTER(w.POINT), w.UINT]),
        ):
            function = getattr(tray.user32, name)
            function.restype, function.argtypes = result, arguments

    def contents(self, menu):
        result = []
        for position in range(tray.user32.GetMenuItemCount(menu)):
            text = ctypes.create_unicode_buffer(4096)
            tray.user32.GetMenuStringW(menu, position, text, len(text), 0x400)
            self.assertFalse(tray.user32.GetSubMenu(menu, position))
            result.append((text.value, tray.user32.GetMenuItemID(menu, position)))
        return result

    def snapshot(self):
        return pool.PoolSnapshot(time.time(), (
            pool.ServerStatus("first", r"c:\a&b\same", "codex", None, 24500, 111, "running"),
            pool.ServerStatus("second", r"c:\other\same", "zcode", "single-project",
                              24501, 222, "port_unavailable"),
        ), (
            pool.ProxyStatus("first", r"c:\a&b\same", 333, "connected"),
            pool.ProxyStatus("first", r"c:\a&b\same", 334, "stale"),
            pool.ProxyStatus("first", r"c:\other\same", 335, "connected"),
            pool.ProxyStatus("missing", r"c:\pending", 444, "connected"),
        ), "running", ("unreadable test record",))

    def observer(self, snapshot=None):
        observer = tray.Tray()
        observer.snapshot = snapshot
        observer._create_status_window()
        self.addCleanup(observer._destroy_status_window)
        self.assertFalse(tray.user32.IsWindowVisible(observer.status_hwnd))
        return observer

    def text(self, handle):
        text = ctypes.create_unicode_buffer(tray.user32.GetWindowTextLengthW(handle) + 1)
        tray.user32.GetWindowTextW(handle, text, len(text))
        return text.value

    def cell(self, observer, row, column):
        text = ctypes.create_unicode_buffer(4096)
        item = tray.LVITEM(iSubItem=column, pszText=ctypes.cast(text, w.LPWSTR),
                           cchTextMax=len(text))
        tray.user32.SendMessageW(observer.status_controls["servers"], tray.LVM_FIRST + 115,
                                row, ctypes.addressof(item))
        return text.value

    def select(self, observer, row):
        item = tray.LVITEM(state=3, stateMask=3)
        tray.user32.SendMessageW(observer.status_controls["servers"], tray.LVM_FIRST + 43,
                                row, ctypes.addressof(item))

    def rect(self, observer, key):
        rect = w.RECT()
        self.assertTrue(tray.user32.GetWindowRect(observer.status_controls[key], ctypes.byref(rect)))
        tray.user32.MapWindowPoints(None, observer.status_hwnd,
            ctypes.cast(ctypes.byref(rect), ctypes.POINTER(w.POINT)), 2)
        return rect

    def test_tray_menu_has_only_three_commands_and_releases_native_menu(self):
        observer = tray.Tray()
        captured = {}

        def select(menu, *args):
            captured["menu"] = menu
            self.assertEqual(self.contents(menu), [
                ("상태 보기", tray.SHOW_STATUS), ("로그 폴더 열기", tray.OPEN_LOGS),
                ("트레이 종료", tray.EXIT)])
            return tray.SHOW_STATUS

        with (patch.object(tray.user32, "TrackPopupMenu", side_effect=select),
              patch.object(observer, "show_status") as shown):
            observer.show_menu()
        shown.assert_called_once_with()
        self.assertFalse(tray.user32.IsMenu(captured["menu"]))

    def test_native_rows_details_and_diagnostics_keep_exact_associations(self):
        snapshot = self.snapshot()
        observer = self.observer(snapshot)
        self.assertEqual(tray.user32.SendMessageW(observer.status_controls["servers"],
                                                 tray.LVM_FIRST + 4, 0, 0), 2)
        self.assertEqual([self.cell(observer, 0, column) for column in range(6)],
                         ["same", "c:\\a&b", "codex", "실행 중", "24500", "1"])
        self.assertEqual(self.cell(observer, 1, 1), "c:\\other")
        self.assertEqual(self.cell(observer, 1, 5), "0")
        self.assertEqual(self.text(observer.status_controls["path"]), snapshot.servers[0].project_path)
        self.assertEqual(self.text(observer.status_controls["proxies"]), "333, 334 (갱신 지연)")
        diagnostic = self.text(observer.status_controls["diagnostics"])
        self.assertIn("연결 대기 2건", diagnostic)
        self.assertIn("PID 335 (연결됨) | c:\\other\\same", diagnostic)
        self.assertIn("PID 444 (연결됨) | c:\\pending", diagnostic)
        self.assertIn("읽기 오류: unreadable test record", diagnostic)
        self.assertEqual(self.text(observer.status_controls["proxy_count"]), "3")
        self.select(observer, 1)
        self.assertEqual(observer.selected_server_key, "second")
        self.assertEqual(self.text(observer.status_controls["server"]), "222")
        self.assertEqual(self.text(observer.status_controls["profile"]), "single-project")
        self.assertEqual(self.text(observer.status_controls["proxies"]), "없음")

    def test_native_selection_and_log_target_follow_displayed_snapshot(self):
        observer = self.observer(self.snapshot())
        self.select(observer, 1)
        observer.snapshot = pool.PoolSnapshot(time.time(), (), (), "missing", ())
        with patch.object(observer, "open_path") as opened:
            observer.status_window_proc(observer.status_hwnd, 0x0111, tray.OPEN_SERVER_LOG,
                                        observer.status_controls["server_log"])
        opened.assert_called_once_with(pool.LOGS_DIR / "serena-second.log")
        previous = observer.displayed_snapshot
        changed = pool.PoolSnapshot(time.time(), tuple(reversed(previous.servers)),
                                    previous.proxies, "running", ())
        focus = tray.user32.GetFocus()
        observer.refresh_status(changed)
        self.assertIs(observer.displayed_snapshot, changed)
        self.assertEqual(observer.selected_server_key, "second")
        self.assertEqual(tray.user32.GetFocus(), focus)
        self.assertEqual(self.text(observer.status_controls["path"]), previous.servers[1].project_path)
        inserted = pool.ServerStatus("new", r"c:\new\aaa", "codex", None, 24502, 999, "running")
        observer.refresh_status(pool.PoolSnapshot(time.time(), (inserted, *previous.servers),
                                                  previous.proxies, "running", ()))
        self.assertEqual(observer.selected_server_key, "second")
        self.assertEqual(self.text(observer.status_controls["path"]), previous.servers[1].project_path)
        observer.refresh_status(pool.PoolSnapshot(time.time(), (previous.servers[0],), (),
                                                  "running", ()))
        self.assertIsNone(observer.selected_server_key)
        self.assertEqual(self.text(observer.status_controls["path"]), "")
        self.assertEqual(self.text(observer.status_controls["proxies"]), "")
        self.assertFalse(tray.user32.IsWindowEnabled(observer.status_controls["server_log"]))
        with patch.object(observer, "open_path") as opened:
            observer.status_window_proc(observer.status_hwnd, 0x0111, tray.OPEN_SERVER_LOG,
                                        observer.status_controls["server_log"])
        opened.assert_not_called()

    def test_long_unicode_paths_stay_in_copyable_details_and_distinct_locations(self):
        paths = [r"c:\long\nested\one\shared\프로젝트&tools",
                 r"c:\long\nested\two\shared\프로젝트&tools"]
        snapshot = pool.PoolSnapshot(time.time(), tuple(
            pool.ServerStatus(str(index), path, "codex", None, 24500 + index, 111 + index,
                              "running") for index, path in enumerate(paths)),
            (), "running", ())
        observer = self.observer(snapshot)
        self.assertEqual(self.cell(observer, 0, 0), "프로젝트&tools")
        self.assertEqual(self.cell(observer, 0, 1), r"one\shared")
        self.assertEqual(self.cell(observer, 1, 1), r"two\shared")
        self.assertEqual(self.text(observer.status_controls["path"]), paths[0])
        self.select(observer, 1)
        self.assertEqual(self.text(observer.status_controls["path"]), paths[1])

    def test_ten_servers_fifty_proxies_fit_in_normal_native_grid(self):
        snapshot = pool.PoolSnapshot(time.time(), tuple(
            pool.ServerStatus(str(index), rf"c:\worktrees\task-{index}\project", "codex",
                              None, 24500 + index, 100 + index, "running")
            for index in range(10)), tuple(
            pool.ProxyStatus(str(index), rf"c:\worktrees\task-{index}\project",
                             1000 + index * 5 + proxy, "connected")
            for index in range(10) for proxy in range(5)), "running", ())
        observer = self.observer(snapshot)
        control = observer.status_controls["servers"]
        self.assertEqual(tray.user32.SendMessageW(control, tray.LVM_FIRST + 4, 0, 0), 10)
        self.assertGreaterEqual(tray.user32.SendMessageW(control, tray.LVM_FIRST + 40, 0, 0), 10)
        self.assertTrue(all(self.cell(observer, row, 5) == "5" for row in range(10)))
        self.assertEqual(self.text(observer.status_controls["server_count"]), "10")
        self.assertEqual(self.text(observer.status_controls["proxy_count"]), "50")
        self.select(observer, 9)
        before = tray.user32.SendMessageW(control, tray.LVM_FIRST + 39, 0, 0)
        observer.refresh_status(snapshot)
        self.assertEqual(observer.selected_server_key, "9")
        self.assertEqual(tray.user32.SendMessageW(control, tray.LVM_FIRST + 39, 0, 0), before)

    def test_checking_and_incomplete_observations_do_not_appear_as_idle(self):
        observer = self.observer()
        self.assertEqual(self.text(observer.status_controls["summary"]), "상태 확인 중")
        self.assertEqual(self.text(observer.status_controls["server_count"]), "확인 중")
        self.assertEqual(self.text(observer.status_controls["proxy_count"]), "확인 중")
        unreadable = pool.PoolSnapshot(time.time(), (), (), "unreadable", ("access denied",))
        observer.refresh_status(unreadable)
        self.assertIn("확인 필요 | 일부 상태 확인 불가",
                      self.text(observer.status_controls["summary"]))
        self.assertIn("access denied", self.text(observer.status_controls["diagnostics"]))
        empty = pool.PoolSnapshot(time.time(), (), (), "missing", ())
        self.assertIn("대기 중", tray.summary(empty))

    def test_diagnostics_disclose_raw_records_and_reset_after_recovery(self):
        observer = self.observer(self.snapshot())
        controls = observer.status_controls
        visible = 0x10000000
        self.assertFalse(observer.diagnostics_expanded)
        self.assertFalse(tray.user32.GetWindowLongW(controls["diagnostics"], -16) & visible)
        self.assertTrue(tray.user32.GetWindowLongW(controls["diagnostic_toggle"], -16) & visible)
        self.assertEqual(self.text(controls["issues"]), "연결 대기 2건 | 읽기 오류 1건")
        observer.status_window_proc(observer.status_hwnd, tray.WM_COMMAND,
                                    tray.TOGGLE_DIAGNOSTICS, controls["diagnostic_toggle"])
        self.assertTrue(observer.diagnostics_expanded)
        self.assertEqual(self.text(controls["diagnostic_toggle"]), "진단 접기")
        self.assertTrue(tray.user32.GetWindowLongW(controls["diagnostics"], -16) & visible)
        observer.refresh_status(pool.PoolSnapshot(time.time(), (), (), "missing", ()))
        self.assertFalse(observer.diagnostics_expanded)
        self.assertFalse(tray.user32.GetWindowLongW(controls["diagnostics"], -16) & visible)
        self.assertFalse(tray.user32.GetWindowLongW(controls["diagnostic_toggle"], -16) & visible)
        self.assertEqual(self.text(controls["diagnostics"]), "")
        self.assertEqual(self.text(controls["empty"]), "등록된 서버가 없습니다.")

    def test_korean_ui_preserves_literal_content_and_copyable_optional_fields(self):
        path = "c:\\원본 & data\\" + "긴 프로젝트 경로\\" * 20 + "프로젝트 & tool"
        context, profile = "Original & 컨텍스트", "profile-&-원본"
        issue = "Raw read issue & 원본: C:\\data\\record.json"
        snapshot = pool.PoolSnapshot(time.time(), (
            pool.ServerStatus("key", path, context, profile, 24500, 123, "pid_unavailable"),
        ), tuple(pool.ProxyStatus("key", path, 1000 + index, "connected")
                 for index in range(19)), "pid_unavailable", (issue,))
        observer = self.observer(snapshot)
        controls = observer.status_controls
        self.assertEqual(self.text(observer.status_hwnd), "Serena Pool 상태")
        self.assertEqual(self.cell(observer, 0, 0), "프로젝트 & tool")
        self.assertEqual(self.cell(observer, 0, 2), context)
        self.assertEqual(self.cell(observer, 0, 3), "PID 확인 불가")
        self.assertEqual(self.text(controls["path"]), path)
        self.assertEqual(self.text(controls["context"]), context)
        self.assertEqual(self.text(controls["profile"]), profile)
        self.assertEqual(self.text(controls["proxies"]).split(", "),
                         [str(1000 + index) for index in range(19)])
        self.assertIn("읽기 오류: " + issue, self.text(controls["diagnostics"]))
        self.assertEqual(self.text(controls["reaper"]), "정리 프로세스 PID 확인 불가")
        for key in ("path", "server", "profile", "proxies"):
            self.assertTrue(tray.user32.IsWindowEnabled(controls[key]))
            self.assertTrue(tray.user32.GetWindowLongW(controls[key], -16) & 0x800)
        self.assertGreaterEqual(self.rect(observer, "proxies").bottom -
                                self.rect(observer, "proxies").top, 100)
        server = snapshot.servers[0]
        observer.refresh_status(pool.PoolSnapshot(time.time(), (
            pool.ServerStatus(server.key, path, context, None, 24500, 123, "running"),
        ), (), "running", ()))
        self.assertFalse(tray.user32.GetWindowLongW(controls["profile"], -16) & 0x10000000)
        self.assertFalse(tray.user32.GetWindowLongW(controls["profile_label"], -16) & 0x10000000)

    def test_normal_and_minimum_layout_keep_visible_controls_reachable(self):
        observer = self.observer(self.snapshot())
        self.select(observer, 1)
        scale = tray.user32.GetDpiForWindow(observer.status_hwnd) / 96
        for width, height in ((1140, 720), (780, 480), (1140, 480), (780, 720),
                              (1115, 602), (1115, 620), (1115, 650)):
            for expanded in (False, True):
                with self.subTest(width=width, height=height, expanded=expanded):
                    observer.diagnostics_expanded = expanded
                    tray.user32.SetWindowPos(observer.status_hwnd, None, 0, 0,
                        round(width * scale), round(height * scale), 0x16)
                    observer._layout_status()
                    client = w.RECT()
                    tray.user32.GetClientRect(observer.status_hwnd, ctypes.byref(client))
                    for key, control in observer.status_controls.items():
                        if not tray.user32.GetWindowLongW(control, -16) & 0x10000000:
                            continue
                        rect = self.rect(observer, key)
                        self.assertGreater(rect.right, rect.left, key)
                        self.assertGreater(rect.bottom, rect.top, key)
                        self.assertGreaterEqual(rect.left, 0, key)
                        self.assertGreaterEqual(rect.top, 0, key)
                        self.assertLessEqual(rect.right, client.right, key)
                        self.assertLessEqual(rect.bottom, client.bottom, key)
                    table = self.rect(observer, "servers")
                    panel = self.rect(observer, "inspector")
                    self.assertGreaterEqual((table.bottom - table.top) / scale, 100)
                    self.assertLessEqual(sum(tray.user32.SendMessageW(
                        observer.status_controls["servers"], tray.LVM_FIRST + 29, column, 0)
                        for column in range(6)), table.right - table.left)
                    if panel.top > table.top:
                        self.assertLess(table.bottom, panel.top)
                        self.assertGreaterEqual((self.rect(observer, "path").bottom -
                                                self.rect(observer, "path").top) / scale, 58)
                        self.assertGreaterEqual((self.rect(observer, "proxies").bottom -
                                                self.rect(observer, "proxies").top) / scale, 78)
                    else:
                        self.assertLess(table.right, panel.left)
                    if expanded:
                        diagnostics = self.rect(observer, "diagnostics")
                        self.assertLessEqual(panel.bottom, diagnostics.top)
                        self.assertLess(diagnostics.bottom, self.rect(observer, "observed").top)

    def test_status_close_and_escape_hide_without_stopping_observer(self):
        observer = self.observer(self.snapshot())
        handle = observer.status_hwnd
        with patch.object(tray.user32, "PostQuitMessage") as quit_message:
            observer.status_window_proc(handle, tray.WM_CLOSE, 0, 0)
            observer.status_window_proc(handle, 0x0111, 2, 0)
        self.assertTrue(tray.user32.IsWindow(handle))
        self.assertFalse(tray.user32.IsWindowVisible(handle))
        self.assertFalse(observer.stop.is_set())
        quit_message.assert_not_called()
        with (patch.object(tray.user32, "ShowWindow") as shown,
              patch.object(tray.user32, "SetWindowPos"),
              patch.object(tray.user32, "SetForegroundWindow"),
              patch.object(tray.user32, "SetFocus")):
            observer.show_status()
        self.assertEqual(observer.status_hwnd, handle)
        self.assertEqual([call.args for call in shown.call_args_list if call.args[0] == handle],
                         [(handle, 5)])

    def test_left_click_opens_status_and_right_click_opens_menu(self):
        observer = tray.Tray()
        with (patch.object(observer, "show_status") as status,
              patch.object(observer, "show_menu") as menu):
            observer.window_proc(None, tray.WM_ICON, 0, 0x0202)
            status.assert_called_once_with()
            menu.assert_not_called()
            observer.window_proc(None, tray.WM_ICON, 0, 0x0205)
            menu.assert_called_once_with()

    def test_missing_log_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.log"
            with (patch.object(tray.user32, "MessageBoxW") as message,
                  patch.object(tray.logging, "exception"), patch.object(tray.os, "startfile") as opened):
                tray.Tray().open_path(path)
            self.assertFalse(path.exists())
            opened.assert_not_called()
            self.assertIn("로그 경로가 없습니다.", message.call_args.args[1])
            self.assertIn(str(path), message.call_args.args[1])
            self.assertEqual(message.call_args.args[2], "로그 열기 실패")

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

    def test_second_launch_requests_existing_native_status(self):
        with (tempfile.TemporaryDirectory() as temp, patch.object(pool, "POOL_DIR", Path(temp)),
              patch.object(tray.user32, "FindWindowW", return_value=1234),
              patch.object(tray.user32, "GetWindowThreadProcessId"),
              patch.object(tray.user32, "AllowSetForegroundWindow"),
              patch.object(tray.user32, "PostMessageW") as posted):
            first = tray.acquire_singleton()
            try:
                self.assertIsNone(tray.acquire_singleton())
                posted.assert_called_once_with(1234, tray.WM_SHOW_STATUS, 0, 0)
            finally:
                tray.kernel32.CloseHandle(first)

    def test_message_loop_routes_status_escape_with_native_msg(self):
        observer = tray.Tray()
        observer.snapshot = self.snapshot()
        handles = []

        def notify(operation, data):
            if operation == 0:
                observer._create_status_window()
                handles.append(observer.status_hwnd)
                self.assertEqual(tray.user32.SendMessageW(observer.status_hwnd, 0x007F, 0, 0),
                                 observer.icons["attention"])
                observer.refresh_status(pool.PoolSnapshot(time.time(), (), (), "missing", ()))
                self.assertEqual(tray.user32.SendMessageW(observer.status_hwnd, 0x007F, 0, 0),
                                 observer.icons["neutral"])
                tray.user32.PostMessageW(observer.status_hwnd, tray.WM_KEYDOWN, 27, 0)
                tray.user32.PostMessageW(observer.hwnd, tray.WM_CLOSE, 0, 0)
            return True

        with (patch.object(observer, "sample", return_value=None),
              patch.object(tray.shell32, "Shell_NotifyIconW", side_effect=notify),
              patch.object(tray.user32, "ShowWindow", wraps=tray.user32.ShowWindow) as shown):
            observer.run()
        self.assertEqual(len(handles), 1)
        shown.assert_any_call(handles[0], 0)
        self.assertFalse(tray.user32.IsWindow(handles[0]))
        self.assertFalse(observer.worker.is_alive())

    def test_hidden_native_window_exits_and_releases_resources(self):
        observer = tray.Tray()
        handles = []

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

    def test_status_resources_are_destroyed_with_the_observer(self):
        observer = self.observer(self.snapshot())
        handles = [observer.status_hwnd, *observer.status_controls.values()]
        observer._destroy_status_window()
        self.assertTrue(all(not tray.user32.IsWindow(handle) for handle in handles))
        self.assertEqual(observer.status_fonts, {})
        self.assertIsNone(observer.row_images)

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
