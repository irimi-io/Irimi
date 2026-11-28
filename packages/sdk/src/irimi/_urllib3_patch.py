from __future__ import annotations

import functools
import inspect
import threading
from collections.abc import Callable, Iterator
from types import GeneratorType, ModuleType
from typing import Any

from irimi._bodies import BODY_LIMIT
from irimi._interception import Interception, OpenCall
from irimi._outbound_calls import CallOutcome, Headers, OutboundCall, OutboundResponse
from irimi._response_recording import ResponseRecording

FOLLOWED_RESPONSE_ATTRIBUTE = "_irimi_followed_response"
LEADING_REQUEST_PARAMETERS = ["self", "conn", "method", "url"]
RECORDED_REQUEST_PARAMETERS = ("body", "headers")
RESPONSE_METHODS = ("_raw_read", "_handle_chunk", "read_chunked", "close", "isclosed")
DEFAULT_PORTS = {"http": 80, "https": 443}
LONGEST_BODY_TAKEN = BODY_LIMIT + 1


def patch_urllib3(library: ModuleType, interception: Interception) -> bool:
    pool_class: Any = getattr(library, "HTTPConnectionPool", None)
    response_class: Any = getattr(library, "HTTPResponse", None)
    if not _sends_one_request(getattr(pool_class, "_make_request", None)):
        return False
    if not _reads_a_body_as_expected(response_class):
        return False
    following = _ResponseFollowing(interception)
    make_request = _wrap_make_request(library, interception, following)
    response_methods = {
        "_raw_read": _wrap_raw_read(response_class._raw_read, following),
        "_handle_chunk": _wrap_handle_chunk(response_class._handle_chunk, following),
        "read_chunked": _wrap_read_chunked(response_class.read_chunked, following),
        "close": _wrap_close(response_class.close, following),
    }
    pool_class._make_request = make_request
    for name, method in response_methods.items():
        setattr(response_class, name, method)
    return True


def _sends_one_request(make_request: object) -> bool:
    if not callable(make_request):
        return False
    parameters = list(inspect.signature(make_request).parameters)
    return parameters[:4] == LEADING_REQUEST_PARAMETERS and set(parameters) >= set(
        RECORDED_REQUEST_PARAMETERS
    )


def _reads_a_body_as_expected(response_class: object) -> bool:
    if not all(callable(getattr(response_class, name, None)) for name in RESPONSE_METHODS):
        return False
    return inspect.isgeneratorfunction(getattr(response_class, "read_chunked", None))


class _Attempt:
    def __init__(self, open_call: OpenCall) -> None:
        self.open_call = open_call
        self.recording: ResponseRecording | None = None


class _FollowedResponse:
    def __init__(self, recording: ResponseRecording) -> None:
        self.recording = recording
        self.chunked_reader: GeneratorType[bytes, None, None] | None = None


