from __future__ import annotations

import base64
import codecs
from dataclasses import dataclass

from irimi._wire import JsonValue

BODY_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class CopiedBody:
    content: bytes
    is_truncated: bool


class BodyCopy:
    def __init__(self) -> None:
        self._content = bytearray()
        self._is_truncated = False

    def add(self, chunk: bytes) -> None:
        room = BODY_LIMIT - len(self._content)
        if len(chunk) > room:
            self._is_truncated = True
        self._content += chunk[:room]

    def take(self) -> CopiedBody:
        return CopiedBody(content=bytes(self._content), is_truncated=self._is_truncated)


def copy_body(content: bytes) -> CopiedBody:
    body_copy = BodyCopy()
    body_copy.add(content)
    return body_copy.take()


def describe_body(
    body: CopiedBody, content_type: str | None, content_encoding: str | None
) -> dict[str, JsonValue]:
    description = _encode(body)
    if content_type is not None:
        description["content_type"] = content_type
    if content_encoding is not None and content_encoding.lower() != "identity":
        description["content_encoding"] = content_encoding
    return description


def _encode(body: CopiedBody) -> dict[str, JsonValue]:
    try:
        return {"encoding": "text", "data": _decode_text(body)}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(body.content).decode("ascii")}


def _decode_text(body: CopiedBody) -> str:
    if not body.is_truncated:
        return body.content.decode("utf-8")
    decoder = codecs.getincrementaldecoder("utf-8")()
    return decoder.decode(body.content, final=False)
