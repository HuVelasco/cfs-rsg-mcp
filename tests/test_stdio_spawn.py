"""Regression: watchdog child spawn must not deadlock under an MCP stdio server.

Claude Desktop runs the server with stdin/stdout as pipes. The stdio transport
keeps a blocking read pending on stdin; a multiprocessing 'spawn' child that
inherits that STD_INPUT handle blocked at start-up and every isolated tool hung
to timeout (last_activity = None). watchdog._nul_stdin() fixes this.

Live test: requires Windows + a licensed CFS 15. Skipped otherwise.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.live

_CFS_DIR = r"C:\Program Files (x86)\RSG Software\CFS 15"


def _skip_reason():
    if os.name != "nt":
        return "Windows-only (stdio-pipe deadlock is a Win32 handle issue)"
    if not os.path.isdir(_CFS_DIR):
        return "CFS 15 not installed"
    return None


def _send(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _reader(stream, q):
    for line in iter(stream.readline, b""):
        q.put(line)


def _wait_for_id(q, msg_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = q.get(timeout=remaining)
        except queue.Empty:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == msg_id:
            return msg


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_connect_cfs_over_stdio_with_watchdog():
    env = dict(os.environ)
    env.pop("CFS_MCP_WATCHDOG", None)  # watchdog ON (default)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfs_rsg_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    q: "queue.Queue[bytes]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    # Drain stderr so a chatty server can't block on a full pipe.
    threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "stdio-spawn-test", "version": "0"}}})
        assert _wait_for_id(q, 1, 30) is not None, "server did not answer initialize"
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        t0 = time.monotonic()
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "connect_cfs", "arguments": {"timeout_s": 40}}})
        resp = _wait_for_id(q, 2, 45)
        elapsed = time.monotonic() - t0
        assert resp is not None, "no response to connect_cfs"
        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        if not payload.get("success") and "licen" in text.lower() \
                and "did not respond" not in text:
            pytest.skip(f"CFS not licensed: {text[:200]}")
        assert payload.get("success") is True, text
        assert elapsed < 10, f"connect_cfs took {elapsed:.1f} s over stdio (deadlock?)"
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        proc.kill()
        proc.wait(10)