class _ResponseFollowing:
    def __init__(self, interception: Interception) -> None:
        self._interception = interception
        self._waiting = threading.local()

    def swap_waiting_attempt(self, attempt: _Attempt | None) -> _Attempt | None:
        previous_attempt: _Attempt | None = getattr(self._waiting, "attempt", None)
        self._waiting.attempt = attempt
        return previous_attempt

    def find(self, response: Any) -> _FollowedResponse | None:
        followed: _FollowedResponse | None = vars(response).get(FOLLOWED_RESPONSE_ATTRIBUTE)
        if followed is not None:
            return followed
        attempt: _Attempt | None = getattr(self._waiting, "attempt", None)
        if attempt is None:
            return None
        return self._claim(attempt, response)

    def follow_returned_response(
        self, attempt: _Attempt, response: Any, response_class: type
    ) -> None:
        can_be_followed = isinstance(response, response_class)
        if not can_be_followed:
            self._end_without_a_body(attempt, response)
            return
        followed = vars(response).get(FOLLOWED_RESPONSE_ATTRIBUTE) or self._claim(attempt, response)
        if followed is None:
            return
        try:
            if response.isclosed() or response.length_remaining == 0:
                followed.recording.end(was_read_to_end=True, error=None)
        except Exception:
            self._interception.report_failure("record an outbound response")

    def end_failed_attempt(self, attempt: _Attempt, error: BaseException) -> None:
        if attempt.recording is not None:
            attempt.recording.end(was_read_to_end=False, error=error)
            return
        outcome = CallOutcome(response=None, body=None, was_read_to_end=False, error=error)
        self._interception.end(attempt.open_call, outcome)

    def record_read(self, followed: _FollowedResponse, response: Any, content: bytes) -> None:
        try:
            if content:
                followed.recording.add(content)
            if response.isclosed():
                followed.recording.end(was_read_to_end=True, error=None)
        except Exception:
            self._interception.report_failure("record an outbound response")

    def end_closed_response(self, response: Any) -> None:
        try:
            followed: _FollowedResponse | None = vars(response).get(FOLLOWED_RESPONSE_ATTRIBUTE)
            if followed is None or _is_reading_chunks(followed):
                return
            followed.recording.end(was_read_to_end=False, error=None)
        except Exception:
            self._interception.report_failure("record an outbound response")

    def _claim(self, attempt: _Attempt, response: Any) -> _FollowedResponse | None:
        if attempt.recording is not None:
            return None
        try:
            recording = ResponseRecording(
                self._interception, attempt.open_call, _translate_response(response)
            )
            followed = _FollowedResponse(recording)
            vars(response)[FOLLOWED_RESPONSE_ATTRIBUTE] = followed
            attempt.recording = recording
        except Exception:
            self._interception.report_failure("record an outbound response")
            return None
        return followed

    def _end_without_a_body(self, attempt: _Attempt, response: Any) -> None:
        try:
            outcome = CallOutcome(
                response=_translate_response(response),
                body=None,
                was_read_to_end=False,
                error=None,
            )
        except Exception:
            self._interception.report_failure("record an outbound response")
            return
        self._interception.end(attempt.open_call, outcome)


def _is_reading_chunks(followed: _FollowedResponse) -> bool:
    return followed.chunked_reader is not None and followed.chunked_reader.gi_running


def _wrap_make_request(
    library: ModuleType, interception: Interception, following: _ResponseFollowing
) -> Callable[..., Any]:
    send_request = library.HTTPConnectionPool._make_request
    response_class = library.HTTPResponse
    skipped_header_value = library.util.SKIP_HEADER
    later_parameters = list(inspect.signature(send_request).parameters)[4:]
    body_position, headers_position = map(later_parameters.index, RECORDED_REQUEST_PARAMETERS)

    @functools.wraps(send_request)
    def _make_request(
        pool: Any, conn: Any, method: Any, url: Any, *arguments: Any, **options: Any
    ) -> Any:
        open_call = _begin_call(
            interception,
            pool,
            method,
            url,
            _find_argument("body", body_position, arguments, options),
            _find_argument("headers", headers_position, arguments, options),
            skipped_header_value,
        )
        if open_call is None:
            return send_request(pool, conn, method, url, *arguments, **options)
        attempt = _Attempt(open_call)
        outer_attempt = following.swap_waiting_attempt(attempt)
        try:
            response = send_request(pool, conn, method, url, *arguments, **options)
        except BaseException as error:
            following.end_failed_attempt(attempt, error)
            raise
        finally:
            following.swap_waiting_attempt(outer_attempt)
        following.follow_returned_response(attempt, response, response_class)
        return response

    return _make_request


def _wrap_raw_read(read: Callable[..., Any], following: _ResponseFollowing) -> Callable[..., Any]:
    @functools.wraps(read)
    def _raw_read(response: Any, *arguments: Any, **options: Any) -> Any:
        followed = following.find(response)
        if followed is None:
            return read(response, *arguments, **options)
        try:
            content = read(response, *arguments, **options)
        except BaseException as error:
            followed.recording.end(was_read_to_end=False, error=error)
            raise
        following.record_read(followed, response, content)
        return content

    return _raw_read


