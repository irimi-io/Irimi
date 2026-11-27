import time
from pathlib import Path

import uvicorn

from irimi_store.app import create_app
from irimi_store.sqlite_storage import SqliteStorageEngine

LOCAL_HOST = "127.0.0.1"


def serve_local_store(database_path: Path, port: int) -> None:
    storage = SqliteStorageEngine(database_path, time.time_ns)
    uvicorn.run(create_app(storage), host=LOCAL_HOST, port=port, log_level="warning")
