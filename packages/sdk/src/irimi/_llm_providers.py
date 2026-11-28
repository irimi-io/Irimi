from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderOperation:
    host: str
    path_segments: tuple[str, ...]
    name: str


PROVIDER_OPERATIONS = (
    ProviderOperation("api.anthropic.com", ("v1", "messages"), "anthropic messages.create"),
    ProviderOperation(
        "api.openai.com", ("v1", "chat", "completions"), "openai chat.completions.create"
    ),
    ProviderOperation("api.openai.com", ("v1", "responses"), "openai responses.create"),
    ProviderOperation(
        "openrouter.ai", ("api", "v1", "chat", "completions"), "openrouter chat.completions.create"
    ),
    ProviderOperation(
        "generativelanguage.googleapis.com",
        ("{version}", "models", "{model}:generateContent"),
        "gemini models.generate_content",
    ),
    ProviderOperation(
        "generativelanguage.googleapis.com",
        ("{version}", "models", "{model}:streamGenerateContent"),
        "gemini models.generate_content",
    ),
)


def recognise_llm_call(method: str, host: str, path: str) -> str | None:
    lower_case_host = host.lower()
    segments = tuple(path.split("/")[1:])
    return next(
        (
            operation.name
            for operation in PROVIDER_OPERATIONS
            if operation.host == lower_case_host
            and _has_matching_segments(operation.path_segments, segments)
        ),
        None,
    )


def _has_matching_segments(patterns: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    return len(patterns) == len(segments) and all(map(_is_matching_segment, patterns, segments))


def _is_matching_segment(pattern: str, segment: str) -> bool:
    _, placeholder_end, ending = pattern.rpartition("}")
    if not placeholder_end:
        return segment == pattern
    return len(segment) > len(ending) and segment.endswith(ending)
