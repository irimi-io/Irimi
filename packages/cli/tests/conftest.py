import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

IRIMI = Path(sys.executable).parent / "irimi"
SECONDS_TO_WAIT_FOR_THE_STORE = 30.0


def find_free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port: int = listener.getsockname()[1]
    return port


def wait_until_the_store_answers(store_url: str) -> None:
    give_up_at = time.monotonic() + SECONDS_TO_WAIT_FOR_THE_STORE
    while True:
        try:
            urllib.request.urlopen(f"{store_url}/api/v1/runs", timeout=1).close()
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() > give_up_at:
                raise
            time.sleep(0.05)
        else:
            return


@pytest.fixture
def local_store_url(tmp_path: Path) -> Iterator[str]:
    store = subprocess.Popen(
        [IRIMI, "store", "--port", str(find_free_port()), "--database", tmp_path / "store.db"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert store.stdout is not None
        store_url = store.stdout.readline().strip()
        wait_until_the_store_answers(store_url)
        yield store_url
    finally:
        store.terminate()
        store.wait(timeout=10)


@pytest.fixture
def run_irimi() -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(store_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [IRIMI, *arguments],
            env={**os.environ, "IRIMI_STORE_URL": store_url},
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run
