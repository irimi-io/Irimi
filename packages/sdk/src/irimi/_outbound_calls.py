from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from irimi._bodies import CopiedBody, copy_body, describe_body
from irimi._credential_headers import replace_credential_values
from irimi._errors import describe_error
from irimi._llm_providers import recognise_llm_call
from irimi._recording import Payload
from irimi._url_credentials import remove_userinfo
from irimi._wire import Flag, JsonValue, field_names, write_flags

Headers = tuple[tuple[str, str], ...]

EVENT_STREAM_TYPE = "text/event-stream"
EVENT_ENDINGS = (b"\n\n", b"\r\r", b"\r\n\r\n")


@dataclass(frozen=True)
class OutboundCall:
    method: str
    url: str
    headers: Headers
    body: bytes | None
    has_streamed_body: bool


@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: Headers
    is_body_decoded: bool = False


@dataclass(frozen=True)
class CallOutcome:
    response: OutboundResponse | None
    body: CopiedBody | None
    was_read_to_end: bool
    error: BaseException | None


@dataclass(frozen=True)
class CallIdentity:
    kind: Literal["http", "llm"]
    name: str


def identify_call(call: OutboundCall) -> CallIdentity:
    address = urlsplit(call.url)
    host = address.hostname or ""
    path = address.path or "/"
    llm_name = recognise_llm_call(call.method, host, path)
    if llm_name is not None:
        return CallIdentity("llm", llm_name)
    return CallIdentity("http", f"{call.method} {host}{path}")


def describe_request(call: OutboundCall, is_attached: bool) -> Payload:
    url = remove_userinfo(call.url)
    request: dict[str, JsonValue] = {
        "method": call.method,
        "url": url,
        "headers": replace_credential_values(call.headers),
    }
    size = len(url) + _measure_header_values(call.headers)
    flags: list[Flag] = []
    if not is_attached:
        flags.append("unattached")
    elif call.has_streamed_body:
        flags.append("request_body_not_recorded")
    elif call.body is not None:
        body = copy_body(call.body)
        request["body"] = _describe_body(body, call.headers, is_whole=True)
        size += len(body.content)
        if body.is_truncated:
            flags.append("request_body_truncated")
    fields: dict[str, JsonValue] = {field_names.REQUEST: request}
    return Payload(_add_flags(fields, flags), size)


def describe_outcome(outcome: CallOutcome, is_attached: bool) -> Payload:
    status = (
        describe_error(outcome.error)
        if outcome.error is not None
        else Payload({field_names.STATUS: "ok"}, 0)
    )
    fields: dict[str, JsonValue] = {**status.fields, field_names.ANSWERED: "live"}
    size = status.approximate_size
    if outcome.response is not None:
        recorded_body = outcome.body if is_attached else None
        is_whole = outcome.was_read_to_end or _was_closed_after_an_event(outcome, recorded_body)
        response = _describe_response(outcome.response, recorded_body, is_whole)
        fields[field_names.RESPONSE] = dict(response.fields)
        fields = _add_flags(fields, _name_response_body_flags(recorded_body, is_whole))
        size += response.approximate_size
    return Payload(fields, size)


def _was_closed_after_an_event(outcome: CallOutcome, body: CopiedBody | None) -> bool:
    if outcome.error is not None or outcome.response is None or body is None:
        return False
    content_type = _find_header(outcome.response.headers, "content-type") or ""
    is_readable_event_stream = (
        content_type.lower().startswith(EVENT_STREAM_TYPE)
        and _find_body_encoding(outcome.response) is None
        and not body.is_truncated
    )
    return is_readable_event_stream and body.content.endswith(EVENT_ENDINGS)


def _name_response_body_flags(body: CopiedBody | None, is_whole: bool) -> list[Flag]:
    flags: list[Flag] = []
    if body is not None and body.is_truncated:
        flags.append("response_body_truncated")
    if not is_whole:
        flags.append("response_body_incomplete")
    return flags


def _add_flags(fields: dict[str, JsonValue], flags: list[Flag]) -> dict[str, JsonValue]:
    if not flags:
        return fields
    return {**fields, field_names.FLAGS: write_flags(*flags)}


def _describe_response(
    response: OutboundResponse, body: CopiedBody | None, is_whole: bool
) -> Payload:
    described: dict[str, JsonValue] = {
        "status": response.status,
        "headers": replace_credential_values(response.headers),
    }
    size = _measure_header_values(response.headers)
    if body is not None and body.content:
        described["body"] = describe_body(
            body,
            _find_header(response.headers, "content-type"),
            _find_body_encoding(response),
            is_whole,
        )
        size += len(body.content)
    return Payload(described, size)


def _find_body_encoding(response: OutboundResponse) -> str | None:
    if response.is_body_decoded:
        return None
    return _find_header(response.headers, "content-encoding")


def _describe_body(body: CopiedBody, headers: Headers, is_whole: bool) -> dict[str, JsonValue]:
    return describe_body(
        body,
        _find_header(headers, "content-type"),
        _find_header(headers, "content-encoding"),
        is_whole,
    )


def _find_header(headers: Sequence[tuple[str, str]], lower_case_name: str) -> str | None:
    return next((value for name, value in headers if name.lower() == lower_case_name), None)


def _measure_header_values(headers: Sequence[tuple[str, str]]) -> int:
    return sum(len(value) for _, value in headers)
