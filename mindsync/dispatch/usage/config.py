"""Usage-reader configuration parsing and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mindsync.dispatch.adapters import user_config_path

DEFAULT_THRESHOLD_PERCENT = 90
DEFAULT_POLLING_INTERVAL_SECONDS = 60
MIN_POLLING_INTERVAL_SECONDS = 5
MAX_POLLING_INTERVAL_SECONDS = 600
KNOWN_READERS = frozenset({"codex-oauth"})


class UsageConfig(BaseModel):
    """Global usage-reader settings from agents.json."""

    enabled: bool = False
    defaultThresholdPercent: int = Field(default=DEFAULT_THRESHOLD_PERCENT, ge=1, le=100)
    pollingIntervalSeconds: int = Field(
        default=DEFAULT_POLLING_INTERVAL_SECONDS,
        ge=MIN_POLLING_INTERVAL_SECONDS,
        le=MAX_POLLING_INTERVAL_SECONDS,
    )

    @field_validator("defaultThresholdPercent", mode="before")
    @classmethod
    def _coerce_threshold(cls, value: Any) -> Any:
        if value is None:
            return DEFAULT_THRESHOLD_PERCENT
        return value


def validate_reader_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = name.strip().lower()
    if not cleaned:
        return None
    if cleaned not in KNOWN_READERS:
        raise ValueError(
            f"Unknown usage reader '{name}'. Known readers: {', '.join(sorted(KNOWN_READERS))}"
        )
    return cleaned


def load_usage_config(path: Path | None = None) -> UsageConfig:
    """Load usage settings from agents.json without changing dispatch defaults."""
    config_path = path or user_config_path()
    if not config_path.is_file():
        return UsageConfig()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Your agents.json at {config_path} is invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Your agents.json at {config_path} is not an object")
    usage = data.get("usage")
    if usage is None:
        return UsageConfig()
    if not isinstance(usage, dict):
        raise ValueError(f"usage in {config_path} must be an object")
    return UsageConfig.model_validate(usage)


def effective_threshold_percent(
    *,
    usage_config: UsageConfig,
    adapter_threshold: int | None,
) -> int:
    if adapter_threshold is not None:
        return adapter_threshold
    return usage_config.defaultThresholdPercent
