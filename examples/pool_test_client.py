#!/usr/bin/env python3
"""Тестовый MCP-клиент для serena_pool_proxy: initialize -> tools/list -> tools/call.

Usage: pool_test_client.py <project_dir> [tool_name]
Запускать интерпретатором с установленным httpx (тем же, что и прокси).
Печатает JSON-строки результата в stdout; код выхода 0 при успехе.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

PROXY = str(Path(__file__).resolve().parent.parent / "serena_pool_proxy.py")


class McpStdio:
    def __init__(self, cwd):
        self.proc = subprocess.Popen(
            [sys.executable, PROXY], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.stderr_lines = []
        threading.Thread(target=self._drain, daemon=True).start()
        self._id = 0

    def _drain(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=180):
        self._id += 1
        self.send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("proxy closed stdout; stderr:\n" + "\n".join(self.stderr_lines[-30:]))
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg
        raise TimeoutError(method)

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def main():
    project_dir = sys.argv[1]
    tool = sys.argv[2] if len(sys.argv) > 2 else "get_current_config"
    c = McpStdio(project_dir)
    try:
        init = c.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pool-test", "version": "0.1"},
        })
        server = init.get("result", {}).get("serverInfo", {})
        print(json.dumps({"step": "initialize", "server": server}))
        c.notify("notifications/initialized")
        tools = c.request("tools/list")
        names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
        print(json.dumps({"step": "tools/list", "count": len(names), "sample": names[:5]}))
        call = c.request("tools/call", {"name": tool, "arguments": {}})
        content = call.get("result", {}).get("content", [])
        text = content[0]["text"][:400] if content and "text" in content[0] else str(call)[:400]
        print(json.dumps({"step": "tools/call", "tool": tool, "isError": call.get("result", {}).get("isError"), "text": text}))
    finally:
        c.close()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
