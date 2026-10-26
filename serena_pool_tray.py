from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import logging
import os
from pathlib import Path
import threading
import time

import serena_pool_proxy as pool


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", w.BYTE * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                ("uFlags", w.UINT), ("uCallbackMessage", w.UINT), ("hIcon", w.HICON),
                ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
                ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
for dll, name, result, arguments in (
    (kernel32, "CreateMutexW", w.HANDLE, [ctypes.c_void_p, w.BOOL, w.LPCWSTR]),
    (kernel32, "CloseHandle", w.BOOL, [w.HANDLE]),
    (kernel32, "GetModuleHandleW", w.HMODULE, [w.LPCWSTR]),
    (user32, "RegisterClassW", w.ATOM, [ctypes.POINTER(WNDCLASS)]),
    (user32, "UnregisterClassW", w.BOOL, [w.LPCWSTR, w.HINSTANCE]),
    (user32, "CreateWindowExW", w.HWND, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
        w.HINSTANCE, ctypes.c_void_p]),
    (user32, "DestroyWindow", w.BOOL, [w.HWND]),
    (user32, "DefWindowProcW", LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
    (user32, "RegisterWindowMessageW", w.UINT, [w.LPCWSTR]),
    (user32, "GetMessageW", w.BOOL, [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]),
    (user32, "TranslateMessage", w.BOOL, [ctypes.POINTER(w.MSG)]),
    (user32, "DispatchMessageW", LRESULT, [ctypes.POINTER(w.MSG)]),
    (user32, "PostMessageW", w.BOOL, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
    (user32, "PostQuitMessage", None, [ctypes.c_int]),
    (user32, "CreatePopupMenu", w.HMENU, []),
    (user32, "AppendMenuW", w.BOOL, [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]),
    (user32, "DestroyMenu", w.BOOL, [w.HMENU]),
    (user32, "GetCursorPos", w.BOOL, [ctypes.POINTER(w.POINT)]),
    (user32, "SetForegroundWindow", w.BOOL, [w.HWND]),
    (user32, "TrackPopupMenu", w.BOOL, [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, w.HWND, ctypes.POINTER(w.RECT)]),
    (user32, "CreateIcon", w.HICON, [w.HINSTANCE, ctypes.c_int, ctypes.c_int,
        w.BYTE, w.BYTE, ctypes.POINTER(w.BYTE), ctypes.POINTER(w.BYTE)]),
    (user32, "DestroyIcon", w.BOOL, [w.HICON]),
    (user32, "MessageBoxW", ctypes.c_int, [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]),
    (shell32, "Shell_NotifyIconW", w.BOOL, [w.DWORD, ctypes.POINTER(NOTIFYICONDATA)]),
):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments

WM_ICON = 0x8001
WM_SNAPSHOT = 0x8002
WM_CLOSE = 0x0010
EXIT = 1
OPEN_LOGS = 2


def acquire_singleton():
    identity = hashlib.sha256(pool.norm_path(pool.POOL_DIR).encode()).hexdigest()
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Global\\SerenaPoolTray-" + identity)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return None
    return handle


def summary(snapshot: pool.PoolSnapshot | None) -> str:
    if snapshot is None:
        return "Checking pool"
    prefix = "Incomplete observation" if snapshot.issues else "Observed"
    state = {"neutral": "Idle", "active": "Active", "attention": "Attention"}[icon_state(snapshot)]
    return f"{state} | {prefix}: {snapshot.server_count} HTTP servers, {snapshot.proxy_count} connected proxies"


def icon_state(snapshot: pool.PoolSnapshot | None) -> str:
    if snapshot is None:
        return "neutral"
    if (snapshot.issues or any(s.state != "running" for s in snapshot.servers)
            or any(p.state == "stale" for p in snapshot.proxies)
            or (snapshot.servers or snapshot.proxy_count) and snapshot.reaper != "running"):
        return "attention"
    return "active" if snapshot.server_count or snapshot.proxy_count else "neutral"


class Tray:
    def __init__(self):
        self.snapshot = None
        self.stop = threading.Event()
        self.hwnd = None
        self.icons = {}
        self.icon_added = False
        self.worker = None
        self.callback = WNDPROC(self.window_proc)
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        if not self.taskbar_created:
            raise ctypes.WinError(ctypes.get_last_error())

    def build_menu(self, snapshot):
        menu = user32.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        actions = {OPEN_LOGS: pool.LOGS_DIR}

        def append(parent, label, command=0, submenu=None):
            flags = 0x10 if submenu else (0 if command else 0x02)
            if not user32.AppendMenuW(parent, flags, submenu or command, label.replace("&", "&&")):
                if submenu:
                    user32.DestroyMenu(submenu)
                raise ctypes.WinError(ctypes.get_last_error())

        def child(parent, label):
            handle = user32.CreatePopupMenu()
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            append(parent, label, submenu=handle)
            return handle

        try:
            append(menu, summary(snapshot))
            if snapshot is not None:
                age = max(0, int(time.time() - snapshot.observed_at))
                append(menu, f"Observation age: {age}s")
                append(menu, "Reaper: " + snapshot.reaper.replace("_", " "))
                for issue in snapshot.issues:
                    append(menu, "Read issue: " + issue)
                projects = sorted({server.project_path for server in snapshot.servers})
                project_names = [Path(project).name for project in projects]
                associated = set()
                for project in projects:
                    servers = [s for s in snapshot.servers if s.project_path == project]
                    proxies = [p for p in snapshot.proxies
                               if p.project_path == project and any(s.key == p.server_key for s in servers)]
                    associated.update(p.pid for p in proxies)
                    count = sum(p.state == "connected" for p in proxies)
                    name = Path(project).name
                    label = project if project_names.count(name) > 1 else name
                    project_menu = child(menu, f"{label} ({count} connected proxies)")
                    append(project_menu, project)
                    for server in sorted(servers, key=lambda s: (s.context, s.port, s.key)):
                        server_menu = child(project_menu, f"{server.context} / port {server.port}")
                        append(server_menu, "Status: " + server.state.replace("_", " "))
                        append(server_menu, f"Server PID: {server.pid}")
                        if server.profile:
                            append(server_menu, "Profile: " + server.profile)
                        for proxy in proxies:
                            if proxy.server_key == server.key:
                                append(server_menu, f"Proxy PID {proxy.pid}: {proxy.state}")
                        command = len(actions) + 2
                        actions[command] = pool.LOGS_DIR / f"serena-{server.key}.log"
                        append(server_menu, "Open server log", command)
                pending = [p for p in snapshot.proxies if p.pid not in associated]
                if pending:
                    pending_menu = child(menu, "Pending connections")
                    for proxy in pending:
                        append(pending_menu, f"{proxy.project_path} / proxy PID {proxy.pid}: {proxy.state}")
            append(menu, "Open logs folder", OPEN_LOGS)
            append(menu, "Exit tray", EXIT)
            return menu, actions
        except Exception:
            user32.DestroyMenu(menu)
            raise

    def open_path(self, path: Path):
        try:
            if not path.exists():
                raise FileNotFoundError(f"Log path does not exist: {path}")
            os.startfile(str(path))
        except OSError as exc:
            logging.exception("Cannot open %s", path)
            user32.MessageBoxW(self.hwnd, str(exc), "Serena pool", 0x10)

    def show_menu(self):
        menu, actions = self.build_menu(self.snapshot)
        try:
            point = w.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
            user32.SetForegroundWindow(self.hwnd)
            command = user32.TrackPopupMenu(menu, 0x0102, point.x, point.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, 0, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if command == EXIT:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        elif command in actions:
            self.open_path(actions[command])

    def update_icon(self):
        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self.hwnd, 1
        data.uFlags, data.uCallbackMessage = 7, WM_ICON
        data.hIcon = self.icons[icon_state(self.snapshot)]
        data.szTip = ("Serena pool | " + summary(self.snapshot))[:127]
        if not shell32.Shell_NotifyIconW(1 if self.icon_added else 0, ctypes.byref(data)):
            raise OSError("Windows could not update the Serena pool tray icon")
        self.icon_added = True

    def sample(self):
        while not self.stop.is_set():
            try:
                snapshot = pool.read_pool_status()
            except Exception as exc:
                logging.exception("Pool observation failed")
                snapshot = pool.PoolSnapshot(time.time(), (), (), "unreadable", (str(exc),))
            if self.stop.is_set():
                return
            self.snapshot = snapshot
            user32.PostMessageW(self.hwnd, WM_SNAPSHOT, 0, 0)
            if self.stop.wait(5):
                return

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_ICON and lparam in (0x0202, 0x0205, 0x007B):
                self.show_menu()
                return 0
            if message == WM_SNAPSHOT:
                self.update_icon()
                return 0
            if message == self.taskbar_created:
                self.icon_added = False
                self.update_icon()
                return 0
            if message == WM_CLOSE:
                self.stop.set()
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            logging.exception("Tray operation failed")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        instance = kernel32.GetModuleHandleW(None)
        name = f"SerenaPoolTray-{os.getpid()}-{id(self)}"
        window_class = WNDCLASS(lpfnWndProc=self.callback, hInstance=instance, lpszClassName=name)
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.hwnd = user32.CreateWindowExW(0, name, "Serena pool", 0, 0, 0, 0, 0,
                                             None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            for state, color in (("neutral", (128, 128, 128)), ("active", (45, 180, 90)),
                                 ("attention", (240, 170, 30))):
                mask = bytearray(32)
                pixels = bytearray()
                for y in range(16):
                    for x in range(16):
                        visible = (x - 7.5) ** 2 + (y - 7.5) ** 2 <= 49
                        if not visible:
                            mask[y * 2 + x // 8] |= 0x80 >> (x % 8)
                        rgb = color if visible else (0, 0, 0)
                        pixels.extend((*reversed(rgb), 255 if visible else 0))
                icon = user32.CreateIcon(instance, 16, 16, 1, 32,
                    (w.BYTE * len(mask)).from_buffer_copy(mask),
                    (w.BYTE * len(pixels)).from_buffer_copy(pixels))
                if not icon:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.icons[state] = icon
            self.update_icon()
            self.worker = threading.Thread(target=self.sample, name="pool-tray-sampler", daemon=True)
            self.worker.start()
            message = w.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self.stop.set()
            if self.worker:
                self.worker.join(timeout=1)
            if self.hwnd:
                data = NOTIFYICONDATA(cbSize=ctypes.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1)
                shell32.Shell_NotifyIconW(2, ctypes.byref(data))
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            for icon in self.icons.values():
                user32.DestroyIcon(icon)
            self.icons.clear()
            user32.UnregisterClassW(name, instance)


def main() -> int:
    handle = None
    try:
        handle = acquire_singleton()
        if handle is None:
            return 0
        pool.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=pool.LOGS_DIR / "tray.log", encoding="utf-8",
                            level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
        Tray().run()
        return 0
    except Exception as exc:
        logging.exception("Tray failed")
        user32.MessageBoxW(None, str(exc), "Serena pool tray", 0x10)
        return 1
    finally:
        if handle:
            kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
