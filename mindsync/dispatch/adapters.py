"""Adapter presets, user agents.json merge, and invocation building."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")

_DEFAULTS: dict[str, Any] = {
    "displayName": None,
    "input": "stdin",
    "runArgs": [],
    "writeArgs": [],
    "modelArgs": [],
    "detectArgs": ["--version"],
    "authCheckArgs": None,
    "loginHint": None,
    "installHint": None,
    "timeoutMs": 600_000,
}


class UnknownAgentError(KeyError):
    def __init__(self, name: str, available: list[str]) -> None:
        avail = ", ".join(available) or "(none)"
        super().__init__(
            f"Unknown agent '{name}'. Available: {avail}. "
            f"Add custom agents in {user_config_path()}"
        )
        self.name = "UnknownAgentError"


class AdapterConfig(BaseModel):
    name: str
    bin: str
    displayName: str | None = None
    input: Literal["stdin", "arg"] = "stdin"
    runArgs: list[str] = Field(default_factory=list)
    writeArgs: list[str] = Field(default_factory=list)
    modelArgs: list[str] = Field(default_factory=list)
    detectArgs: list[str] = Field(default_factory=lambda: ["--version"])
    authCheckArgs: list[str] | None = None
    loginHint: str | None = None
    installHint: str | None = None
    timeoutMs: int = 600_000

    @model_validator(mode="after")
    def _require_prompt_placeholder(self) -> AdapterConfig:
        if self.input == "arg" and not any("{prompt}" in t for t in self.runArgs):
            raise ValueError(
                f"Adapter '{self.name}': input 'arg' requires a {{prompt}} placeholder in runArgs"
            )
        return self


def dispatch_home() -> Path:
    env = os.environ.get("AGENT_DISPATCH_HOME")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "agent-dispatch"


def user_config_path() -> Path:
    return dispatch_home() / "agents.json"


def presets_dir() -> Path:
    return Path(__file__).resolve().parent / "presets"


def _validate_raw(data: dict[str, Any]) -> AdapterConfig:
    if not data.get("name") or not data.get("bin"):
        raise ValueError(f"Adapter missing name/bin: {data!r}")
    merged = {**_DEFAULTS, **data}
    return AdapterConfig.model_validate(merged)


def load_adapters() -> dict[str, AdapterConfig]:
    """Load bundled presets then merge ~/.claude/agent-dispatch/agents.json."""
    out: dict[str, AdapterConfig] = {}
    pdir = presets_dir()
    if pdir.is_dir():
        for path in sorted(pdir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = _validate_raw(raw)
            out[cfg.name] = cfg

    user_path = user_config_path()
    if user_path.is_file():
        try:
            user = json.loads(user_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Your agents.json at {user_path} is invalid: {exc}") from exc
        for entry in user.get("agents") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            base = out.get(name)
            merged: dict[str, Any] = {
                **_DEFAULTS,
                **(base.model_dump() if base else {}),
                **entry,
            }
            cfg = _validate_raw(merged)
            out[cfg.name] = cfg
    return out


def resolve_adapter(name: str) -> AdapterConfig:
    adapters = load_adapters()
    try:
        return adapters[name]
    except KeyError as exc:
        raise UnknownAgentError(name, sorted(adapters.keys())) from exc


def build_invocation(
    adapter: AdapterConfig,
    *,
    prompt: str,
    model: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    if model and not SAFE_MODEL.match(model):
        raise ValueError(
            f"Invalid model '{model}': use letters, digits, and . _ / : - only"
        )
    args = list(adapter.runArgs)
    if write:
        args.extend(adapter.writeArgs)
    if model:
        args.extend(t.replace("{model}", model) for t in adapter.modelArgs)
    input_text: str | None = None
    if adapter.input == "stdin":
        input_text = prompt
    else:
        args = [t.replace("{prompt}", prompt) for t in args]
    return {
        "bin": adapter.bin,
        "args": args,
        "input": input_text,
        "timeoutMs": adapter.timeoutMs,
    }