def _wrap_handle_chunk(
    read_chunk: Callable[..., Any], following: _ResponseFollowing
) -> Callable[..., Any]:
    @functools.wraps(read_chunk)
    def _handle_chunk(response: Any, *arguments: Any, **options: Any) -> Any:
        followed = following.find(response)
        chunk = read_chunk(response, *arguments, **options)
        if followed is not None and chunk:
            followed.recording.add(chunk)
        return chunk

    return _handle_chunk


def _wrap_read_chunked(
    read_chunks: Callable[..., GeneratorType[bytes, None, None]], following: _ResponseFollowing
) -> Callable[..., Iterator[bytes]]:
    @functools.wraps(read_chunks)
    def read_chunked(response: Any, *arguments: Any, **options: Any) -> Iterator[bytes]:
        followed = following.find(response)
        reader = read_chunks(response, *arguments, **options)
        if followed is None:
            yield from reader
            return
        followed.chunked_reader = reader
        try:
            yield from reader
        except GeneratorExit:
            followed.recording.end(was_read_to_end=False, error=None)
            raise
        except BaseException as error:
            followed.recording.end(was_read_to_end=False, error=error)
            raise
        following.record_read(followed, response, b"")

    return read_chunked


def _wrap_close(close: Callable[..., Any], following: _ResponseFollowing) -> Callable[..., Any]:
    @functools.wraps(close)
    def close_response(response: Any, *arguments: Any, **options: Any) -> None:
        try:
            following.end_closed_response(response)
        finally:
            close(response, *arguments, **options)

    return close_response


def _begin_call(
    interception: Interception,
    pool: Any,
    method: Any,
    target: Any,
    body: Any,
    headers: Any,
    skipped_header_value: object,
) -> OpenCall | None:
    try:
        in_memory_body = _find_in_memory_body(body) if interception.is_inside_workflow() else b""
        call = OutboundCall(
            method=str(method),
            url=_build_url(str(pool.scheme), str(pool.host), pool.port, str(target)),
            headers=_translate_request_headers(headers, skipped_header_value),
            body=in_memory_body or None,
            has_streamed_body=in_memory_body is None,
        )
    except Exception:
        interception.report_failure("read an outbound request")
        return None
    return interception.begin(call)


def _build_url(scheme: str, host: str, port: int | None, target: str) -> str:
    if not target.startswith("/"):
        return target
    bracketed_host = f"[{host}]" if ":" in host else host
    shown_port = "" if port is None or port == DEFAULT_PORTS.get(scheme) else f":{port}"
    return f"{scheme}://{bracketed_host}{shown_port}{target}"


def _find_argument(
    name: str, position: int, arguments: tuple[Any, ...], options: dict[str, Any]
) -> Any:
    if name in options:
        return options[name]
    return arguments[position] if position < len(arguments) else None


def _find_in_memory_body(body: Any) -> bytes | None:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body[:LONGEST_BODY_TAKEN].encode()
    if hasattr(body, "read"):
        return None
    try:
        return bytes(memoryview(body).cast("B")[:LONGEST_BODY_TAKEN])
    except TypeError:
        return None


def _translate_request_headers(headers: Any, skipped_header_value: object) -> Headers:
    if headers is None:
        return ()
    return tuple(
        (_decode_header_part(name), _decode_header_part(value))
        for name, value in headers.items()
        if value != skipped_header_value
    )


def _translate_response(response: Any) -> OutboundResponse:
    return OutboundResponse(
        status=int(response.status),
        headers=tuple(
            (_decode_header_part(name), _decode_header_part(value))
            for name, value in response.headers.items()
        ),
    )


def _decode_header_part(part: Any) -> str:
    return part.decode("latin-1") if isinstance(part, bytes) else str(part)
