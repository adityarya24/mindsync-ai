"""Cross-platform process helpers for agent dispatch."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WIN = sys.platform == "win32"


def resolve_bin(bin_name: str) -> str | None:
    """Resolve an executable on PATH. Windows prefers .exe > .cmd > .bat."""
    if os.path.isabs(bin_name):
        return bin_name if os.path.isfile(bin_name) else None
    if not IS_WIN:
        return shutil.which(bin_name)

    # On Windows, shutil.which may return .cmd before .exe depending on PATHEXT.
    # Mirror Node agent-dispatch: prefer .exe, then .cmd, then .bat.
    hits: list[str] = []
    path_env = os.environ.get("PATH", "")
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    name = bin_name
    has_ext = bool(os.path.splitext(name)[1])
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        base = Path(directory)
        if has_ext:
            candidate = base / name
            if candidate.is_file():
                hits.append(str(candidate))
        else:
            for ext in pathext:
                candidate = base / f"{name}{ext}"
                if candidate.is_file():
                    hits.append(str(candidate))
    if not hits:
        found = shutil.which(bin_name)
        return found
    lower = [h.lower() for h in hits]

    def by_ext(ext: str) -> str | None:
        for h, low in zip(hits, lower):
            if low.endswith(ext):
                return h
        return None

    return by_ext(".exe") or by_ext(".cmd") or by_ext(".bat") or hits[0]


def spawn_spec(resolved_bin: str, args: list[str]) -> dict[str, Any]:
    """Route Windows .cmd/.bat shims through cmd.exe with an args array."""
    if IS_WIN and re.search(r"\.(cmd|bat)$", resolved_bin, re.I):
        comspec = os.environ.get("ComSpec") or "cmd.exe"
        return {"bin": comspec, "args": ["/d", "/s", "/c", resolved_bin, *args]}
    return {"bin": resolved_bin, "args": list(args)}


def is_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            out = (r.stdout or "").strip()
            if not out or "No tasks" in out or "INFO:" in out:
                return False
            return f'","{pid}",' in out or f'"{pid}"' in out
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_name(pid: int) -> str | None:
    if not pid or pid <= 0:
        return None
    if IS_WIN:
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for line in (r.stdout or "").splitlines():
                if f'","{pid}",' in line:
                    # "image.exe","1234",...
                    return line[1 : line.index('","')].lower()
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return None
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode != 0:
            return None
        name = (r.stdout or "").strip()
        return Path(name).name.lower() if name else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def kill_tree(pid: int) -> bool:
    if not pid or pid <= 0:
        return True
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return not is_alive(pid)
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return not is_alive(pid)


def names_match(reported_name: str | None, spawned_name: str | None) -> bool:
    """Match process image names with Linux comm truncation + Node 26 renames."""
    if reported_name is None or spawned_name is None:
        return False
    if reported_name == spawned_name:
        return True
    if reported_name.startswith(f"{spawned_name}-"):
        return True
    return len(reported_name) == 15 and spawned_name.startswith(reported_name)


def spawn_background(
    resolved_bin: str,
    args: list[str],
    *,
    cwd: str | None = None,
    stdout_path: str | Path,
    stderr_path: str | Path,
    stdin_path: str | Path | None = None,
) -> dict[str, Any]:
    spec = spawn_spec(resolved_bin, args)
    stdout_path = Path(stdout_path)
    stderr_path = Path(stderr_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    out_f = open(stdout_path, "a", encoding="utf-8", errors="replace")
    err_f = (
        out_f
        if Path(stdout_path).resolve() == Path(stderr_path).resolve()
        else open(stderr_path, "a", encoding="utf-8", errors="replace")
    )
    in_f = open(stdin_path, "r", encoding="utf-8", errors="replace") if stdin_path else subprocess.DEVNULL

    creationflags = 0
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdin": in_f,
        "stdout": out_f,
        "stderr": err_f,
        "close_fds": not IS_WIN,
    }
    if IS_WIN:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        creationflags = 0x00000008 | 0x00000200
        kwargs["creationflags"] = creationflags
        kwargs["close_fds"] = False
    else:
        kwargs["start_new_session"] = True

    try:
        child = subprocess.Popen([spec["bin"], *spec["args"]], **kwargs)
    finally:
        if in_f is not subprocess.DEVNULL:
            try:
                in_f.close()
            except OSError:
                pass
        try:
            out_f.close()
        except OSError:
            pass
        if err_f is not out_f:
            try:
                err_f.close()
            except OSError:
                pass

    return {
        "pid": child.pid,
        "spawnedName": Path(spec["bin"]).name.lower(),
    }


async def spawn_foreground(
    resolved_bin: str,
    args: list[str],
    *,
    cwd: str | None = None,
    timeout_ms: int = 600_000,
    input_text: str | None = None,
) -> dict[str, Any]:
    spec = spawn_spec(resolved_bin, args)
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_exec(
            spec["bin"],
            *spec["args"],
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # POSIX: without its own process group the kill_tree below has nothing to
            # aim at, so a timed-out agent's children outlive it. spawn_background
            # already does this; the foreground path was missing it.
            start_new_session=not IS_WIN,
        )
    except OSError as exc:
        return {
            "exitCode": -1,
            "stdout": "",
            "stderr": str(exc),
            "timedOut": False,
        }

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_text.encode("utf-8") if input_text is not None else None),
            timeout=max(timeout_ms, 1) / 1000.0,
        )
    except asyncio.TimeoutError:
        timed_out = True
        if proc.pid:
            kill_tree(proc.pid)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            stdout_b, stderr_b = b"", b""

    code = proc.returncode if proc.returncode is not None else -1
    return {
        "exitCode": code,
        "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
        "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
        "timedOut": timed_out,
    }
