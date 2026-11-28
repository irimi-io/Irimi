from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from irimi._wire import EndEvent, Event, JsonValue, StartEvent, field_names
from irimi._wire.markers import LITERAL, RESERVED_KEY, unescape_reserved_keys

CallRole = Literal["plain", "write_function", "read_function", "llm", "http_read", "http_write"]

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FUNCTION_KINDS = frozenset({"function", "workflow"})
HTTP_ROLES = frozenset({"llm", "http_read", "http_write"})
UNSERVABLE_RESPONSE_FLAGS = frozenset({"response_body_truncated", "response_body_incomplete"})


@dataclass(frozen=True)
class RecordedResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class RecordedOutput:
    value: JsonValue


GENERIC_FAKE_RESPONSE = RecordedResponse(
    200, (("content-type", "application/json"), ("content-length", "2")), b"{}"
)


@dataclass(frozen=True)
class RecordedCall:
    span_id: str
    name: str
    seq: int
    role: CallRole
    match_key: JsonValue
    result: RecordedResponse | RecordedOutput | None


@dataclass(frozen=True)
class RecordedRun:
    trace_id: str
    root: StartEvent
    calls: tuple[RecordedCall, ...]


class NotReplayableError(Exception):
    pass


def classify_call(kind: str, start_fields: Mapping[str, JsonValue]) -> CallRole:
    if kind == "llm":
        return "llm"
    if kind == "http":
        method = _read_object(start_fields, field_names.REQUEST).get("method")
        return "http_read" if method in READ_METHODS else "http_write"
    if kind in FUNCTION_KINDS:
        if start_fields.get(field_names.WRITE) is True:
            return "write_function"
        if start_fields.get(field_names.READ) is True:
            return "read_function"
    return "plain"


def key_call(role: CallRole, start_fields: Mapping[str, JsonValue]) -> JsonValue:
    if role in HTTP_ROLES:
        return _key_request(_read_object(start_fields, field_names.REQUEST))
    return _replace_unserialisable(start_fields.get(field_names.INPUT))


def read_recorded_run(events: Sequence[Event]) -> RecordedRun:
    starts = {event.span_id: event for event in events if isinstance(event, StartEvent)}
    ends = {event.span_id: event for event in events if isinstance(event, EndEvent)}
    root = _find_root(starts)
    roles = {span_id: classify_call(start.kind, start.fields) for span_id, start in starts.items()}
    calls = [
        _read_call(start, roles[span_id], ends.get(span_id))
        for span_id, start in starts.items()
        if start is not root and not _is_inside_write(start, starts, roles)
    ]
    calls.sort(key=lambda call: call.seq)
    return RecordedRun(trace_id=root.trace_id, root=root, calls=tuple(calls))


def _find_root(starts: Mapping[str, StartEvent]) -> StartEvent:
    roots = sorted(
        (start for start in starts.values() if start.parent_span_id is None),
        key=lambda start: start.seq,
    )
    if not roots:
        raise NotReplayableError("The recording has no root call, so there is nothing to replay.")
    if "not_replayable" in _read_list(roots[0].fields, field_names.FLAGS):
        raise NotReplayableError(
            "The recording is flagged not replayable, because its input was not fully recorded."
        )
    return roots[0]


def _is_inside_write(
    start: StartEvent, starts: Mapping[str, StartEvent], roles: Mapping[str, CallRole]
) -> bool:
    seen = {start.span_id}
    parent_span_id = start.parent_span_id
    while parent_span_id is not None and parent_span_id not in seen:
        seen.add(parent_span_id)
        if roles.get(parent_span_id) == "write_function":
            return True
        parent = starts.get(parent_span_id)
        parent_span_id = parent.parent_span_id if parent is not None else None
    return False


def _read_call(start: StartEvent, role: CallRole, end: EndEvent | None) -> RecordedCall:
    return RecordedCall(
        span_id=start.span_id,
        name=start.name,
        seq=start.seq,
        role=role,
        match_key=key_call(role, start.fields),
        result=_read_result(role, end),
    )


def _read_result(role: CallRole, end: EndEvent | None) -> RecordedResponse | RecordedOutput | None:
    if end is None or end.fields.get(field_names.STATUS) != "ok":
        return None
    flags = set(_read_list(end.fields, field_names.FLAGS))
    if role in HTTP_ROLES:
        if flags & UNSERVABLE_RESPONSE_FLAGS:
            return None
        return _read_response(_read_object(end.fields, field_names.RESPONSE))
    if "unserialisable_output" in flags:
        return None
    return RecordedOutput(end.fields.get(field_names.OUTPUT))


def _read_response(response: Mapping[str, JsonValue]) -> RecordedResponse | None:
    status = response.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    description = _read_object(response, "body")
    body = _decode_body(description)
    return RecordedResponse(
        status=status,
        headers=_make_servable(
            _read_headers(response), len(body), "content_encoding" in description
        ),
        body=body,
    )


def _decode_body(description: Mapping[str, JsonValue]) -> bytes:
    data = description.get("data")
    if not isinstance(data, str):
        return b""
    if description.get("encoding") == "base64":
        return base64.b64decode(data)
    return data.encode("utf-8")


def _read_headers(document: Mapping[str, JsonValue]) -> list[tuple[str, str]]:
    headers = unescape_reserved_keys(document.get("headers"))
    if not isinstance(headers, dict):
        return []
    return [(name, value) for name, value in headers.items() if isinstance(value, str)]


def _make_servable(
    headers: list[tuple[str, str]], body_length: int, has_content_encoding: bool
) -> tuple[tuple[str, str], ...]:
    dropped = {"transfer-encoding", "content-length"}
    if not has_content_encoding:
        dropped.add("content-encoding")
    kept = [(name, value) for name, value in headers if name.lower() not in dropped]
    return (*kept, ("content-length", str(body_length)))


def _key_request(request: Mapping[str, JsonValue]) -> JsonValue:
    return [request.get("method"), request.get("url"), _key_body(_read_object(request, "body"))]


def _key_body(description: Mapping[str, JsonValue]) -> JsonValue:
    if not description:
        return None
    encoding = description.get("encoding")
    data = description.get("data")
    if encoding == "text" and isinstance(data, str):
        try:
            parsed: JsonValue = json.loads(data)
        except ValueError:
            return [encoding, data]
        return parsed
    return [encoding, data]


def _replace_unserialisable(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_replace_unserialisable(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get(RESERVED_KEY)
    if marker == "unserialisable":
        return None
    if marker == LITERAL:
        return value
    return {name: _replace_unserialisable(item) for name, item in value.items()}


def _read_object(fields: Mapping[str, JsonValue], name: str) -> Mapping[str, JsonValue]:
    value = fields.get(name)
    return value if isinstance(value, dict) else {}


def _read_list(fields: Mapping[str, JsonValue], name: str) -> list[JsonValue]:
    value = fields.get(name)
    return value if isinstance(value, list) else []
