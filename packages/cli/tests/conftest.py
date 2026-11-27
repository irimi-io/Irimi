import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

IRIMI = Path(sys.executable).parent / "irimi"


@pytest.fixture
def local_store_url(tmp_path: Path) -> Iterator[str]:
    store = subprocess.Popen(
        [IRIMI, "store", "--port", "0", "--database", tmp_path / "store.db"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert store.stdout is not None
    try:
        store_url = store.stdout.readline().strip()
        yield store_url
    finally:
        store.terminate()
        store.wait(timeout=10)
        store.stdout.close()


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
