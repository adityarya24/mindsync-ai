"""Deterministic capability-based routing for installed CLI agents."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Iterable

from mindsync.dispatch.adapters import AdapterConfig, load_adapters
from mindsync.dispatch.limits import cooldown_reason
from mindsync.dispatch.proc import resolve_bin
from mindsync.dispatch.usage.config import UsageConfig, load_usage_config
from mindsync.dispatch.usage.preemptive import preflight_skip_reason, preemptive_usage_active
from mindsync.dispatch.usage.registry import evaluate_adapter_threshold
from mindsync.dispatch.usage.types import ThresholdEvaluation

HEADROOM_BONUS_SPAN = 50.0


KNOWN_CAPABILITIES = frozenset({
    "architecture",
    "coding",
    "debugging",
    "devops",
    "general",
    "large-context",
    "multimodal",
    "reasoning",
    "refactoring",
    "repository",
    "research",
    "review",
    "security",
    "testing",
    "writing",
})
HEAVY_CAPABILITIES = frozenset({"security", "large-context", "multimodal"})

_CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "architecture": ("architecture", "architect", "design", "system design"),
    "coding": ("code", "coding", "implement", "build", "feature", "fix", "bug"),
    "debugging": ("debug", "root cause", "traceback", "crash", "failing"),
    "devops": ("devops", "deploy", "docker", "kubernetes", "ci", "cd", "vps", "server"),
    "large-context": ("large context", "whole repo", "entire repo", "full codebase"),
    "multimodal": ("image", "screenshot", "video", "audio", "multimodal"),
    "reasoning": ("reason", "analyze", "complex", "tradeoff", "strategy"),
    "refactoring": ("refactor", "cleanup", "restructure", "simplify"),
    "repository": ("repo", "repository", "codebase", "git"),
    "research": ("research", "investigate", "compare", "find", "documentation", "docs"),
    "review": ("review", "audit", "critique", "inspect", "verify"),
    "security": ("security", "secure", "vulnerability", "threat", "auth", "injection"),
    "testing": ("test", "tests", "pytest", "regression", "reproduce", "verification"),
    "writing": ("write", "writing", "readme", "document", "summarize", "content"),
}


def normalize_capabilities(values: Iterable[str] | None) -> list[str]:
    """Normalize user-supplied capability names without changing their order."""
    result: list[str] = []
    for raw in values or []:
        value = raw.strip().lower()
        if value and value not in result:
            result.append(value)
    return result


def infer_capabilities(prompt: str) -> list[str]:
    """Infer broad work capabilities from a task when the orchestrator supplies none."""
    text = re.sub(r"\s+", " ", prompt.lower())
    inferred = [
        capability
        for capability, keywords in _CAPABILITY_KEYWORDS.items()
        if any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords)
    ]
    return inferred or ["general"]


def _hottest_used_percent(evaluation: ThresholdEvaluation | None) -> float | None:
    if evaluation is None or evaluation.status == "unavailable":
        return None
    percents = [window.used_percent for window in evaluation.windows]
    if not percents:
        return None
    return max(percents)


def _headroom_bonus(used_percent: float | None) -> float:
    """Signed headroom in the same band as routingPriority.

    Unknown/unreadable usage is 0 (neutral), not the same as 100% used.
    Fresh (0% used) is +50; exhausted (100% used) is -50.
    """
    if used_percent is None:
        return 0.0
    return max(-HEADROOM_BONUS_SPAN, min(HEADROOM_BONUS_SPAN, HEADROOM_BONUS_SPAN - used_percent))


def _format_used_percent(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _candidate(
    adapter: AdapterConfig,
    required: list[str],
    *,
    used_percent: float | None = None,
) -> dict[str, Any] | None:
    capabilities = set(adapter.capabilities or ["general"])
    matched = [capability for capability in required if capability in capabilities]
    if not matched:
        return None
    capability_score = sum(adapter.capabilityWeights.get(capability, 50) for capability in matched)
    bonus = _headroom_bonus(used_percent)
    score = capability_score * 100 + adapter.routingPriority + bonus
    return {
        "agent": adapter.name,
        "displayName": adapter.displayName or adapter.name,
        "family": adapter.family or adapter.name,
        "capabilities": sorted(capabilities),
        "matchedCapabilities": matched,
        "routingPriority": adapter.routingPriority,
        "capabilityScore": capability_score,
        "usedPercent": used_percent,
        "headroomBonus": bonus,
        "score": score,
    }


def _usage_reason_suffix(ranked: list[dict[str, Any]]) -> str:
    winner = ranked[0]
    used = winner.get("usedPercent")
    if used is None:
        return ""
    suffix = f"; {_format_used_percent(used)}% used"
    for other in ranked[1:]:
        other_used = other.get("usedPercent")
        if other_used is not None:
            return f"{suffix} vs {other['agent']} {_format_used_percent(other_used)}%"
    return suffix


def select_agent(
    prompt: str,
    *,
    required_capabilities: Iterable[str] | None = None,
    exclude_agents: Iterable[str] | None = None,
    adapters: dict[str, AdapterConfig] | None = None,
    usage_config: UsageConfig | None = None,
    usage_aware: bool = False,
    on_limit: str | None = None,
    evaluator: Callable[..., ThresholdEvaluation] | None = None,
) -> dict[str, Any]:
    """Choose the best installed agent and return an explainable ranked decision."""
    required = normalize_capabilities(required_capabilities) or infer_capabilities(prompt)
    excluded = {name.strip().lower() for name in (exclude_agents or []) if name.strip()}
    config = usage_config or load_usage_config()
    usage_filter = usage_aware and preemptive_usage_active(
        usage_config=config, on_limit=on_limit
    )
    evaluate_fn = evaluator or evaluate_adapter_threshold
    evaluations: dict[str, ThresholdEvaluation] = {}

    def evaluation_for(adapter: AdapterConfig) -> ThresholdEvaluation | None:
        if not config.enabled:
            return None
        cached = evaluations.get(adapter.name)
        if cached is not None:
            return cached
        observed = evaluate_fn(adapter, usage_config=config)
        evaluations[adapter.name] = observed
        return observed

    available: list[AdapterConfig] = []
    unavailable: list[str] = []
    unavailable_reasons: dict[str, str] = {}

    for adapter in (adapters or load_adapters()).values():
        if adapter.name.lower() in excluded:
            continue
        cooling = cooldown_reason(adapter)
        if cooling:
            unavailable.append(adapter.name)
            unavailable_reasons[adapter.name] = cooling
            continue
        if usage_filter:
            skip = preflight_skip_reason(
                adapter,
                usage_config=config,
                evaluation=evaluation_for(adapter),
                evaluator=evaluate_fn,
            )
            if skip:
                unavailable.append(adapter.name)
                unavailable_reasons[adapter.name] = skip
                continue
        if resolve_bin(adapter.bin):
            available.append(adapter)
        else:
            unavailable.append(adapter.name)
            unavailable_reasons[adapter.name] = "binary is not installed"

    if not available:
        suffix = f" Excluded: {', '.join(sorted(excluded))}." if excluded else ""
        detail = "; ".join(
            f"{name}: {unavailable_reasons[name]}" for name in sorted(unavailable_reasons)
        )
        unavailable_suffix = f" Unavailable: {detail}." if detail else ""
        raise RuntimeError(
            f"No installed dispatch agents are available.{suffix}{unavailable_suffix}"
        )

    ranked = [
        candidate
        for adapter in available
        if (
            candidate := _candidate(
                adapter,
                required,
                used_percent=_hottest_used_percent(evaluation_for(adapter)),
            )
        )
    ]
    ranked.sort(key=lambda item: (-item["score"], item["agent"]))
    if not ranked:
        advertised = "; ".join(
            f"{adapter.name}={','.join(adapter.capabilities or ['general'])}"
            for adapter in available
        )
        raise ValueError(
            "No installed agent matches required capabilities "
            f"{', '.join(required)}. Available profiles: {advertised}"
        )

    winner = ranked[0]
    matched = ", ".join(winner["matchedCapabilities"])
    missing = [
        capability
        for capability in required
        if capability not in winner["matchedCapabilities"]
    ]
    coverage = len(winner["matchedCapabilities"]) / len(required)
    gap = f" Missing: {', '.join(missing)}." if missing else ""
    decision = {
        **winner,
        "requiredCapabilities": required,
        "missingCapabilities": missing,
        "coverage": coverage,
        "reason": (
            f"Selected {winner['agent']} because it is installed and matched: {matched}; "
            f"routing priority {winner['routingPriority']}"
            f"{_usage_reason_suffix(ranked)}.{gap}"
        ),
        "candidates": ranked,
        "unavailableAgents": sorted(unavailable),
        "unavailableReasons": {
            name: unavailable_reasons[name] for name in sorted(unavailable_reasons)
        },
        "excludedAgents": sorted(excluded),
    }
    return decision
