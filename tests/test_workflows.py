"""Static checks for release and dependency-lock workflow guardrails."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_gate_checks_tag_version_master_ancestry_and_push_ci():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'pathlib.Path("mindsync/__init__.py")' in workflow
    assert 'expected_tag="v${package_version}"' in workflow
    assert '"${GITHUB_REF_NAME}" != "$expected_tag"' in workflow
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
        "astral-sh/setup-uv@eb1897b8dc4b5d5bfe39a428a8f2304605e0983c"
        in workflow
    )
    assert 'version: "0.11.26"' in workflow
    assert "uv lock --check" in workflow


def test_workflows_pin_node24_action_releases():
    workflows = "\n".join(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("ci.yml", "release.yml")
    )

    expected_pins = (
        "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "astral-sh/setup-uv@eb1897b8dc4b5d5bfe39a428a8f2304605e0983c",
        "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
        "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131",
        "softprops/action-gh-release@3d0d9888cb7fd7b750713d6e236d1fcb99157228",
        "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247",
    )

    assert all(pin in workflows for pin in expected_pins)
