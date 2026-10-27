# FILE: watchdog.py
# PURPOSE: Per-call watchdog for CFS15 COM work - timeout, hung-engine restart,
#          partial results, and "what was running" logging.
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# WHY A CHILD PROCESS
# -------------------
# RSG.CFS15.Calculation is an IN-PROCESS .NET COM server (InprocServer32 =
# mscoree.dll). A hung COM call therefore hangs a thread *inside* whatever
# process made it, and Python cannot kill a thread. In production, design_wall
# and even single check_stud calls hung for > 4 minutes until the app was
# restarted. The only real recovery is process isolation:
#
#   * every CFS job runs in a fresh child process (spawn) with its own STA and
#     its own licensed Calculation object;
#   * the job streams progress ("evaluating section X, combo Y") and partial
#     results (each finished section / combo / member) back over a pipe;
#   * if the job exceeds its timeout, the child is TERMINATED (which is the
#     engine restart - the next call gets a brand-new process and COM object),
#     and the tool returns the partial results plus an honest error naming the
#     section/combo that was being evaluated.
#
# Set CFS_MCP_WATCHDOG=0 to run jobs inline (no isolation, no timeout) for
# debugging. CFS_MCP_TIMEOUT_S sets the default timeout (60 s).

from __future__ import annotations

import contextlib
import logging
import multiprocessing as mp
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("cfs_rsg_mcp.watchdog")

DEFAULT_TIMEOUT_S = 60.0

# Set inside the child process (or inline mode) to a callable(msg_tuple).
_sink: Optional[Callable[[tuple], None]] = None


