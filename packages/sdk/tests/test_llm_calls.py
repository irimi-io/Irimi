from collections.abc import Callable
from typing import Any

import irimi
import pytest

TakeEvents = Callable[[], list[dict[str, Any]]]


@pytest.mark.parametrize(
    ("url", "kind", "name"),
    [
        ("http://api.anthropic.com/v1/messages", "llm", "anthropic messages.create"),
        ("http://API.Anthropic.com/v1/messages?beta=true", "llm", "anthropic messages.create"),
        ("http://api.openai.com/v1/chat/completions", "llm", "openai chat.completions.create"),
        ("http://api.openai.com/v1/responses", "llm", "openai responses.create"),
        (
            "http://openrouter.ai/api/v1/chat/completions",
            "llm",
            "openrouter chat.completions.create",
        ),
        (
            "http://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:generateContent",
            "llm",
            "gemini models.generate_content",
        ),
        (
            "http://generativelanguage.googleapis.com/v1/models/gemini-3-pro:streamGenerateContent?alt=sse",
            "llm",
            "gemini models.generate_content",
        ),
        (
            "http://api.anthropic.com/v1/messages/count_tokens",
            "http",
            "POST api.anthropic.com/v1/messages/count_tokens",
        ),
        ("http://api.openai.com/v1/embeddings", "http", "POST api.openai.com/v1/embeddings"),
        (
            "http://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:countTokens",
            "http",
            "POST generativelanguage.googleapis.com/v1beta/models/gemini-3-pro:countTokens",
        ),
        (
            "http://my-gateway.internal/v1/chat/completions",
            "http",
            "POST my-gateway.internal/v1/chat/completions",
        ),
    ],
)
def test_a_call_is_named_by_its_provider_and_operation(
    recorded_events: TakeEvents,
    upstream: Any,
    http_library: Any,
    url: str,
    kind: str,
    name: str,
) -> None:
    @irimi.workflow
    def ask() -> int:
        with http_library.Client(proxy=upstream.url) as client:
            answer = client.post(url, json={"model": "claude-opus-5-5", "messages": []})
        return int(answer.status_code)

    assert ask() == 200

    _, call_start, call_end, _ = recorded_events()

    assert (call_start["kind"], call_start["name"]) == (kind, name)
    assert call_start["request"]["url"].lower() == url.lower()
    assert "claude-opus-5-5" in call_start["request"]["body"]["data"]
    assert "gen_ai.request.model" not in call_start["request"]
    assert "usage" not in call_end
