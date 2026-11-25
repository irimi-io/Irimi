import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import irimi
import pytest


class FakeStore:
    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def take_events(self) -> list[dict[str, Any]]:
        events = [event for batch in self.batches for event in batch["events"]]
        self.batches.clear()
        return events

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        store = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                store.paths.append(self.path)
                store.batches.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler


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