def default_timeout() -> float:
    try:
        return float(os.environ.get("CFS_MCP_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def isolation_enabled() -> bool:
    return os.environ.get("CFS_MCP_WATCHDOG", "1").strip().lower() not in ("0", "off", "false", "no")


# ---------------------------------------------------------------------------
# Called by job code (no-ops when nobody is listening)
# ---------------------------------------------------------------------------
def report(**activity) -> None:
    """Record what the job is doing right now (e.g. section=..., combo=...)."""
    if _sink is not None:
        try:
            _sink(("progress", activity))
        except Exception:  # noqa: BLE001  (a broken pipe must not kill the job)
            pass


def partial(item: Any) -> None:
    """Hand back one finished unit of work (returned if the job later times out)."""
    if _sink is not None:
        try:
            _sink(("partial", item))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------
class JobResult:
    def __init__(self):
        self.ok: bool = False
        self.result: Any = None
        self.error: Optional[str] = None
        self.error_type: Optional[str] = None
        self.timed_out: bool = False
        self.engine_restarted: bool = False
        self.partial: List[Any] = []
        self.last_activity: Optional[Dict] = None
        self.elapsed_s: float = 0.0
        self.isolated: bool = False

    def diagnostics(self) -> Dict:
        return {"timed_out": self.timed_out, "engine_restarted": self.engine_restarted,
                "elapsed_s": round(self.elapsed_s, 2), "isolated_process": self.isolated,
                "last_activity": self.last_activity,
                "partial_count": len(self.partial)}


# ---------------------------------------------------------------------------
# Child side
# ---------------------------------------------------------------------------
def _child_main(conn, func, kwargs):
    # stdout is the MCP JSON-RPC channel of the PARENT - never write to it.
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    sys.stdout = sys.stderr
    global _sink
    _sink = conn.send
    try:
        res = func(**kwargs)
        conn.send(("done", res))
    except BaseException as e:  # noqa: BLE001
        try:
            conn.send(("error", f"{e}", type(e).__name__, traceback.format_exc()))
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _nul_stdin():
    """Point the process STD_INPUT handle at NUL while spawning a child.

    Under an MCP stdio server (e.g. Claude Desktop) the transport keeps a
    blocking ReadFile pending on the stdin pipe. A spawn child inherits that
    STD_INPUT handle, and its interpreter start-up probes stdin -> it blocks
    behind the parent's pending read and never reaches _child_main (every
    isolated tool then hangs to timeout with last_activity=None). The MCP
    reader uses the CRT fd 0 handle, so swapping the Win32 std handle for the
    duration of start() does not affect it.
    """
    if os.name != "nt":
        yield
        return
    import ctypes
    import msvcrt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = ctypes.c_void_p
    k32.GetStdHandle.argtypes = [ctypes.c_uint32]
    k32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    STD_INPUT = 0xFFFFFFF6  # STD_INPUT_HANDLE (-10)
    fd = os.open(os.devnull, os.O_RDONLY)
    old = k32.GetStdHandle(STD_INPUT)
    k32.SetStdHandle(STD_INPUT, msvcrt.get_osfhandle(fd))
    try:
        yield
    finally:
        k32.SetStdHandle(STD_INPUT, old)
        os.close(fd)


def _describe(activity: Optional[Dict]) -> str:
    if not activity:
        return "before any section was reached (engine start-up / license acquisition)"
    return ", ".join(f"{k}={v!r}" for k, v in activity.items())


def run(func: Callable, kwargs: Optional[Dict] = None,
        timeout_s: Optional[float] = None, label: str = "") -> JobResult:
    """Run `func(**kwargs)` under the watchdog. Never raises for job failures.

    func must be a module-level (picklable) function; kwargs plain data.
    """
    kwargs = dict(kwargs or {})
    timeout = float(timeout_s) if timeout_s else default_timeout()
    jr = JobResult()
    t0 = time.monotonic()
    name = label or getattr(func, "__name__", "cfs job")

    if not isolation_enabled():
        return _run_inline(func, kwargs, jr, t0)

    jr.isolated = True
    ctx = mp.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(send_conn, func, kwargs),
                       name=f"cfs-{name}", daemon=True)
    try:
        with _nul_stdin():
            proc.start()
    except Exception as e:  # noqa: BLE001
        jr.error = f"Could not start the CFS worker process: {e}"
        jr.error_type = type(e).__name__
        jr.elapsed_s = time.monotonic() - t0
        return jr
    send_conn.close()
    deadline = t0 + timeout
    finished = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = recv_conn.poll(min(remaining, 0.25))
            except (EOFError, OSError):
                ready = False
                if not proc.is_alive():
                    break
            if not ready:
                if not proc.is_alive() and not recv_conn.poll(0):
                    break
                continue
            try:
                msg = recv_conn.recv()
            except (EOFError, OSError):
                break
            kind = msg[0]
            if kind == "progress":
                jr.last_activity = msg[1]
            elif kind == "partial":
                jr.partial.append(msg[1])
            elif kind == "done":
                jr.ok, jr.result, finished = True, msg[1], True
                break
            elif kind == "error":
                jr.error, jr.error_type, finished = msg[1], msg[2], True
                log.error("CFS job %s failed: %s\n%s", name, msg[1], msg[3])
                break
    finally:
        jr.elapsed_s = time.monotonic() - t0
        if not finished:
            if time.monotonic() >= deadline and proc.is_alive():
                jr.timed_out = True
                jr.error = (f"CFS did not respond within {timeout:g} s while evaluating "
                            f"{_describe(jr.last_activity)}. The hung CFS engine process "
                            f"was terminated; the next call starts a fresh engine. "
                            f"{len(jr.partial)} unit(s) of work completed before the "
                            f"hang are returned as partial results.")
                log.error("CFS job %s TIMED OUT after %.1f s; last activity: %s",
                          name, jr.elapsed_s, _describe(jr.last_activity))
            else:
                jr.error = (f"The CFS worker process exited unexpectedly (exit code "
                            f"{proc.exitcode}) while evaluating "
                            f"{_describe(jr.last_activity)}. This usually means the "
                            f"CFS engine crashed; {len(jr.partial)} partial result(s) "
                            f"are returned.")
                log.error("CFS job %s: worker died (exit %s); last activity: %s",
                          name, proc.exitcode, _describe(jr.last_activity))
        if finished:
            proc.join(5)          # let a finished worker exit on its own
        if proc.is_alive():
            if not finished:
                jr.engine_restarted = True
            proc.terminate()
        proc.join(5)
        if proc.is_alive():  # pragma: no cover - terminate() is TerminateProcess on Windows
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            proc.join(2)
        try:
            recv_conn.close()
        except Exception:  # noqa: BLE001
            pass
    return jr


def _run_inline(func, kwargs, jr: JobResult, t0: float) -> JobResult:
    global _sink

    def sink(msg):
        if msg[0] == "progress":
            jr.last_activity = msg[1]
        elif msg[0] == "partial":
            jr.partial.append(msg[1])

    prev, _sink = _sink, sink
    try:
        jr.result, jr.ok = func(**kwargs), True
    except Exception as e:  # noqa: BLE001
        jr.error, jr.error_type = str(e), type(e).__name__
    finally:
        _sink = prev
        jr.elapsed_s = time.monotonic() - t0
    return jr


# ---------------------------------------------------------------------------
# Self-test jobs (used by the unit tests to simulate a COM hang / crash)
# ---------------------------------------------------------------------------
def selftest_hang_job(n_before_hang: int = 2, hang_s: float = 3600.0, **_ignored) -> Dict:
    """Emit n partial results, then hang like a stuck COM call.

    Accepts (and ignores) any other kwargs so a test can substitute it for a real
    tool job and exercise the tool's full timeout path.
    """
    for i in range(n_before_hang):
        report(section=f"SECTION-{i}", combo="2a")
        partial({"section": f"SECTION-{i}", "UC": 0.5})
    report(section="HUNG-SECTION", combo="4a")
    time.sleep(hang_s)
    return {"never": "reached"}


def selftest_ok_job(x: int = 1) -> Dict:
    report(section="S", combo="1a")
    partial({"i": x})
    return {"value": x * 2}


def selftest_error_job() -> Dict:
    report(section="BAD")
    raise RuntimeError("simulated CFS failure")
