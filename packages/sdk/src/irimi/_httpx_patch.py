from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import ModuleType
from typing import Any

from irimi._bodies import BodyCopy, CopiedBody
from irimi._interception import Interception, OpenCall
from irimi._outbound_calls import CallOutcome, Headers, OutboundCall, OutboundResponse

TRANSPORT_METHODS = (
    "HTTPTransport.handle_request",
    "AsyncHTTPTransport.handle_async_request",
)
PATCH_TARGETS = (
    *TRANSPORT_METHODS,
    "SyncByteStream",
    "AsyncByteStream",
    "ByteStream",
    "RequestNotRead",
)


def patch_httpx(library: ModuleType, interception: Interception) -> bool:
    if not all(_find_attribute(library, path) is not None for path in PATCH_TARGETS):
        return False
    if not all(_takes_one_request(_find_attribute(library, path)) for path in TRANSPORT_METHODS):
        return False
    handle_request = _wrap_sync_transport(library, interception)
    handle_async_request = _wrap_async_transport(library, interception)
    library.HTTPTransport.handle_request = handle_request
    library.AsyncHTTPTransport.handle_async_request = handle_async_request
    return True


class _ResponseRecording:
    def __init__(
        self, interception: Interception, open_call: OpenCall, response: OutboundResponse
    ) -> None:
        self._interception = interception
        self._open_call = open_call
        self._response = response
        self._body: BodyCopy | None = BodyCopy() if open_call.is_attached else None
        self._has_copy_failed = False
        self._is_ended = False
        self._end_lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        body_copy = self._body
        if body_copy is None:
            return
        try:
            body_copy.add(chunk)
        except Exception:
            self._body = None
            self._has_copy_failed = True
            self._interception.report_failure("copy an outbound response body")

    def end(self, was_read_to_end: bool, error: BaseException | None) -> None:
        if not self._claim_end():
            return
        try:
            outcome = CallOutcome(
                response=self._response,
                body=self._release_body(),
                was_read_to_end=was_read_to_end and not self._has_copy_failed,
                error=error,
            )
        except Exception:
            self._interception.report_failure("take the copy of an outbound response body")
            return
        self._interception.end(self._open_call, outcome)

    def _release_body(self) -> CopiedBody | None:
        body_copy, self._body = self._body, None
        return body_copy.take() if body_copy is not None else None

    def _claim_end(self) -> bool:
        with self._end_lock:
            if self._is_ended:
                return False
            self._is_ended = True
            return True


def _wrap_sync_transport(
    library: ModuleType, interception: Interception
) -> Callable[[object, Any], Any]:
    send_request = library.HTTPTransport.handle_request
    stream_class = type(
        "RecordingSyncByteStream",
        (library.SyncByteStream,),
        {"__init__": _hold_stream, "__iter__": _iterate_sync_stream, "close": _close_sync_stream},
    )

    @functools.wraps(send_request)
    def handle_request(transport: object, request: Any) -> Any:
        open_call = _begin_call(interception, request, library)
        try:
            response = send_request(transport, request)
        except BaseException as error:
            _end_without_response(interception, open_call, error)
            raise
        _record_response(interception, open_call, response, stream_class)
        return response

    return handle_request


def _wrap_async_transport(
    library: ModuleType, interception: Interception
) -> Callable[[object, Any], Awaitable[Any]]:
    send_request = library.AsyncHTTPTransport.handle_async_request
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
        open_call = _begin_call(interception, request, library)
        try:
            response = await send_request(transport, request)
        except BaseException as error:
            _end_without_response(interception, open_call, error)
            raise
        _record_response(interception, open_call, response, stream_class)
        return response

    return handle_async_request


def _begin_call(interception: Interception, request: Any, library: ModuleType) -> OpenCall | None:
    try:
        call = _translate_request(request, library)
    except Exception:
        interception.report_failure("read an outbound request")
        return None
    return interception.begin(call)


def _end_without_response(
    interception: Interception, open_call: OpenCall | None, error: BaseException
) -> None:
    outcome = CallOutcome(response=None, body=None, was_read_to_end=False, error=error)
    interception.end(open_call, outcome)


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


def _translate_request(request: Any, library: ModuleType) -> OutboundCall:
    in_memory_body = _find_in_memory_body(request, library)
    return OutboundCall(
        method=str(request.method),
        url=str(request.url),
        headers=_decode_headers(request.headers.raw),
        body=in_memory_body or None,
        has_streamed_body=in_memory_body is None,
    )


def _find_in_memory_body(request: Any, library: ModuleType) -> bytes | None:
    try:
        return bytes(request.content)
    except library.RequestNotRead:
        return _read_unloaded_byte_stream(request.stream, library)


def _read_unloaded_byte_stream(stream: Any, library: ModuleType) -> bytes | None:
    if not isinstance(stream, library.ByteStream):
        return None
    return b"".join(stream)


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
    try:
        for chunk in stream.inner_stream:
            stream.recording.add(chunk)
            yield chunk
    except GeneratorExit:
        raise
    except BaseException as error:
        stream.recording.end(was_read_to_end=False, error=error)
        raise
    stream.recording.end(was_read_to_end=True, error=None)


def _close_sync_stream(stream: Any) -> None:
    try:
        stream.recording.end(was_read_to_end=False, error=None)
    finally:
        stream.inner_stream.close()


async def _iterate_async_stream(stream: Any) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream.inner_stream:
            stream.recording.add(chunk)
            yield chunk
    except GeneratorExit:
        raise
    except BaseException as error:
        stream.recording.end(was_read_to_end=False, error=error)
        raise
    stream.recording.end(was_read_to_end=True, error=None)


async def _close_async_stream(stream: Any) -> None:
    try:
        stream.recording.end(was_read_to_end=False, error=None)
    finally:
        await stream.inner_stream.aclose()


def _takes_one_request(send_request: object) -> bool:
    if not callable(send_request):
        return False
    return list(inspect.signature(send_request).parameters)[1:] == ["request"]


def _find_attribute(root: object, path: str) -> object:
    found = root
    for name in path.split("."):
        found = getattr(found, name, None)
    return found
