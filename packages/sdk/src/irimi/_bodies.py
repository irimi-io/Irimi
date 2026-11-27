from __future__ import annotations

import base64
from dataclasses import dataclass

from irimi._wire import JsonValue

BODY_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class CopiedBody:
    content: bytes
    is_truncated: bool


def copy_body(content: bytes) -> CopiedBody:
    return CopiedBody(content=content[:BODY_LIMIT], is_truncated=len(content) > BODY_LIMIT)


def describe_body(
    body: CopiedBody, content_type: str | None, content_encoding: str | None
) -> dict[str, JsonValue]:
    description = _encode(body.content)
    if content_type is not None:
        description["content_type"] = content_type
    if content_encoding is not None and content_encoding.lower() != "identity":
        description["content_encoding"] = content_encoding
    return description


def _encode(content: bytes) -> dict[str, JsonValue]:
    try:
        return {"encoding": "text", "data": content.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(content).decode("ascii")}
