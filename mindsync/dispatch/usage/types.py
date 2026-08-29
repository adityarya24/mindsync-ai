"""Runtime usage-reader result models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class UsageWindow(BaseModel):
    """One provider usage window with consumed percentage and reset metadata."""

    id: str
    label: str
    used_percent: float = Field(ge=0.0, le=100.0)
    reset_at: datetime | None = None

    @field_validator("id", "label")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


class UsageReadResult(BaseModel):
    """Safe, provider-scoped usage observation."""

    status: Literal["available", "unavailable"]
    provider: str
    account_scope: str
    reader: str | None = None
    source: str | None = None
    windows: list[UsageWindow] = Field(default_factory=list)
    reason: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def unavailable(
        cls,
        *,
        provider: str,
        account_scope: str,
        reason: str,
        reader: str | None = None,
    ) -> UsageReadResult:
        return cls(
            status="unavailable",
            provider=provider,
            account_scope=account_scope,
            reader=reader,
            reason=reason,
        )

    @classmethod
    def available(
        cls,
        *,
        provider: str,
        account_scope: str,
        reader: str,
        source: str,
        windows: list[UsageWindow],
    ) -> UsageReadResult:
        return cls(
            status="available",
            provider=provider,
            account_scope=account_scope,
            reader=reader,
            source=source,
            windows=windows,
        )


class ThresholdEvaluation(BaseModel):
    """Threshold decision across all reported usage windows."""

    status: Literal["below_threshold", "at_threshold", "unavailable"]
    threshold_percent: float = Field(ge=0.0, le=100.0)
    provider: str
    account_scope: str
    windows: list[UsageWindow] = Field(default_factory=list)
    triggering_window: UsageWindow | None = None
    earliest_reset_at: datetime | None = None
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without secret-bearing fields for logs or job metadata."""
        payload = self.model_dump(mode="json", exclude_none=True)
        for window in payload.get("windows", []):
            window.pop("raw", None)
        return payload
