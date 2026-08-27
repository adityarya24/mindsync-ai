"""Static checks for release and dependency-lock workflow guardrails."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_gate_checks_master_ancestry_and_push_ci():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "git fetch --no-tags origin master" in workflow
    assert 'git merge-base --is-ancestor "$sha" origin/master' in workflow
    assert "branch=master" in workflow
    assert "event=push" in workflow
    assert 'head_branch == \\"master\\"' in workflow
    assert 'event == \\"push\\"' in workflow


def test_ci_has_pinned_uv_lock_check():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e"
        in workflow
    )
    assert 'version: "0.11.26"' in workflow
    assert "uv lock --check" in workflow
