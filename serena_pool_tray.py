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


class LVITEM(ctypes.Structure):
    _fields_ = [("mask", w.UINT), ("iItem", ctypes.c_int), ("iSubItem", ctypes.c_int),
                ("state", w.UINT), ("stateMask", w.UINT), ("pszText", w.LPWSTR),
                ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int), ("lParam", w.LPARAM),
                ("iIndent", ctypes.c_int), ("iGroupId", ctypes.c_int), ("cColumns", w.UINT),
                ("puColumns", ctypes.POINTER(w.UINT)), ("piColFmt", ctypes.POINTER(ctypes.c_int)),
                ("iGroup", ctypes.c_int)]


class LVCOLUMN(ctypes.Structure):
    _fields_ = [("mask", w.UINT), ("fmt", ctypes.c_int), ("cx", ctypes.c_int),
                ("pszText", w.LPWSTR), ("cchTextMax", ctypes.c_int), ("iSubItem", ctypes.c_int),
                ("iImage", ctypes.c_int), ("iOrder", ctypes.c_int), ("cxMin", ctypes.c_int),
                ("cxDefault", ctypes.c_int), ("cxIdeal", ctypes.c_int)]


class NMHDR(ctypes.Structure):
    _fields_ = [("hwndFrom", w.HWND), ("idFrom", ctypes.c_size_t), ("code", w.UINT)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", w.POINT), ("ptMaxSize", w.POINT), ("ptMaxPosition", w.POINT),
                ("ptMinTrackSize", w.POINT), ("ptMaxTrackSize", w.POINT)]


