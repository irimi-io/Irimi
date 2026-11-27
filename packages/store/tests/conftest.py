import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from irimi_store.app import create_app
from irimi_store.sqlite_storage import SqliteStorageEngine
from irimi_store.storage import StorageEngine


class StoreClient:
    def __init__(self, url: str) -> None:
        self.url = url

    def post(self, path: str, document: Any) -> tuple[int, Any]:
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        return _send(urllib.request.Request(self.url + path, data=body, method="POST"))

    def get(self, path: str) -> tuple[int, Any]:
        return _send(urllib.request.Request(self.url + path))


def _send(request: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def storage(tmp_path: Path) -> StorageEngine:
    return SqliteStorageEngine(tmp_path / "store.db", time.time_ns)


@contextmanager
def serve(storage: StorageEngine) -> Iterator[StoreClient]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(storage), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    while not server.started:
        thread.join(timeout=0.01)
    try:
        yield StoreClient(f"http://127.0.0.1:{listener.getsockname()[1]}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def store(storage: StorageEngine) -> Iterator[StoreClient]:
    with serve(storage) as client:
        yield client


@pytest.fixture
def serve_store() -> Callable[[StorageEngine], AbstractContextManager[StoreClient]]:
    return serve
