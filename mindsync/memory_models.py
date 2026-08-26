"""Local-only model calls for Tier 2 memory recall and consolidation."""

from __future__ import annotations

import ipaddress
import json
import math
import struct
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from mindsync import config

_MAX_MODEL_RESPONSE_BYTES = 2_000_000
_MAX_EMBEDDING_DIMENSIONS = 8_192
_FLOAT32_MAX = 3.4028235e38


class MemoryModelError(RuntimeError):
    """A visible, non-secret-bearing local model failure."""


def _loopback_base_url(base_url: str | None = None) -> str:
    raw = (base_url or config.settings.memory_model_url).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Memory model URL must be an HTTP(S) loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Memory model URL must not contain credentials, query, or fragment")
    host = parsed.hostname
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise ValueError("Memory model URL must resolve explicitly to loopback")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _post_json(endpoint: str, payload: dict[str, Any], *, base_url: str | None = None) -> dict[str, Any]:
    base = _loopback_base_url(base_url)
    request = Request(
        f"{base}/{endpoint.lstrip('/')}",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(
            request, timeout=config.settings.memory_model_timeout_seconds
        ) as response:
            body = response.read(_MAX_MODEL_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise MemoryModelError(f"Local memory model returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise MemoryModelError("Local memory model is unavailable") from exc
    if len(body) > _MAX_MODEL_RESPONSE_BYTES:
        raise MemoryModelError("Local memory model response exceeded the size limit")
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryModelError("Local memory model returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise MemoryModelError("Local memory model returned an invalid response object")
    return decoded


def embed_texts(
    texts: list[str],
    model: str,
    *,
    base_url: str | None = None,
) -> list[list[float]]:
    if not texts:
        return []
    if not isinstance(model, str) or not model.strip():
        raise ValueError("embedding model must be a non-empty string")
    response = _post_json(
        "/api/embed",
        {"model": model.strip(), "input": texts},
        base_url=base_url,
    )
    embeddings = response.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise MemoryModelError("Local embedding model returned the wrong vector count")
    normalized: list[list[float]] = []
    dimensions: int | None = None
    for vector in embeddings:
        if not isinstance(vector, list) or not vector:
            raise MemoryModelError("Local embedding model returned an empty vector")
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise MemoryModelError("Local embedding model returned a non-numeric vector") from exc
        if any(not math.isfinite(value) for value in values):
            raise MemoryModelError("Local embedding model returned a non-finite vector")
        if any(abs(value) > _FLOAT32_MAX for value in values):
            raise MemoryModelError(
                "Local embedding model returned values outside float32 range"
            )
        if dimensions is None:
            dimensions = len(values)
        elif len(values) != dimensions:
            raise MemoryModelError("Local embedding model returned inconsistent dimensions")
        if len(values) > _MAX_EMBEDDING_DIMENSIONS:
            raise MemoryModelError("Local embedding model returned too many dimensions")
        try:
            struct.pack(f"{len(values)}f", *values)
        except (OverflowError, struct.error) as exc:
            raise MemoryModelError(
                "Local embedding model returned values outside float32 range"
            ) from exc
        normalized.append(values)
    return normalized


def consolidate_facts(
    facts: list[dict[str, str]],
    model: str,
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    if len(facts) < 2:
        raise ValueError("consolidation requires at least two facts")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("consolidation model must be a non-empty string")
    fact_lines = "\n".join(
        json.dumps(
            {"fact_id": fact["fact_id"], "text": fact["text"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for fact in facts
    )
    schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "supporting_fact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
            },
        },
        "required": ["text", "supporting_fact_ids"],
        "additionalProperties": False,
    }
    response = _post_json(
        "/api/chat",
        {
            "model": model.strip(),
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Generalize only the supplied facts. Combine meaning supported by at "
                        "least two distinct facts into one concise paraphrase. Do not copy any "
                        "source text verbatim. Do not infer, predict, or add details. Return the "
                        "exact IDs that support the paraphrase."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Consolidate these already-redacted JSON Lines facts:\n"
                        + fact_lines
                    ),
                },
            ],
        },
        base_url=base_url,
    )
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise MemoryModelError("Local consolidation model returned no structured content")
    try:
        proposal = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MemoryModelError("Local consolidation model returned invalid structured content") from exc
    if not isinstance(proposal, dict):
        raise MemoryModelError("Local consolidation model returned an invalid proposal")
    return proposal
