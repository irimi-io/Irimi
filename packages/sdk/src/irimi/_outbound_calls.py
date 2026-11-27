from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from irimi._bodies import CopiedBody, copy_body, describe_body
from irimi._credential_headers import replace_credential_values
from irimi._errors import describe_error
from irimi._recording import Payload
from irimi._wire import Flag, JsonValue, field_names, write_flags

Headers = tuple[tuple[str, str], ...]


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


@dataclass(frozen=True)
class CallOutcome:
    response: OutboundResponse | None
    body: CopiedBody | None
    is_body_complete: bool
    error: BaseException | None


def name_http_call(call: OutboundCall) -> str:
    address = urlsplit(call.url)
    return f"{call.method} {address.hostname or ''}{address.path or '/'}"


def _remove_userinfo(url: str) -> str:
    address = urlsplit(url).netloc
    _, at_sign, host_and_port = address.rpartition("@")
    if not at_sign:
        return url
    return url.replace(address, host_and_port, 1)


def describe_request(call: OutboundCall, is_attached: bool) -> Payload:
    url = _remove_userinfo(call.url)
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
    elif call.body:
        body = copy_body(call.body)
        request["body"] = _describe_body(body, call.headers)
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
        response = _describe_response(outcome.response, recorded_body)
        fields[field_names.RESPONSE] = dict(response.fields)
        fields = _add_flags(
            fields, _name_response_body_flags(recorded_body, outcome.is_body_complete)
        )
        size += response.approximate_size
    return Payload(fields, size)


def _name_response_body_flags(body: CopiedBody | None, is_body_complete: bool) -> list[Flag]:
    flags: list[Flag] = []
    if body is not None and body.is_truncated:
        flags.append("response_body_truncated")
    if not is_body_complete:
        flags.append("response_body_incomplete")
    return flags


def _add_flags(fields: dict[str, JsonValue], flags: list[Flag]) -> dict[str, JsonValue]:
    if not flags:
        return fields
    return {**fields, field_names.FLAGS: write_flags(*flags)}


def _describe_response(response: OutboundResponse, body: CopiedBody | None) -> Payload:
    described: dict[str, JsonValue] = {
        "status": response.status,
        "headers": replace_credential_values(response.headers),
    }
    size = _measure_header_values(response.headers)
    if body is not None and body.content:
        described["body"] = _describe_body(body, response.headers)
        size += len(body.content)
    return Payload(described, size)


def _describe_body(body: CopiedBody, headers: Headers) -> dict[str, JsonValue]:
    return describe_body(
        body, _find_header(headers, "content-type"), _find_header(headers, "content-encoding")
    )


def _find_header(headers: Sequence[tuple[str, str]], lower_case_name: str) -> str | None:
    return next((value for name, value in headers if name.lower() == lower_case_name), None)


def _measure_header_values(headers: Sequence[tuple[str, str]]) -> int:
    return sum(len(value) for _, value in headers)
