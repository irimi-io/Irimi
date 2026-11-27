import socket
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from irimi_store.app import create_app
from irimi_store.sqlite_schema import NewerSchemaError
from irimi_store.sqlite_storage import SqliteStorageEngine

LOCAL_HOST = "127.0.0.1"


class LocalStoreError(Exception):
    pass


def serve_local_store(database_path: Path, port: int, announce: Callable[[str], None]) -> None:
    listener = _listen_on(port)
    try:
        storage = SqliteStorageEngine(database_path, time.time_ns)
    except (sqlite3.Error, OSError, NewerSchemaError) as error:
        listener.close()
        raise LocalStoreError(f"cannot open the database {database_path}: {error}") from error
    announce(f"http://{LOCAL_HOST}:{listener.getsockname()[1]}")
    server = uvicorn.Server(uvicorn.Config(create_app(storage), log_level="warning"))
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        return


def _listen_on(port: int) -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((LOCAL_HOST, port))
    except (OSError, OverflowError) as error:
        listener.close()
        raise LocalStoreError(f"cannot listen on port {port}: {error}") from error
    listener.listen()
    return listener
