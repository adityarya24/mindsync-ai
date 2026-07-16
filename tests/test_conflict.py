from datetime import datetime, timedelta, timezone

from mindsync.conflict import detect_focus_conflicts, tokenize_focus


def test_tokenize_focus_tokens():
    tokens = tokenize_focus("fix auth.py and api routes")
    assert "auth.py" in tokens
    assert "api" in tokens
    assert "routes" in tokens


def test_no_false_positive_same_project_different_focus():
    now = datetime.now(timezone.utc)
    agents = {
        "agent-a": {
            "project": "mindsync-mcp",
            "branch": "main",
            "focus": "rewrite README docs",
            "timestamp": now.isoformat(),
        }
    }
    warnings = detect_focus_conflicts(
        "agent-b",
        "mindsync-mcp",
        "feature/locks",
        "add file locking to storage",
        agents,
        now=now,
    )
    assert warnings == []


def test_token_overlap_same_project_warns():
    now = datetime.now(timezone.utc)
    agents = {
        "agent-a": {
            "project": "mindsync-mcp",
            "branch": "main",
            "focus": "fix mindsync/storage.py locking",
            "timestamp": now.isoformat(),
        }
    }
    warnings = detect_focus_conflicts(
        "agent-b",
        "mindsync-mcp",
        "feature/locks",
        "rewrite mindsync/storage.py queue flush",
        agents,
        now=now,
    )
    assert len(warnings) == 1
    assert "storage.py" in warnings[0]


def test_stale_agent_ignored():
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=5)).isoformat()
    agents = {
        "agent-a": {
            "project": "mindsync-mcp",
            "branch": "main",
            "focus": "fix mindsync/storage.py",
            "timestamp": stale,
        }
    }
    warnings = detect_focus_conflicts(
        "agent-b",
        "mindsync-mcp",
        "main",
        "edit mindsync/storage.py",
        agents,
        stale_seconds=7200,
        now=now,
    )
    assert warnings == []


def test_different_project_no_warning_even_with_token_overlap():
    now = datetime.now(timezone.utc)
    agents = {
        "agent-a": {
            "project": "other-app",
            "branch": "main",
            "focus": "fix storage.py",
            "timestamp": now.isoformat(),
        }
    }
    warnings = detect_focus_conflicts(
        "agent-b",
        "mindsync-mcp",
        "main",
        "fix storage.py",
        agents,
        now=now,
    )
    assert warnings == []
