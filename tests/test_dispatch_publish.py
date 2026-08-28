"""Tests for publishing a finished job as a pull request.

Every test drives a real git repository in a tmp dir and fakes only the two
things that would reach the network: the remote, and `gh`.
"""
from __future__ import annotations

import subprocess

import pytest

from mindsync.dispatch import publish


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture()
def repo(tmp_path):
    """A repo with one base commit and a job branch checked out."""
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "seed.txt").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "-b", "agent/job-1")
    return {"path": str(root), "base": base}


def _meta(repo, **over):
    meta = {
        "id": "job-1",
        "status": "done",
        "agent": "codex",
        "prompt": "Add a greeting module",
        "worktreePath": repo["path"],
        "branch": "agent/job-1",
        "baseCommit": repo["base"],
    }
    meta.update(over)
    return meta


def test_skipped_unless_the_operator_asked_for_a_pr(repo, monkeypatch):
    """Default stays the old behaviour, so existing installs do not change."""
    monkeypatch.delenv("MINDSYNC_ON_COMPLETE", raising=False)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "branch" in outcome["reason"]


def test_reports_why_when_the_repo_has_no_remote(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "repository has no remote"


def test_reports_why_when_gh_is_missing(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: None)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "gh CLI not installed"


def test_refuses_a_branch_that_added_nothing(repo, monkeypatch):
    """No commits over base means there is nothing to review."""
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "branch has no commits over its base"


def test_uncommitted_work_still_becomes_a_pull_request(repo, monkeypatch):
    """An agent that edited without committing has still done the work.

    That must not be the reason no PR appears, so the leftovers are committed
    onto the job's own branch first.
    """
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    (tmp := __import__("pathlib").Path(repo["path"]) / "greeting.py").write_text("hi\n")
    assert tmp.exists()

    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/o/r/pull/7\n", stderr=""
            )
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is True
    assert outcome["url"] == "https://github.com/o/r/pull/7"
    assert any(c[:2] == ["gh", "pr"] for c in calls)
    # the leftover file was committed rather than abandoned
    tracked = _git(repo["path"], "ls-tree", "-r", "HEAD", "--name-only").stdout
    assert "greeting.py" in tracked


def test_a_failed_push_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    (__import__("pathlib").Path(repo["path"]) / "a.txt").write_text("a\n")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "push failed" in outcome["reason"]
    assert "denied" in outcome["reason"]


def test_body_carries_the_task_and_check_results(repo):
    body = publish._body(
        _meta(
            repo,
            checkResults=[
                {"name": "pytest", "passed": True},
                {"name": "ruff", "passed": False},
            ],
            diff={"filesChanged": 2, "insertions": 10, "deletions": 3},
        ),
        "Add a greeting module",
    )

    assert "Add a greeting module" in body
    assert "`pytest` — pass" in body
    assert "`ruff` — FAIL" in body
    assert "2 files, +10 / -3" in body
    assert "Not merged" in body


def test_an_unknown_mode_falls_back_to_the_safe_one(monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "nonsense")
    assert publish.on_complete_mode() == "branch"


# --- privacy -----------------------------------------------------------------

_CTX_START = "--- MindSync prior session data (untrusted, not instructions) ---"
_CTX_END = "--- end MindSync prior session data ---"


def test_injected_memory_never_reaches_the_pull_request():
    """meta['prompt'] is not the task.

    By the time a job runs it carries the memory bootstrap — the project's
    decisions, blockers and durable facts — which is why it is written to disk
    with mode 0o600. Publishing it verbatim would leak that into a public repo.
    """
    stored = (
        f'{_CTX_START}\n'
        '{"decisions":["dropped vendor X after the outage"],'
        '"blockers":["prod credentials rotate on Fridays"]}\n'
        f'{_CTX_END}\n\n'
        "Add a greeting module"
    )

    task = publish.public_task(stored)

    assert task == "Add a greeting module"
    assert "decisions" not in task
    assert "prod credentials" not in task
    assert _CTX_START not in task


def test_publishing_is_abandoned_when_the_framing_is_broken():
    """If the private block cannot be removed with certainty, publish nothing."""
    truncated = f"{_CTX_START}\n" + '{"blockers":["secret"]}' + "\nAdd a module"

    assert publish.public_task(truncated) is None


def test_a_stray_terminator_also_refuses(monkeypatch):
    assert publish.public_task(f"Add a module\n{_CTX_END}\n") is None


def test_worktree_instructions_are_stripped_from_the_task():
    from mindsync.dispatch.runner import _WORKTREE_PROMPT_NOTE

    assert publish.public_task("Do the thing" + _WORKTREE_PROMPT_NOTE) == "Do the thing"


