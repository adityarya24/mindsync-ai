"""Mechanical review gate for agent dispatch jobs."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mindsync.dispatch.proc import IS_WIN, kill_tree


_OUTPUT_TAIL_CHARS = 2000


class CheckResult(BaseModel):
    name: str
    passed: bool
    exitCode: int | None
    output: str  # tail only
    durationMs: int
    # A check can end without an exit code for two different reasons; the reader
    # needs to know which, because a timeout and a command that could not start
    # call for different fixes.
    timedOut: bool = False


def _tail(text: str) -> str:
    """Keep the end of a check's output — the start of a failure is rarely the reason."""
    return text[-_OUTPUT_TAIL_CHARS:] if len(text) > _OUTPUT_TAIL_CHARS else text


def run_checks(
    cwd: str, checks: list[str], timeout_ms: int = 600_000
) -> list[CheckResult]:
    """Run check commands in cwd through the platform shell, keeping stdout+stderr tail.

    A check that times out or fails to start is recorded as failed with no exit code.
    """
    results: list[CheckResult] = []
    timeout_sec = timeout_ms / 1000.0

    for cmd in checks:
        start = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                # POSIX: make the shell its own process group leader, so killing it
                # on timeout reaches the command it spawned. Without this there is no
                # group for os.killpg to aim at and only the shell itself dies — the
                # same reason spawn_background already does this.
                start_new_session=not IS_WIN,
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    name=cmd,
                    passed=False,
                    exitCode=None,
                    output=_tail(f"Check could not be started: {exc}"),
                    durationMs=int((time.perf_counter() - start) * 1000),
                )
            )
            continue

        timed_out = False
        try:
            output = proc.communicate(timeout=timeout_sec)[0] or ""
            exit_code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            # Killing the shell is not enough: on Windows the real command survives
            # as a grandchild, keeps holding file handles, and then blocks worktree
            # cleanup on a directory the check itself is sitting in.
            timed_out = True
            exit_code = None
            try:
                kill_tree(proc.pid)
            except Exception:
                pass
            try:
                output = proc.communicate(timeout=10)[0] or ""
            except Exception:
                output = ""
        except Exception as exc:
            exit_code = None
            output = f"Check failed to run: {exc}"

        results.append(
            CheckResult(
                name=cmd,
                passed=exit_code == 0,
                exitCode=exit_code,
                output=_tail(output),
                durationMs=int((time.perf_counter() - start) * 1000),
                timedOut=timed_out,
            )
        )
    return results


