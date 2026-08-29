"""Focus overlap detection between local agents."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{1,}")

# Generic filler/dev words that don't identify a work area on their own;
# an overlap must come from something specific (module, path, feature name).
_STOPWORDS = {
    "with", "from", "this", "that", "into", "about", "using", "focus",
    "the", "and", "for", "are", "was", "will", "then", "when", "where",
    "fix", "fixing", "fixes", "bug", "bugs", "add", "adding", "added",
    "update", "updating", "updated", "change", "changes", "changing",
    "work", "working", "code", "coding", "new", "old", "file", "files",
    "implement", "implementing", "improve", "improving", "refactor",
    "refactoring", "rewrite", "rewriting", "edit", "editing", "issue",
    "issues", "task", "tasks", "feature", "features", "some", "more",
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def tokenize_focus(focus: str) -> set[str]:
    """Extract meaningful tokens from a free-text focus string."""
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(focus.lower()):
        if raw in _STOPWORDS:
            continue
        if len(raw) >= 3:
            tokens.add(raw)
    return tokens


def detect_focus_conflicts(
    agent_name: str,
    project: str,
    branch: str,
    focus: str,
    agents_focus: dict[str, Any],
    *,
    paths: list[str] | None = None,
    stale_seconds: int = 7200,
    now: datetime | None = None,
) -> list[str]:
    """Return warnings only when non-stale agents share project + focus tokens or path overlap.

    Same project alone is NOT a conflict (avoids the original always-true bug).
    """
    del branch  # reserved for future branch-aware heuristics / API stability
    now = now or datetime.now(timezone.utc)
    my_tokens = tokenize_focus(focus)
    my_paths = paths or []
    warnings: list[str] = []

    for other_agent, other in agents_focus.items():
        if other_agent == agent_name:
            continue
        if not isinstance(other, dict):
            continue

        other_ts = _parse_ts(other.get("timestamp"))
        if other_ts is None:
            continue
        if other_ts.tzinfo is None:
            other_ts = other_ts.replace(tzinfo=timezone.utc)
        age = (now - other_ts).total_seconds()
        if age < 0 or age > stale_seconds:
            continue

        other_project = other.get("project") or other.get("active_project")
        # If the other agent recorded a project and it differs, skip.
        if other_project is not None and other_project != project:
            continue

        # Check path overlaps
        other_paths = other.get("paths") or []
        overlap_paths = []
        for p1 in my_paths:
            for p2 in other_paths:
                np1 = p1.replace("\\", "/").strip("/").lower()
                np2 = p2.replace("\\", "/").strip("/").lower()
                if np1 == np2 or np1.startswith(np2 + "/") or np2.startswith(np1 + "/"):
                    overlap_paths.append(p2)

        if overlap_paths:
            overlap_paths = sorted(list(set(overlap_paths)))
            warnings.append(
                f"CONFLICT WARNING: Agent '{other_agent}' claims paths: {', '.join(overlap_paths)}"
            )
            continue

        other_focus = str(other.get("focus") or "")
        other_tokens = tokenize_focus(other_focus)
        overlap = sorted(my_tokens & other_tokens)
        if not overlap:
            continue

        other_branch = other.get("branch") or "?"
        warnings.append(
            "CONFLICT WARNING: "
            f"Agent '{other_agent}' is active in project '{other_project or project}' "
            f"(branch '{other_branch}') focusing on '{other_focus}'. "
            f"Your focus '{focus}' may overlap on: {', '.join(overlap)}."
        )

    return warnings


def list_active_agents(
    agents_focus: dict[str, Any],
    *,
    stale_seconds: int = 7200,
    now: datetime | None = None,
) -> list[str]:
    """Return agent names whose focus entries are fresh enough to count as active."""
    now = now or datetime.now(timezone.utc)
    active: list[str] = []
    for agent_name, entry in agents_focus.items():
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(entry.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()
        if age < 0 or age > stale_seconds:
            continue
        active.append(agent_name)
    return sorted(active)
