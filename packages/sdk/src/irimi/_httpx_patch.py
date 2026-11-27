from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Iterator
from types import ModuleType
from typing import Any

from irimi._bodies import copy_body
from irimi._interception import Interception, OpenCall
from irimi._outbound_calls import CallOutcome, Headers, OutboundCall, OutboundResponse

PATCH_TARGETS = (
    "HTTPTransport.handle_request",
    "AsyncHTTPTransport.handle_async_request",
    "SyncByteStream",
    "AsyncByteStream",
    "RequestNotRead",
)


def patch_httpx(library: ModuleType, interception: Interception) -> bool:
    if not all(_find_attribute(library, path) is not None for path in PATCH_TARGETS):
        return False
    _patch_sync_transport(library, interception)
    _patch_async_transport(library, interception)
    return True


class _ResponseRecording:
    def __init__(
        self, interception: Interception, open_call: OpenCall, response: OutboundResponse
    ) -> None:
        self._interception = interception
        self._open_call = open_call
        self._response = response
        self._chunks: list[bytes] = []
        self._is_ended = False

    def add(self, chunk: bytes) -> None:
        try:
            self._chunks.append(chunk)
        except Exception:
            self._is_ended = True
            self._interception.report_failure("copy an outbound response")

    def end(self, is_body_complete: bool) -> None:
        if self._is_ended:
            return
        self._is_ended = True
        try:
            body = copy_body(b"".join(self._chunks))
        except Exception:
            self._interception.report_failure("copy an outbound response")
            return
        outcome = CallOutcome(
            response=self._response, body=body, is_body_complete=is_body_complete, error=None
        )
        self._interception.end(self._open_call, outcome)


def _patch_sync_transport(library: ModuleType, interception: Interception) -> None:
    transport_class = library.HTTPTransport
    send_request = transport_class.handle_request
    stream_class = type(
        "RecordingSyncByteStream",
        (library.SyncByteStream,),
        {"__init__": _hold_stream, "__iter__": _iterate_sync_stream, "close": _close_sync_stream},
    )

    @functools.wraps(send_request)
    def handle_request(transport: object, request: Any) -> Any:
        open_call = _begin_call(interception, request, library.RequestNotRead)
        response = send_request(transport, request)
        _record_response(interception, open_call, response, stream_class)
        return response

    transport_class.handle_request = handle_request


def _patch_async_transport(library: ModuleType, interception: Interception) -> None:
    transport_class = library.AsyncHTTPTransport
    send_request = transport_class.handle_async_request
    stream_class = type(
        "RecordingAsyncByteStream",
        (library.AsyncByteStream,),
        {
            "__init__": _hold_stream,
            "__aiter__": _iterate_async_stream,
            "aclose": _close_async_stream,
        },
    )

    @functools.wraps(send_request)
    async def handle_async_request(transport: object, request: Any) -> Any:
        open_call = _begin_call(interception, request, library.RequestNotRead)
        response = await send_request(transport, request)
        _record_response(interception, open_call, response, stream_class)
        return response

    transport_class.handle_async_request = handle_async_request


def _begin_call(interception: Interception, request: Any, request_not_read: Any) -> OpenCall | None:
    try:
        call = _translate_request(request, request_not_read)
    except Exception:
        interception.report_failure("read an outbound request")
        return None
    return interception.begin(call)


def _record_response(
    interception: Interception, open_call: OpenCall | None, response: Any, stream_class: type
) -> None:
    if open_call is None:
        return
    try:
        recording = _ResponseRecording(interception, open_call, _translate_response(response))
        response.stream = stream_class(response.stream, recording)
    except Exception:
        interception.report_failure("record an outbound response")


def _translate_request(request: Any, request_not_read: Any) -> OutboundCall:
    body = _read_request_body(request, request_not_read)
    return OutboundCall(
        method=str(request.method),
        url=str(request.url),
        headers=_decode_headers(request.headers.raw),
        body=body,
        has_streamed_body=body is None,
    )


def _read_request_body(request: Any, request_not_read: Any) -> bytes | None:
    try:
        return bytes(request.content)
    except request_not_read:
        return None


def _translate_response(response: Any) -> OutboundResponse:
    return OutboundResponse(
        status=int(response.status_code), headers=_decode_headers(response.headers.raw)
    )


def _decode_headers(raw_headers: Any) -> Headers:
    return tuple((name.decode("latin-1"), value.decode("latin-1")) for name, value in raw_headers)


def _hold_stream(stream: Any, inner_stream: Any, recording: _ResponseRecording) -> None:
    stream.inner_stream = inner_stream
    stream.recording = recording


def _iterate_sync_stream(stream: Any) -> Iterator[bytes]:
    for chunk in stream.inner_stream:
        stream.recording.add(chunk)
        yield chunk
    stream.recording.end(is_body_complete=True)


def _close_sync_stream(stream: Any) -> None:
    stream.recording.end(is_body_complete=False)
    stream.inner_stream.close()


async def _iterate_async_stream(stream: Any) -> AsyncIterator[bytes]:
    async for chunk in stream.inner_stream:
        stream.recording.add(chunk)
        yield chunk
    stream.recording.end(is_body_complete=True)


async def _close_async_stream(stream: Any) -> None:
    stream.recording.end(is_body_complete=False)
    await stream.inner_stream.aclose()


def _find_attribute(root: object, path: str) -> object:
    found = root
    for name in path.split("."):
        found = getattr(found, name, None)
    return found