def diff_summary(cwd: str, base_commit: str | None) -> dict[str, Any]:
    """Return git diff metrics against base_commit plus uncommitted files and commits.

    Returns zeros and empty file list on error, non-git dir, or missing base_commit.
    """
    empty: dict[str, Any] = {
        "filesChanged": 0,
        "insertions": 0,
        "deletions": 0,
        "files": [],
        "commits": 0,
    }
    if not cwd or not base_commit or not Path(cwd).is_dir():
        return empty

    try:
        rev_check = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if rev_check.returncode != 0 or rev_check.stdout.strip() != "true":
            return empty

        commits = 0
        rev_list = subprocess.run(
            ["git", "-C", cwd, "rev-list", "--count", f"{base_commit}..HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if rev_list.returncode == 0 and rev_list.stdout.strip().isdigit():
            commits = int(rev_list.stdout.strip())

        numstat = subprocess.run(
            ["git", "-C", cwd, "diff", "--numstat", base_commit],
            capture_output=True,
            text=True,
            check=False,
        )
        files_set: list[str] = []
        insertions = 0
        deletions = 0

        if numstat.returncode == 0:
            for line in numstat.stdout.splitlines():
                parts = line.strip().split("\t", 2)
                if len(parts) == 3:
                    ins_s, del_s, filename = parts
                    if filename not in files_set:
                        files_set.append(filename)
                    if ins_s != "-":
                        insertions += int(ins_s)
                    if del_s != "-":
                        deletions += int(del_s)

        st = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if len(line) >= 4:
                    code = line[:2]
                    filename = line[3:].strip()
                    if code.startswith("??") or code.startswith("A"):
                        if filename not in files_set:
                            files_set.append(filename)
                            file_path = Path(cwd) / filename
                            if file_path.is_file():
                                try:
                                    lines_count = sum(1 for _ in file_path.open("rb"))
                                    insertions += lines_count
                                except OSError:
                                    pass

        return {
            "filesChanged": len(files_set),
            "insertions": insertions,
            "deletions": deletions,
            "files": files_set,
            "commits": commits,
        }
    except Exception:
        return empty


def check_reasons(meta: dict[str, Any]) -> list[str]:
    """Why a job's mechanical checks do not count as passing. Empty means they do.

    Kept separate from :func:`verdict` because publishing needs exactly this
    question and nothing else about the job, and two copies of it would drift —
    the first one already did, and passed a job whose checks never reported.
    """
    reasons: list[str] = []
    check_results = meta.get("checkResults") or []

    for check in check_results:
        if isinstance(check, CheckResult):
            c_name = check.name
            c_passed = check.passed
            c_exit = check.exitCode
            c_timed_out = check.timedOut
        else:
            c_name = check.get("name", "unknown")
            c_passed = check.get("passed", False)
            c_exit = check.get("exitCode")
            c_timed_out = check.get("timedOut", False)

        if not c_passed:
            if c_timed_out:
                reasons.append(f'check "{c_name}" timed out')
            elif c_exit is None:
                reasons.append(f'check "{c_name}" could not be run')
            else:
                reasons.append(f'check "{c_name}" failed (exit {c_exit})')

    # Missing results are not the same as passing results. Anything that stops the
    # checks from being recorded — a crash in the review block, a job that never got
    # that far — would otherwise produce a clean PASS from a gate that never ran.
    requested = meta.get("checks") or []
    if len(requested) > len(check_results):
        missing = len(requested) - len(check_results)
        reasons.append(
            f"{missing} of {len(requested)} requested checks produced no result"
        )

    return reasons


def verdict(meta: dict[str, Any]) -> dict[str, Any]:
    """Fold job outcome and recorded checks into a single verdict dictionary.

    Returns {"passed": bool, "reasons": [str, ...]}.
    """
    reasons: list[str] = []

    status = meta.get("status")
    if status != "done":
        if status == "failed":
            if meta.get("timedOut"):
                reasons.append("job failed (timed out)")
            elif meta.get("exitCode") is not None:
                reasons.append(f"job failed (exit {meta['exitCode']})")
            else:
                reasons.append("job failed")
        else:
            reasons.append(f"job status is {status}")

    reasons.extend(check_reasons(meta))

    if meta.get("write"):
        diff = meta.get("diff")
        if diff is None:
            cwd = meta.get("cwd")
            base_commit = meta.get("baseCommit")
            if cwd:
                diff = diff_summary(cwd, base_commit)
            else:
                diff = {"filesChanged": 0}

        if diff.get("filesChanged", 0) == 0:
            reasons.append("no files changed by a write job")

    passed = len(reasons) == 0
    return {"passed": passed, "reasons": reasons}


def format_review(meta: dict[str, Any]) -> str:
    """Format a job's review verdict into compact text for CLI and MCP."""
    v = verdict(meta)
    job_id = meta.get("id", "unknown")
    agent = meta.get("agent", "unknown")
    status = meta.get("status", "unknown")
    exit_code = meta.get("exitCode")

    exit_str = f" (exit {exit_code})" if exit_code is not None else ""
    header = f"Job {job_id}  agent: {agent}  status: {status}{exit_str}"

    verdict_label = "PASS" if v["passed"] else "FAIL"
    lines = [header, f"VERDICT: {verdict_label}"]

    for r in v["reasons"]:
        lines.append(f"  - {r}")

    check_results = meta.get("checkResults") or []
    if check_results:
        lines.append("")
        lines.append("checks:")
        for check in check_results:
            if isinstance(check, CheckResult):
                c_name = check.name
                c_passed = check.passed
                c_dur = check.durationMs
                c_out = check.output
            else:
                c_name = check.get("name", "")
                c_passed = check.get("passed", False)
                c_dur = check.get("durationMs", 0)
                c_out = check.get("output", "")

            status_tag = "[PASS]" if c_passed else "[FAIL]"
            dur_sec = c_dur / 1000.0
            lines.append(f"  {status_tag} {c_name:<20} {dur_sec:.1f}s")
            if not c_passed and c_out.strip():
                for out_line in c_out.strip().splitlines():
                    lines.append(f"    {out_line}")

    diff = meta.get("diff")
    if diff is None and meta.get("cwd"):
        diff = diff_summary(meta.get("cwd"), meta.get("baseCommit"))

    if diff and (diff.get("filesChanged", 0) > 0 or diff.get("commits", 0) > 0 or meta.get("baseCommit")):
        lines.append("")
        files_cnt = diff.get("filesChanged", 0)
        file_label = "file" if files_cnt == 1 else "files"
        commits_cnt = diff.get("commits", 0)
        commit_label = "commit" if commits_cnt == 1 else "commits"
        ins = diff.get("insertions", 0)
        dels = diff.get("deletions", 0)

        lines.append(f"diff vs base: {files_cnt} {file_label}, +{ins} -{dels}, {commits_cnt} {commit_label}")
        for f in diff.get("files", []):
            lines.append(f"  {f}")

    return "\n".join(lines)
