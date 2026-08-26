"""Security and response validation for the loopback-only model adapter."""

from __future__ import annotations

import json

import pytest

import mindsync.memory_models as models


class _Response:
    def __init__(self, payload: dict[str, object]):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


@pytest.mark.parametrize(
    "url",
    [
        "https://models.example.com",
        "http://localhost.evil.example",
        "http://localhost:11434",
        "http://user:password@127.0.0.1:11434",
        "file:///tmp/model.sock",
    ],
)
def test_model_url_rejects_non_loopback_or_credentialed_targets(url: str):
    with pytest.raises(ValueError, match="loopback|credentials"):
        models._loopback_base_url(url)


def test_embed_calls_loopback_api_and_validates_vectors(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float):
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data)
        seen["timeout"] = timeout
        return _Response({"embeddings": [[1, 2], [3, 4]]})

    monkeypatch.setattr(models, "urlopen", fake_urlopen)
    result = models.embed_texts(
        ["one", "two"], "nomic-embed-text", base_url="http://[::1]:11434"
    )

    assert result == [[1.0, 2.0], [3.0, 4.0]]
    assert seen["url"] == "http://[::1]:11434/api/embed"
    assert seen["payload"] == {
        "model": "nomic-embed-text",
        "input": ["one", "two"],
    }


def test_consolidation_requests_strict_structured_output(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict[str, object] = {}
    fact_ids = ["a" * 32, "b" * 32]

    def fake_urlopen(request: object, timeout: float):
        seen["payload"] = json.loads(request.data)
        content = json.dumps(
            {"text": "General fact", "supporting_fact_ids": fact_ids}
        )
        return _Response({"message": {"content": content}})

    monkeypatch.setattr(models, "urlopen", fake_urlopen)
    result = models.consolidate_facts(
        [
            {"fact_id": fact_ids[0], "text": "First\nredacted fact"},
            {"fact_id": fact_ids[1], "text": "Second redacted fact"},
        ],
        "qwen-local",
    )

    assert result["supporting_fact_ids"] == fact_ids
    payload = seen["payload"]
    assert payload["stream"] is False
    assert payload["format"]["additionalProperties"] is False
    assert payload["options"] == {"temperature": 0}
    system_prompt = payload["messages"][0]["content"]
    assert "at least two distinct facts" in system_prompt
    assert "Do not copy any source text verbatim" in system_prompt
    prompt_lines = payload["messages"][1]["content"].splitlines()[1:]
    parsed_lines = [json.loads(line) for line in prompt_lines]
    assert parsed_lines[0] == {
        "fact_id": fact_ids[0],
        "text": "First\nredacted fact",
    }
    assert len(parsed_lines) == 2


def test_model_response_size_is_bounded(monkeypatch: pytest.MonkeyPatch):
    class OversizedResponse(_Response):
        def read(self, size: int = -1) -> bytes:
            return b"x" * size

    monkeypatch.setattr(
        models,
        "urlopen",
        lambda request, timeout: OversizedResponse({}),
    )

    with pytest.raises(models.MemoryModelError, match="size limit"):
        models.embed_texts(["one"], "embed-local")


def test_embedding_dimensions_and_float32_magnitude_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        models,
        "urlopen",
        lambda request, timeout: _Response({"embeddings": [[1e300]]}),
    )

    with pytest.raises(models.MemoryModelError, match="float32"):
        models.embed_texts(["one"], "embed-local")