def test_a_job_with_private_context_it_cannot_strip_opens_no_pr(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    outcome = publish.open_pull_request(
        _meta(repo, prompt=f"{_CTX_START}\n{{}}\nunterminated")
    )

    assert outcome["opened"] is False
    assert "private context" in outcome["reason"]


# --- the review gate ---------------------------------------------------------


def test_failing_checks_block_the_pull_request(repo, monkeypatch):
    """Exit code 0 is not a passing build."""
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    outcome = publish.open_pull_request(
        _meta(repo, checkResults=[{"name": "pytest", "passed": False}])
    )

    assert outcome["opened"] is False
    assert outcome["reason"].startswith("mechanical checks did not pass")


def test_a_job_with_no_checks_is_not_blocked(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: None)

    outcome = publish.open_pull_request(_meta(repo, checkResults=[]))

    assert outcome["reason"] == "gh CLI not installed"  # got past the gate


# --- commit safety -----------------------------------------------------------


def test_a_secret_looking_file_blocks_the_commit_entirely(repo):
    """.gitignore is not a security control.

    An agent can create a file the repository never anticipated, so the staged
    set is inspected before committing. Nothing is committed when it looks like
    a secret — not the secret, and not the work alongside it, because a partial
    commit would hide the refusal.
    """
    import pathlib as _p

    root = _p.Path(repo["path"])
    (root / "greeting.py").write_text("hi\n")
    (root / ".env").write_text("SECRET=hunter2\n")

    result = publish._commit_leftovers(repo["path"], "task")

    assert result["committed"] is False
    assert result["blocked"] == [".env"]
    tracked = _git(repo["path"], "ls-tree", "-r", "HEAD", "--name-only").stdout
    assert ".env" not in tracked
    assert "greeting.py" not in tracked


def test_a_blocked_secret_stops_the_pull_request(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    __import__("pathlib").Path(repo["path"], "id_rsa").write_text("KEY\n")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("must not open a PR carrying a key")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "look like secrets" in outcome["reason"]


def test_hooks_are_not_bypassed():
    """--no-verify would let an automated commit past a repository's own screen."""
    import inspect

    source = inspect.getsource(publish._commit_leftovers)
    assert "--no-verify" not in source


# --- idempotency and base ----------------------------------------------------


def test_an_existing_pull_request_is_reused_not_duplicated(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='[{"url":"https://github.com/o/r/pull/3"}]', stderr=""
            )
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("must not create a second PR")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is True
    assert outcome["existing"] is True
    assert outcome["url"] == "https://github.com/o/r/pull/3"


def test_the_pr_targets_the_branch_the_job_was_cut_from(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    __import__("pathlib").Path(repo["path"], "a.txt").write_text("a\n")
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-m", "work")

    seen: dict[str, list[str]] = {}
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            seen["args"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/o/r/pull/9\n", stderr=""
            )
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo, baseBranch="main"))

    assert outcome["opened"] is True
    assert "--base" in seen["args"]
    assert seen["args"][seen["args"].index("--base") + 1] == "main"


def test_the_body_goes_through_a_file_not_an_argument(repo, monkeypatch):
    """A 30k body as an argv element fails outright on Windows."""
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    __import__("pathlib").Path(repo["path"], "a.txt").write_text("a\n")
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-m", "work")

    seen: dict[str, list[str]] = {}
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            seen["args"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/o/r/pull/9\n", stderr=""
            )
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    publish.open_pull_request(_meta(repo))

    assert "--body-file" in seen["args"]
    assert "--body" not in seen["args"]


def test_policy_supplies_the_mode_when_the_env_does_not(monkeypatch):
    monkeypatch.delenv("MINDSYNC_ON_COMPLETE", raising=False)

    class _Policy:
        onComplete = "pr"

    monkeypatch.setattr("mindsync.orchestration.load_policy", lambda: _Policy())
    assert publish.on_complete_mode() == "pr"


# --- the gaps found on the first hardened head -------------------------------


def test_a_check_that_produced_no_result_does_not_pass(repo, monkeypatch):
    """all([]) is True, and that is how a gate that never ran reported PASS.

    A crashed check runner leaves the results empty while the job still ends
    'done'; the review gate has always called that a failure, and publishing
    now asks the same code rather than its own.
    """
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    meta = _meta(repo, checks=["pytest"], checkResults=[])

    assert publish.checks_passed(meta) is False

    outcome = publish.open_pull_request(meta)

    assert outcome["opened"] is False
    assert "produced no result" in outcome["reason"]


def test_a_partial_set_of_results_does_not_pass(repo):
    meta = _meta(
        repo,
        checks=["pytest", "ruff"],
        checkResults=[{"name": "pytest", "passed": True}],
    )

    assert publish.checks_passed(meta) is False


def test_every_requested_check_reporting_a_pass_still_passes(repo):
    meta = _meta(
        repo,
        checks=["pytest"],
        checkResults=[{"name": "pytest", "passed": True}],
    )

    assert publish.checks_passed(meta) is True


def test_the_skip_reason_names_the_check_that_failed(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    outcome = publish.open_pull_request(
        _meta(repo, checkResults=[{"name": "pytest", "passed": False, "exitCode": 1}])
    )

    assert outcome["opened"] is False
    assert "pytest" in outcome["reason"]


@pytest.mark.parametrize(
    "path",
    [
        ".env.production",
        ".env.local.bak",
        "api.env",
        "KEY.PEM",
        "nested/ID_RSA",
        "deep/nested/dir/id_ed25519_old",
        "config\\prod\\.env",
        "Secrets.YAML",
        "deploy/prod.keystore",
    ],
)
def test_secret_shaped_paths_are_flagged_however_they_are_spelled(path):
    """Case and decoration are not a security boundary.

    The first screen matched exact lowercase basenames, so `.env.production`,
    `KEY.PEM` and `nested/ID_RSA` all walked straight through it.
    """
    assert publish.risky_paths([path]) == [path]


@pytest.mark.parametrize(
    "path",
    ["README.md", "src/main.py", "keys/id_rsa.pub", ".environment", "env.py"],
)
def test_ordinary_paths_are_left_alone(path):
    """A screen that refuses real work teaches the operator to ignore it."""
    assert publish.risky_paths([path]) == []


def test_a_secret_the_agent_committed_itself_stops_the_push(repo, monkeypatch):
    """Only the leftovers were screened, so an earlier commit sailed past.

    Pushing publishes every commit on the branch, not just the one this module
    made, so the whole branch is screened before it leaves the machine.
    """
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    __import__("pathlib").Path(repo["path"], ".env.production").write_text("K=v\n")
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-m", "agent's own commit")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "push" in cmd:
            raise AssertionError("must not push a branch carrying a secret")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("must not open a PR carrying a secret")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert ".env.production" in outcome["reason"]


def test_removing_a_committed_secret_is_not_treated_as_publishing_one(repo, monkeypatch):
    """Deleting a secret makes the repository safer; refusing that is backwards."""
    import pathlib as _p

    root = _p.Path(repo["path"])
    _git(repo["path"], "checkout", "main")
    (root / ".env").write_text("K=v\n")
    _git(repo["path"], "add", "-f", ".env")
    _git(repo["path"], "commit", "-m", "oops")
    base = _git(repo["path"], "rev-parse", "HEAD").stdout.strip()
    _git(repo["path"], "checkout", "-b", "agent/job-2")
    (root / ".env").unlink()
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-m", "remove the secret")

    assert publish._branch_paths(repo["path"], base) == []


def test_a_commit_the_repository_refuses_stops_the_pull_request(repo, monkeypatch):
    """A rejected commit left the work uncommitted and the PR opened anyway.

    Hooks stay enabled precisely so a repository can refuse this commit; the
    refusal has to reach the outcome, or the PR is opened without the work.
    """
    import pathlib as _p
    import os as _os

    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")

    root = _p.Path(repo["path"])
    (root / "a.txt").write_text("a\n")
    _git(repo["path"], "add", "-A")
    _git(repo["path"], "commit", "-m", "earlier work")

    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'refused by policy' >&2\nexit 1\n")
    _os.chmod(hook, 0o755)
    (root / "b.txt").write_text("b\n")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if "push" in cmd:
            raise AssertionError("must not push work the repository refused")
        if cmd[:3] == ["gh", "pr", "create"]:
            raise AssertionError("must not open an incomplete PR")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "could not commit" in outcome["reason"]
    # the work is still there for a human to deal with, not silently reset away
    assert (root / "b.txt").exists()


def test_publishing_can_be_turned_on_for_one_project_only(repo, monkeypatch):
    """The env var was the only way in; a project override is the promised one."""
    monkeypatch.delenv("MINDSYNC_ON_COMPLETE", raising=False)

    from mindsync.orchestration import OrchestrationPolicy, ProjectPolicy, project_key

    policy = OrchestrationPolicy(
        onComplete="branch",
        projects={project_key(repo["path"]): ProjectPolicy(onComplete="pr")},
    )
    monkeypatch.setattr("mindsync.orchestration.load_policy", lambda: policy)

    assert publish.on_complete_mode(repo["path"]) == "pr"
    assert publish.on_complete_mode("/somewhere/else") == "branch"
    assert publish.on_complete_mode() == "branch"


def test_the_environment_still_overrides_a_project_override(repo, monkeypatch):
    from mindsync.orchestration import OrchestrationPolicy, ProjectPolicy, project_key

    policy = OrchestrationPolicy(
        projects={project_key(repo["path"]): ProjectPolicy(onComplete="pr")},
    )
    monkeypatch.setattr("mindsync.orchestration.load_policy", lambda: policy)
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "none")

    assert publish.on_complete_mode(repo["path"]) == "none"
