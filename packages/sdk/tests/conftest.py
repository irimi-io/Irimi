import importlib
import json
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import irimi
import pytest

SECONDS_BETWEEN_SHUTDOWN_CHECKS = 0.01
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
RELEASE_PATH = "/release-answers"


class FakeStore:
    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.answer = b'{"refused": []}'
        self._answers_released = threading.Event()
        self._answers_released.set()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def release_url(self) -> str:
        return f"{self.url}{RELEASE_PATH}"

    def take_events(self) -> list[dict[str, Any]]:
        events = [event for batch in self.batches for event in batch["events"]]
        self.batches.clear()
        return events

    def hold_answers(self) -> None:
        self._answers_released.clear()

    def close(self) -> None:
        self._answers_released.set()
        self._server.shutdown()
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        store = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                if self.path == RELEASE_PATH:
                    store._answers_released.set()
                    self._answer(b"")
                    return
                store.paths.append(self.path)
                store.batches.append(json.loads(self.rfile.read(length)))
                store._answers_released.wait()
                self._answer(store.answer)

            def _answer(self, content: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler


@dataclass
class UpstreamAnswer:
    status: int = 200
    headers: list[tuple[str, str]] = field(
        default_factory=lambda: [("Content-Type", "application/json")]
    )
    chunks: list[bytes] = field(default_factory=lambda: [b'{"ok": true}'])
    breaks_off: bool = False


class FakeUpstream:
    def __init__(self) -> None:
        self._answers: dict[str, UpstreamAnswer] = {}
        self.requests: list[tuple[str, str, bytes]] = []
        self.request_headers: list[dict[str, str]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        threading.Thread(
            target=self._server.serve_forever, args=(SECONDS_BETWEEN_SHUTDOWN_CHECKS,), daemon=True
        ).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def set_answer(self, path: str, **answer_fields: Any) -> None:
        self._answers[path] = UpstreamAnswer(**answer_fields)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self) -> None:
                path = urlsplit(self.path).path
                upstream.request_headers.append(dict(self.headers.items()))
                upstream.requests.append((self.command, path, self._read_body()))
                answer = upstream._answers.get(path, UpstreamAnswer())
                self.send_response(answer.status)
                for name, value in answer.headers:
                    self.send_header(name, value)
                if answer.breaks_off or len(answer.chunks) > 1:
                    self._send_chunked(answer)
                else:
                    self._send_whole(b"".join(answer.chunks))

            def _read_body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    return self._read_chunked_body()
                return self.rfile.read(int(self.headers.get("Content-Length", "0")))

            def _read_chunked_body(self) -> bytes:
                body = b""
                while size := int(self.rfile.readline().split(b";")[0], 16):
                    body += self.rfile.read(size)
                    self.rfile.readline()
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return body

            def _send_whole(self, content: bytes) -> None:
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def _send_chunked(self, answer: UpstreamAnswer) -> None:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    self._write_chunks(answer)
                except ConnectionError:
                    self.close_connection = True

            def _write_chunks(self, answer: UpstreamAnswer) -> None:
                for chunk in answer.chunks:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                if answer.breaks_off:
                    self.close_connection = True
                    return
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, format: str, *args: Any) -> None:
                return

        for method in HTTP_METHODS:
            setattr(Handler, f"do_{method}", Handler.answer)
        return Handler


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    server = FakeUpstream()
    yield server
    server.close()


@pytest.fixture(params=["httpx", "httpx2"])
def http_library(request: pytest.FixtureRequest) -> Any:
    return importlib.import_module(request.param)


@pytest.fixture
def unused_url() -> str:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{unused.getsockname()[1]}"


@pytest.fixture
def fake_store() -> Iterator[FakeStore]:
    store = FakeStore()
    yield store
    store.close()


@pytest.fixture(scope="session")
def recording_store() -> Iterator[FakeStore]:
    store = FakeStore()
    irimi.init(store_url=store.url, version="2026.11.3", environment="test")
    yield store
    store.close()


@pytest.fixture
def recorded_events(recording_store: FakeStore) -> Callable[[], list[dict[str, Any]]]:
    irimi.flush()
    recording_store.take_events()

    def flush_and_take_events() -> list[dict[str, Any]]:
        assert irimi.flush(timeout=5.0)
        return recording_store.take_events()

    return flush_and_take_events
