"""Adapter presets, user agents.json merge, and invocation building."""

from __future__ import annotations

import json
import os
import re
import subprocess
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
    "defaultModel": None,
    "efforts": [],
    "effortArgs": [],
    "modelsArgs": None,
    "models": [],
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

    defaultModel: str | None = None
    efforts: list[str] = Field(default_factory=list)
    effortArgs: list[str] = Field(default_factory=list)
    modelsArgs: list[str] | None = None
    models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_prompt_placeholder(self) -> AdapterConfig:
        if self.input == "arg" and not any("{prompt}" in t for t in self.runArgs):
            raise ValueError(
                f"Adapter '{self.name}': input 'arg' requires a {{prompt}} placeholder in runArgs"
            )
        if self.efforts and not any("{effort}" in t for t in self.effortArgs):
            raise ValueError(f"Adapter '{self.name}': efforts is set but effortArgs lacks {{effort}}")
        if self.effortArgs and not self.efforts:
            raise ValueError(f"Adapter '{self.name}': effortArgs is set but efforts is empty")
        if self.defaultModel and not any("{model}" in t for t in self.modelArgs):
            raise ValueError(f"Adapter '{self.name}': defaultModel is set but modelArgs lacks {{model}}")
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


def list_models(adapter: AdapterConfig) -> list[str]:
    if not adapter.modelsArgs:
        return list(adapter.models)
    
    from mindsync.dispatch.proc import resolve_bin
    bin_path = resolve_bin(adapter.bin)
    if not bin_path:
        return list(adapter.models)

    try:
        res = subprocess.run(
            [bin_path, *adapter.modelsArgs],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return list(adapter.models)
        
        models = []
        for line in res.stdout.splitlines():
            line = line.strip()
            # strip leading * or -
            line = re.sub(r"^[*>-]\s*", "", line)
            # strip trailing (default) marker
            line = re.sub(r"\s*\(default\)$", "", line)
            
            if not line:
                continue
            if " " in line or line.endswith(":"):
                continue
                
            models.append(line)
        return models or list(adapter.models)
    except Exception:
        return list(adapter.models)


def build_invocation(
    adapter: AdapterConfig,
    *,
    prompt: str,
    model: str | None = None,
    effort: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    eff_model = model or adapter.defaultModel
    if eff_model:
        if not adapter.modelArgs:
            raise ValueError(f"Adapter '{adapter.name}' does not support models (modelArgs is empty) but model '{eff_model}' was requested. Fix this in {user_config_path()}")
        if not SAFE_MODEL.match(eff_model):
            raise ValueError(
                f"Invalid model '{eff_model}': use letters, digits, and . _ / : - only"
            )

    if effort:
        if not adapter.effortArgs or not adapter.efforts:
            raise ValueError(f"Adapter '{adapter.name}' does not support reasoning effort but effort '{effort}' was requested.")
        if effort.lower() not in [e.lower() for e in adapter.efforts]:
            raise ValueError(f"Invalid effort '{effort}' for adapter '{adapter.name}'. Allowed values: {', '.join(adapter.efforts)}")
        if not SAFE_MODEL.match(effort):
            raise ValueError(f"Invalid effort '{effort}': use letters, digits, and . _ / : - only")

    args = list(adapter.runArgs)
    if write:
        args.extend(adapter.writeArgs)
    if eff_model:
        args.extend(t.replace("{model}", eff_model) for t in adapter.modelArgs)
    if effort:
        # keep case for substitution
        actual_effort = next(e for e in adapter.efforts if e.lower() == effort.lower())
        args.extend(t.replace("{effort}", actual_effort) for t in adapter.effortArgs)
        
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
