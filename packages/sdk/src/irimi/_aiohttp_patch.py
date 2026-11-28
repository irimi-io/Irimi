from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from irimi._interception import Interception, OpenCall
from irimi._outbound_calls import Headers, OutboundCall, OutboundResponse
from irimi._response_recording import ResponseRecording, end_without_response

SWITCHING_PROTOCOLS = 101
INHERITED_ITERATORS = frozenset({"iter_chunked", "iter_any", "iter_chunks"})
FORWARDED_METHODS = (
    "exception",
    "on_eof",
    "feed_eof",
    "feed_data",
    "is_eof",
    "at_eof",
    "wait_eof",
    "unread_data",
    "begin_http_chunk_receiving",
    "end_http_chunk_receiving",
    "get_read_buffer_limits",
    "set_read_chunk_size",
)
FORWARDED_PROPERTIES = ("total_bytes", "total_compressed_bytes", "total_raw_bytes")
READS_OF_BYTES = ("read", "readany", "readexactly", "readline", "readuntil")

Middleware = Callable[[Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]


def patch_aiohttp(library: ModuleType, interception: Interception) -> bool:
    send_request = library.ClientSession._request
    if "middlewares" not in inspect.signature(send_request).parameters:
        return False
    if not hasattr(library.BytesPayload, "as_bytes"):
        return False
    if not hasattr(library.StreamReader, "total_compressed_bytes"):
        return False
    reader_class = _build_recording_reader(library)
    if reader_class is None:
        return False
    record_call = _build_recording_middleware(library, interception, reader_class)
    library.ClientSession._request = _wrap_request(send_request, interception, record_call)
    return True


def _wrap_request(
    send_request: Callable[..., Awaitable[Any]], interception: Interception, record_call: Middleware
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(send_request)
    async def _request(session: Any, *arguments: Any, **options: Any) -> Any:
        recorded_options = _add_middleware(session, options, record_call, interception)
        return await send_request(session, *arguments, **recorded_options)

    return _request


def _add_middleware(
    session: Any, options: dict[str, Any], record_call: Middleware, interception: Interception
) -> dict[str, Any]:
    try:
        given_middlewares = options.get("middlewares")
        middlewares = session._middlewares if given_middlewares is None else given_middlewares
        return {**options, "middlewares": (*middlewares, record_call)}
    except Exception:
        interception.report_failure("record a call made through aiohttp")
        return options


def _build_recording_middleware(
    library: ModuleType, interception: Interception, reader_class: type
) -> Middleware:
    async def record_call(request: Any, send_request: Callable[[Any], Awaitable[Any]]) -> Any:
        open_call = await _begin_call(interception, request, library)
        try:
            response = await send_request(request)
        except BaseException as error:
            end_without_response(interception, open_call, error)
            raise
        _record_response(interception, open_call, response, reader_class)
        return response

    return record_call


async def _begin_call(
    interception: Interception, request: Any, library: ModuleType
) -> OpenCall | None:
    try:
        call = await _translate_request(request, library)
    except Exception:
        interception.report_failure("read an outbound request")
        return None
    return interception.begin(call)


async def _translate_request(request: Any, library: ModuleType) -> OutboundCall:
    payload = request.body
    is_held_in_memory = isinstance(payload, library.BytesPayload) and not request.compress
    in_memory_body = bytes(await payload.as_bytes()) if is_held_in_memory else b""
    return OutboundCall(
        method=str(request.method),
        url=str(request.url),
        headers=tuple((str(name), str(value)) for name, value in request.headers.items()),
        body=in_memory_body or None,
        has_streamed_body=bool(payload) and not is_held_in_memory,
    )


def _record_response(
    interception: Interception, open_call: OpenCall | None, response: Any, reader_class: type
) -> None:
    if open_call is None:
        return
    try:
        content = response.content
        has_body_to_read = int(response.status) != SWITCHING_PROTOCOLS and not content.at_eof()
        translated = OutboundResponse(
            status=int(response.status),
            headers=_decode_headers(response.raw_headers),
            is_body_decoded=has_body_to_read and content.total_compressed_bytes is not None,
        )
        recording = ResponseRecording(interception, open_call, translated)
        if has_body_to_read:
            response.content = reader_class(content, recording)
        else:
            recording.end(was_read_to_end=True, error=None)
    except Exception:
        interception.report_failure("record an outbound response")


def _decode_headers(raw_headers: Any) -> Headers:
    return tuple((name.decode("latin-1"), value.decode("latin-1")) for name, value in raw_headers)


def _build_recording_reader(library: ModuleType) -> type | None:
    members: dict[str, Any] = {
        "__slots__": ("inner_reader", "recording"),
        "__init__": _hold_reader,
        "__repr__": _describe_reader,
        "set_exception": _end_and_set_exception,
        "readchunk": _read_http_chunk,
        "read_nowait": _read_without_waiting,
        **{name: _forward_method(name) for name in FORWARDED_METHODS},
        **{name: _forward_property(name) for name in FORWARDED_PROPERTIES},
        **{name: _copy_read(name) for name in READS_OF_BYTES},
    }
    public_names = {name for name in vars(library.StreamReader) if not name.startswith("_")}
    if not public_names <= members.keys() | INHERITED_ITERATORS:
        return None
    return type("RecordingStreamReader", (library.StreamReader,), members)


def _hold_reader(reader: Any, inner_reader: Any, recording: ResponseRecording) -> None:
    reader.inner_reader = inner_reader
    reader.recording = recording


def _describe_reader(reader: Any) -> str:
    return repr(reader.inner_reader)


def _forward_method(name: str) -> Callable[..., Any]:
    def forward(reader: Any, *arguments: Any, **options: Any) -> Any:
        return getattr(reader.inner_reader, name)(*arguments, **options)

    return forward


def _forward_property(name: str) -> property:
    return property(lambda reader: getattr(reader.inner_reader, name))


def _copy_read(name: str) -> Callable[..., Awaitable[bytes]]:
    async def read(reader: Any, *arguments: Any, **options: Any) -> bytes:
        try:
            content: bytes = await getattr(reader.inner_reader, name)(*arguments, **options)
        except BaseException as error:
            reader.recording.end(was_read_to_end=False, error=error)
            raise
        _record_read(reader, content)
        return content

    return read


async def _read_http_chunk(reader: Any) -> tuple[bytes, bool]:
    try:
        http_chunk: tuple[bytes, bool] = await reader.inner_reader.readchunk()
    except BaseException as error:
        reader.recording.end(was_read_to_end=False, error=error)
        raise
    _record_read(reader, http_chunk[0])
    return http_chunk


def _read_without_waiting(reader: Any, *arguments: Any, **options: Any) -> bytes:
    try:
        content: bytes = reader.inner_reader.read_nowait(*arguments, **options)
    except BaseException as error:
        reader.recording.end(was_read_to_end=False, error=error)
        raise
    _record_read(reader, content)
    return content


def _record_read(reader: Any, content: bytes) -> None:
    reader.recording.add(content)
    if reader.inner_reader.at_eof():
        reader.recording.end(was_read_to_end=True, error=None)


def _end_and_set_exception(reader: Any, *arguments: Any, **options: Any) -> None:
    try:
        reader.recording.end(was_read_to_end=reader.inner_reader.at_eof(), error=None)
    finally:
        reader.inner_reader.set_exception(*arguments, **options)