class INITCOMMONCONTROLS(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("dwICC", w.DWORD)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)
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
    (user32, "FindWindowW", w.HWND, [w.LPCWSTR, w.LPCWSTR]),
    (user32, "GetWindowThreadProcessId", w.DWORD, [w.HWND, ctypes.POINTER(w.DWORD)]),
    (user32, "AllowSetForegroundWindow", w.BOOL, [w.DWORD]),
    (user32, "ShowWindow", w.BOOL, [w.HWND, ctypes.c_int]),
    (user32, "IsIconic", w.BOOL, [w.HWND]),
    (user32, "IsWindowVisible", w.BOOL, [w.HWND]),
    (user32, "IsWindow", w.BOOL, [w.HWND]),
    (user32, "IsChild", w.BOOL, [w.HWND, w.HWND]),
    (user32, "IsDialogMessageW", w.BOOL, [w.HWND, ctypes.POINTER(w.MSG)]),
    (user32, "GetFocus", w.HWND, []),
    (user32, "SetFocus", w.HWND, [w.HWND]),
    (user32, "GetClientRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)]),
    (user32, "MoveWindow", w.BOOL, [w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, w.BOOL]),
    (user32, "SetWindowPos", w.BOOL, [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, w.UINT]),
    (user32, "GetDpiForWindow", w.UINT, [w.HWND]),
    (user32, "GetDpiForSystem", w.UINT, []),
    (user32, "GetSysColor", w.DWORD, [ctypes.c_int]),
    (user32, "GetSysColorBrush", w.HBRUSH, [ctypes.c_int]),
    (user32, "SetThreadDpiAwarenessContext", w.HANDLE, [w.HANDLE]),
    (user32, "LoadCursorW", w.HANDLE, [w.HINSTANCE, ctypes.c_void_p]),
    (user32, "SendMessageW", LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
    (user32, "SetWindowTextW", w.BOOL, [w.HWND, w.LPCWSTR]),
    (user32, "GetWindowTextLengthW", ctypes.c_int, [w.HWND]),
    (user32, "GetWindowTextW", ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
    (user32, "EnableWindow", w.BOOL, [w.HWND, w.BOOL]),
    (user32, "InvalidateRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT), w.BOOL]),
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
    (comctl32, "InitCommonControlsEx", w.BOOL, [ctypes.POINTER(INITCOMMONCONTROLS)]),
    (comctl32, "ImageList_Create", w.HANDLE,
        [ctypes.c_int, ctypes.c_int, w.UINT, ctypes.c_int, ctypes.c_int]),
    (comctl32, "ImageList_Destroy", w.BOOL, [w.HANDLE]),
    (gdi32, "CreateFontW", w.HANDLE, [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
        w.DWORD, w.DWORD, w.LPCWSTR]),
    (gdi32, "DeleteObject", w.BOOL, [w.HANDLE]),
    (gdi32, "SetBkColor", w.DWORD, [w.HDC, w.DWORD]),
    (uxtheme, "SetWindowTheme", LRESULT, [w.HWND, w.LPCWSTR, w.LPCWSTR]),
):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments

WM_ICON = 0x8001
WM_SNAPSHOT = 0x8002
WM_CLOSE = 0x0010
WM_SHOW_STATUS = 0x8003
WM_SIZE = 0x0005
WM_SETREDRAW = 0x000B
WM_GETMINMAXINFO = 0x0024
WM_SETFONT = 0x0030
WM_NOTIFY = 0x004E
WM_KEYDOWN = 0x0100
WM_COMMAND = 0x0111
WM_CTLCOLORSTATIC = 0x0138
WM_DPICHANGED = 0x02E0
EXIT = 1
OPEN_LOGS = 2
SHOW_STATUS = 3
OPEN_SERVER_LOG = 4
LVM_FIRST = 0x1000
LVM_SETIMAGELIST = LVM_FIRST + 3
LVM_DELETEALLITEMS = LVM_FIRST + 9
LVM_GETNEXTITEM = LVM_FIRST + 12
LVM_ENSUREVISIBLE = LVM_FIRST + 19
LVM_SETCOLUMNWIDTH = LVM_FIRST + 30
LVM_SETITEMSTATE = LVM_FIRST + 43
LVM_SETEXTENDEDLISTVIEWSTYLE = LVM_FIRST + 54
LVM_SETITEMW = LVM_FIRST + 76
LVM_INSERTITEMW = LVM_FIRST + 77
LVM_INSERTCOLUMNW = LVM_FIRST + 97
LVM_SETITEMTEXTW = LVM_FIRST + 116


def _window_class_name():
    identity = hashlib.sha256(pool.norm_path(pool.POOL_DIR).encode()).hexdigest()
    return "SerenaPoolTray-" + identity


def acquire_singleton():
    name = _window_class_name()
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Global\\" + name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        existing = user32.FindWindowW(name, None)
        if existing:
            process = w.DWORD()
            user32.GetWindowThreadProcessId(existing, ctypes.byref(process))
            user32.AllowSetForegroundWindow(process.value)
            user32.PostMessageW(existing, WM_SHOW_STATUS, 0, 0)
        return None
    return handle


def summary(snapshot: pool.PoolSnapshot | None) -> str:
    if snapshot is None:
        return "Checking pool"
    state = {"neutral": "Idle", "active": "Active", "attention": "Attention"}[icon_state(snapshot)]
    if snapshot.issues:
        state += " | Incomplete observation"
    return f"{state} | {snapshot.server_count} HTTP servers | {snapshot.proxy_count} connected proxies"


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
        self.displayed_snapshot = None
        self.selected_server_key = None
        self.server_rows = []
        self.stop = threading.Event()
        self.hwnd = None
        self.status_hwnd = None
        self.status_controls = {}
        self.status_font = None
        self.status_heading_font = None
        self.row_images = None
        self.status_dpi = 0
        self._refreshing_status = False
        self._status_class = f"SerenaPoolStatus-{os.getpid()}-{id(self)}"
        self.icons = {}
        self.icon_added = False
        self.worker = None
        self.callback = WNDPROC(self.window_proc)
        self.status_callback = WNDPROC(self.status_window_proc)
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        if not self.taskbar_created:
            raise ctypes.WinError(ctypes.get_last_error())

    def build_menu(self):
        menu = user32.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for label, command in (("Show status", SHOW_STATUS), ("Open logs folder", OPEN_LOGS),
                                   ("Exit tray", EXIT)):
                if not user32.AppendMenuW(menu, 0, command, label):
                    raise ctypes.WinError(ctypes.get_last_error())
            return menu
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
            user32.MessageBoxW(self.status_hwnd or self.hwnd, str(exc), "Serena pool", 0x10)

    def show_menu(self):
        menu = self.build_menu()
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
        elif command == SHOW_STATUS:
            self.show_status()
        elif command == OPEN_LOGS:
            self.open_path(pool.LOGS_DIR)

    def _create_status_window(self):
        instance = kernel32.GetModuleHandleW(None)
        common = INITCOMMONCONTROLS(ctypes.sizeof(INITCOMMONCONTROLS), 1)
        if not comctl32.InitCommonControlsEx(ctypes.byref(common)):
            raise ctypes.WinError(ctypes.get_last_error())
        window_class = WNDCLASS(lpfnWndProc=self.status_callback, hInstance=instance,
            hIcon=self.icons.get("active"), hCursor=user32.LoadCursorW(None, 32512),
            hbrBackground=6, lpszClassName=self._status_class)
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            scale = user32.GetDpiForSystem() / 96
            self.status_hwnd = user32.CreateWindowExW(0x10000, self._status_class,
                "Serena Pool status", 0x00CF0000, -2147483648, -2147483648,
                round(980 * scale), round(620 * scale), None, None, instance, None)
            if not self.status_hwnd:
                raise ctypes.WinError(ctypes.get_last_error())

            def control(key, kind, text="", style=0x50000080, command=0, edge=0):
                handle = user32.CreateWindowExW(edge, kind, text, style, 0, 0, 0, 0,
                    self.status_hwnd, command, instance, None)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.status_controls[key] = handle
                return handle

            control("summary", "STATIC", "Checking pool")
            control("logs", "BUTTON", "Open logs folder", 0x50010000, OPEN_LOGS)
            servers = control("servers", "SysListView32", style=0x5001000D, edge=0x200)
            user32.SendMessageW(servers, LVM_SETEXTENDEDLISTVIEWSTYLE, 0, 0x10020)
            uxtheme.SetWindowTheme(servers, "Explorer", None)
            for column, label in enumerate(("Project", "Location", "Context", "Status",
                                             "Port", "Connected proxies")):
                text = ctypes.create_unicode_buffer(label)
                native_column = LVCOLUMN(mask=7, fmt=1 if column >= 4 else 0, cx=100,
                                         pszText=ctypes.cast(text, w.LPWSTR))
                if user32.SendMessageW(servers, LVM_INSERTCOLUMNW, column,
                                       ctypes.addressof(native_column)) == -1:
                    raise OSError("Windows could not create the server columns")
            control("selected", "STATIC", "Select a server")
            control("server_log", "BUTTON", "Open server log", 0x50010000, OPEN_SERVER_LOG)
            control("path_label", "STATIC", "Project path")
            control("path", "EDIT", style=0x50010880)
            control("server", "STATIC")
            control("proxies_label", "STATIC", "Proxy PIDs")
            control("proxies", "EDIT", style=0x50010844)
            control("diagnostics", "EDIT", style=0x50210844)
            self._layout_status()
            self.refresh_status(self.snapshot)
        except Exception:
            self._destroy_status_window()
            raise

    def _layout_status(self):
        if not self.status_controls:
            return
        dpi = user32.GetDpiForWindow(self.status_hwnd)
        scale = dpi / 96
        if dpi != self.status_dpi:
            font = gdi32.CreateFontW(-round(10 * dpi / 72), 0, 0, 0, 400, 0, 0, 0,
                                     1, 0, 0, 5, 0, "Segoe UI")
            heading = gdi32.CreateFontW(-round(10 * dpi / 72), 0, 0, 0, 600, 0, 0, 0,
                                        1, 0, 0, 5, 0, "Segoe UI")
            if not font or not heading:
                if font:
                    gdi32.DeleteObject(font)
                if heading:
                    gdi32.DeleteObject(heading)
                raise ctypes.WinError(ctypes.get_last_error())
            for key, handle in self.status_controls.items():
                user32.SendMessageW(handle, WM_SETFONT,
                    heading if key in ("summary", "selected") else font, True)
            for old_font in (self.status_font, self.status_heading_font):
                if old_font:
                    gdi32.DeleteObject(old_font)
            self.status_font, self.status_heading_font = font, heading
            images = comctl32.ImageList_Create(1, round(26 * scale), 0x20, 1, 0)
            if not images:
                raise ctypes.WinError(ctypes.get_last_error())
            user32.SendMessageW(self.status_controls["servers"], LVM_SETIMAGELIST, 1, images)
            if self.row_images:
                comctl32.ImageList_Destroy(self.row_images)
            self.row_images = images
            self.status_dpi = dpi
        rect = w.RECT()
        user32.GetClientRect(self.status_hwnd, ctypes.byref(rect))
        width, height = rect.right / scale, rect.bottom / scale
        grid_height = max(100, height - 266)
        detail_y = 60 + grid_height + 12
        positions = {
            "summary": (18, 18, width - 190, 24),
            "logs": (width - 160, 12, 142, 30),
            "servers": (18, 60, width - 36, grid_height),
            "selected": (18, detail_y + 3, width - 196, 24),
            "server_log": (width - 160, detail_y, 142, 29),
            "path_label": (18, detail_y + 39, 84, 23),
            "path": (106, detail_y + 34, width - 124, 27),
            "server": (106, detail_y + 69, width - 124, 22),
            "proxies_label": (18, detail_y + 98, 84, 22),
            "proxies": (106, detail_y + 94, width - 124, 40),
            "diagnostics": (18, height - 44, width - 36, 36),
        }
        for key, (x, y, cw, ch) in positions.items():
            user32.MoveWindow(self.status_controls[key], round(x * scale), round(y * scale),
                               round(cw * scale), round(ch * scale), True)
        available = width - 36 - 20
        for column, column_width in enumerate((150, max(100, available - 570), 100, 120, 65, 135)):
            user32.SendMessageW(self.status_controls["servers"], LVM_SETCOLUMNWIDTH,
                                column, round(column_width * scale))

    def show_status(self):
        if not self.status_hwnd:
            self._create_status_window()
        else:
            self.refresh_status(self.snapshot)
        was_visible = user32.IsWindowVisible(self.status_hwnd)
        user32.ShowWindow(self.status_hwnd, 9 if user32.IsIconic(self.status_hwnd) else 5)
        user32.SetWindowPos(self.status_hwnd, None, 0, 0, 0, 0, 0x47)
        user32.SetForegroundWindow(self.status_hwnd)
        if not was_visible:
            user32.SetFocus(self.status_controls["servers"])

    def _set_status_text(self, key, text):
        handle = self.status_controls[key]
        current = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(handle) + 1)
        user32.GetWindowTextW(handle, current, len(current))
        if current.value != text:
            user32.SetWindowTextW(handle, text)

    def refresh_status(self, snapshot):
        if not self.status_hwnd:
            return
        servers = self.status_controls["servers"]
        previous = self.displayed_snapshot
        old_keys = [previous.servers[index].key for index in self.server_rows] if previous else []
        order = sorted(range(len(snapshot.servers)), key=lambda index: (
            Path(snapshot.servers[index].project_path).name.casefold(),
            snapshot.servers[index].project_path.casefold(), snapshot.servers[index].context,
            snapshot.servers[index].port, snapshot.servers[index].key)) if snapshot else []
        keys = [snapshot.servers[index].key for index in order]
        locations = {}
        if snapshot:
            projects = {server.project_path for server in snapshot.servers}
            for project in projects:
                path = Path(project)
                parents = [Path(other).parent for other in projects
                           if other != project and Path(other).name.casefold() == path.name.casefold()]
                parts = path.parent.parts
                length = min(2, len(parts)) if len(parts) <= 2 else 1
                while length < len(parts) and any(
                    tuple(part.casefold() for part in other.parts[-length:]) ==
                    tuple(part.casefold() for part in parts[-length:]) for other in parents):
                    length += 1
                locations[project] = str(Path(*parts[-length:]))
        self._refreshing_status = True
        user32.SendMessageW(servers, WM_SETREDRAW, False, 0)
        try:
            if keys != old_keys:
                user32.SendMessageW(servers, LVM_DELETEALLITEMS, 0, 0)
            for row, index in enumerate(order):
                server = snapshot.servers[index]
                count = sum(proxy.state == "connected" and proxy.server_key == server.key
                            and proxy.project_path == server.project_path for proxy in snapshot.proxies)
                values = (Path(server.project_path).name, locations[server.project_path],
                          server.context, {"running": "Running", "pid_unavailable": "PID unavailable",
                                           "port_unavailable": "Port unavailable"}[server.state],
                          str(server.port), str(count))
                for column, value in enumerate(values):
                    text = ctypes.create_unicode_buffer(value)
                    item = LVITEM(mask=5, iItem=row, iSubItem=column,
                                  pszText=ctypes.cast(text, w.LPWSTR), lParam=index)
                    if column == 0:
                        message = LVM_INSERTITEMW if keys != old_keys else LVM_SETITEMW
                        result = user32.SendMessageW(servers, message, 0, ctypes.addressof(item))
                        if result == (-1 if message == LVM_INSERTITEMW else 0):
                            raise OSError("Windows could not render a server row")
                    else:
                        user32.SendMessageW(servers, LVM_SETITEMTEXTW, row, ctypes.addressof(item))
            if previous is None and keys and self.selected_server_key is None:
                self.selected_server_key = keys[0]
            if self.selected_server_key not in keys:
                self.selected_server_key = None
            self.server_rows = order
            self.displayed_snapshot = snapshot
            if keys != old_keys and self.selected_server_key is not None:
                selected = LVITEM(state=3, stateMask=3)
                user32.SendMessageW(servers, LVM_SETITEMSTATE, keys.index(self.selected_server_key),
                                    ctypes.addressof(selected))
                user32.SendMessageW(servers, LVM_ENSUREVISIBLE, keys.index(self.selected_server_key), False)
        finally:
            user32.SendMessageW(servers, WM_SETREDRAW, True, 0)
            user32.InvalidateRect(servers, None, False)
            self._refreshing_status = False
        self._set_status_text("summary", summary(snapshot))
        diagnostics = ["Waiting for the first observation."]
        if snapshot:
            age = max(0, int(time.time() - snapshot.observed_at))
            diagnostics = [f"Observed {age}s ago | Reaper {snapshot.reaper.replace('_', ' ')}"]
            associated = {(server.key, server.project_path) for server in snapshot.servers}
            pending = [proxy for proxy in snapshot.proxies
                       if (proxy.server_key, proxy.project_path) not in associated]
            if pending:
                diagnostics[0] += f" | {len(pending)} pending connections"
                diagnostics.append("Pending connections")
                diagnostics.extend(f"PID {proxy.pid} ({proxy.state}) | {proxy.project_path}"
                                   for proxy in pending)
            if snapshot.issues:
                diagnostics[0] += f" | {len(snapshot.issues)} read issues"
            diagnostics.extend("Read issue: " + issue for issue in snapshot.issues)
        self._set_status_text("diagnostics", "\r\n".join(diagnostics))
        self._refresh_selection()

    def _refresh_selection(self):
        snapshot = self.displayed_snapshot
        server = next((server for server in snapshot.servers
                       if server.key == self.selected_server_key), None) if snapshot else None
        self._set_status_text("selected",
            f"{Path(server.project_path).name} / {server.context}" if server else "Select a server")
        self._set_status_text("path", server.project_path if server else "")
        self._set_status_text("server", (f"Server PID {server.pid}" +
            (f" | Profile {server.profile}" if server.profile else "")) if server else "")
        proxies = [proxy for proxy in snapshot.proxies if proxy.server_key == server.key
                   and proxy.project_path == server.project_path] if server else []
        self._set_status_text("proxies", ", ".join(
            str(proxy.pid) + (" (stale)" if proxy.state == "stale" else "") for proxy in proxies)
            if proxies else ("None" if server else ""))
        user32.EnableWindow(self.status_controls["server_log"], server is not None)

    def status_window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_CTLCOLORSTATIC:
                gdi32.SetBkColor(wparam, user32.GetSysColor(5))
                return user32.GetSysColorBrush(5)
            if message == WM_CLOSE:
                user32.ShowWindow(hwnd, 0)
                return 0
            if message == WM_SIZE:
                self._layout_status()
                return 0
            if message == WM_GETMINMAXINFO:
                info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                scale = (user32.GetDpiForWindow(hwnd) or user32.GetDpiForSystem()) / 96
                info.ptMinTrackSize = w.POINT(round(780 * scale), round(480 * scale))
                return 0
            if message == WM_DPICHANGED:
                rect = ctypes.cast(lparam, ctypes.POINTER(w.RECT)).contents
                user32.SetWindowPos(hwnd, None, rect.left, rect.top,
                    rect.right - rect.left, rect.bottom - rect.top, 0x14)
                self._layout_status()
                return 0
            if message == WM_NOTIFY and self.status_controls and not self._refreshing_status:
                header = ctypes.cast(lparam, ctypes.POINTER(NMHDR)).contents
                if (header.hwndFrom == self.status_controls["servers"]
                        and header.code == ctypes.c_uint(-101).value):
                    row = user32.SendMessageW(header.hwndFrom, LVM_GETNEXTITEM, -1, 2)
                    self.selected_server_key = (self.displayed_snapshot.servers[self.server_rows[row]].key
                        if 0 <= row < len(self.server_rows) else None)
                    self._refresh_selection()
                    return 0
            if message == WM_COMMAND:
                command = wparam & 0xFFFF
                if command == 2 and not lparam:
                    user32.ShowWindow(hwnd, 0)
                elif command == OPEN_LOGS:
                    self.open_path(pool.LOGS_DIR)
                elif command == OPEN_SERVER_LOG and self.displayed_snapshot:
                    server = next((server for server in self.displayed_snapshot.servers
                                   if server.key == self.selected_server_key), None)
                    if server:
                        self.open_path(pool.LOGS_DIR / f"serena-{server.key}.log")
                return 0
        except Exception:
            logging.exception("Status window operation failed")
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _destroy_status_window(self):
        if self.status_hwnd:
            user32.DestroyWindow(self.status_hwnd)
            self.status_hwnd = None
        self.status_controls.clear()
        self.displayed_snapshot = None
        self.selected_server_key = None
        self.server_rows.clear()
        self.status_dpi = 0
        for font in (self.status_font, self.status_heading_font):
            if font:
                gdi32.DeleteObject(font)
        self.status_font = self.status_heading_font = None
        if self.row_images:
            comctl32.ImageList_Destroy(self.row_images)
            self.row_images = None
        user32.UnregisterClassW(self._status_class, kernel32.GetModuleHandleW(None))

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
            if message == WM_SHOW_STATUS or message == WM_ICON and lparam == 0x0202:
                self.show_status()
                return 0
            if message == WM_ICON and lparam in (0x0205, 0x007B):
                self.show_menu()
                return 0
            if message == WM_SNAPSHOT:
                self.update_icon()
                if self.status_hwnd and user32.IsWindowVisible(self.status_hwnd):
                    self.refresh_status(self.snapshot)
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
        name = _window_class_name()
        dpi_context = user32.SetThreadDpiAwarenessContext(-4)
        window_class = WNDCLASS(lpfnWndProc=self.callback, hInstance=instance, lpszClassName=name)
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.hwnd = user32.CreateWindowExW(0, name, "Serena pool observer", 0, 0, 0, 0, 0,
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
                if self.status_hwnd and (message.hWnd == self.status_hwnd
                        or user32.IsChild(self.status_hwnd, message.hWnd)):
                    if message.message == WM_KEYDOWN and message.wParam == 27:
                        user32.ShowWindow(self.status_hwnd, 0)
                        continue
                    if user32.IsDialogMessageW(self.status_hwnd, ctypes.byref(message)):
                        continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self.stop.set()
            if self.worker:
                self.worker.join(timeout=1)
            self._destroy_status_window()
            if self.hwnd:
                data = NOTIFYICONDATA(cbSize=ctypes.sizeof(NOTIFYICONDATA), hWnd=self.hwnd, uID=1)
                shell32.Shell_NotifyIconW(2, ctypes.byref(data))
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            for icon in self.icons.values():
                user32.DestroyIcon(icon)
            self.icons.clear()
            user32.UnregisterClassW(name, instance)
            if dpi_context:
                user32.SetThreadDpiAwarenessContext(dpi_context)


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
