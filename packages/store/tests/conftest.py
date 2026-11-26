import socket
import threading
from collections.abc import Iterator

import pytest
import uvicorn
from irimi_store.app import create_app
from irimi_store.storage import StorageEngine


@pytest.fixture
def store_url(storage: StorageEngine) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(storage), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    while not server.started:
        thread.join(timeout=0.01)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
