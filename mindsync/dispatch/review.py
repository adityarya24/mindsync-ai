"""Mechanical review gate for agent dispatch jobs."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class CheckResult(BaseModel):
    name: str
    passed: bool
    exitCode: int | None
    output: str  # tail only
    durationMs: int


def run_checks(
    cwd: str, checks: list[str], timeout_ms: int = 600_000
) -> list[CheckResult]:
    """Run check commands in cwd using platform shell, keeping stdout+stderr tail.

    A check that times out or fails to run is marked passed=False with exitCode=None.
    """
    results: list[CheckResult] = []
    timeout_sec = timeout_ms / 1000.0

    for cmd in checks:
        start = time.perf_counter()
        try:
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
                errors="replace",
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            output = res.stdout or ""
            if len(output) > 2000:
                output = output[-2000:]
            passed = res.returncode == 0
            results.append(
                CheckResult(
                    name=cmd,
                    passed=passed,
                    exitCode=res.returncode,
                    output=output,
                    durationMs=elapsed_ms,
                )
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            output = (exc.stdout or "") if hasattr(exc, "stdout") and exc.stdout else ""
            if len(output) > 2000:
                output = output[-2000:]
            results.append(
                CheckResult(
                    name=cmd,
                    passed=False,
                    exitCode=None,
                    output=output,
                    durationMs=elapsed_ms,
                )
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            results.append(
                CheckResult(
                    name=cmd,
                    passed=False,
                    exitCode=None,
                    output=f"Error executing check: {exc}"[-2000:],
                    durationMs=elapsed_ms,
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

    check_results = meta.get("checkResults") or []
    for check in check_results:
        if isinstance(check, CheckResult):
            c_name = check.name
            c_passed = check.passed
            c_exit = check.exitCode
        else:
            c_name = check.get("name", "unknown")
            c_passed = check.get("passed", False)
            c_exit = check.get("exitCode")

        if not c_passed:
            if c_exit is None:
                reasons.append(f'check "{c_name}" timed out')
            else:
                reasons.append(f'check "{c_name}" failed (exit {c_exit})')

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
