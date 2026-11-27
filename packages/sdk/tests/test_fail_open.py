import os
import subprocess
import sys
from typing import Any

import pytest

THREE_MEGABYTES = 3 * 1024 * 1024
APP = """
import irimi

{setup}

@irimi.function
def look_up_charge(ticket_id):
    return "ch_123"

@irimi.workflow
def handle_ticket(ticket_id):
    return look_up_charge(ticket_id)

print(handle_ticket("T-1042"))
{teardown}
"""


def run_app(setup: str, teardown: str = "", **environment: str) -> str:
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("IRIMI_")}
    completed = subprocess.run(
        [sys.executable, "-c", APP.format(setup=setup, teardown=teardown)],
        env={**inherited, **environment},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def test_importing_irimi_does_not_load_the_modules_only_sending_needs() -> None:
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, irimi; print(sorted(sys.modules))"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout

    assert "'urllib.request'" not in loaded
    assert "'importlib.metadata'" not in loaded


def test_events_still_queued_at_exit_are_sent(fake_store: Any) -> None:
    output = run_app("irimi.init()", IRIMI_STORE_URL=fake_store.url, IRIMI_ENVIRONMENT="staging")

    events = fake_store.take_events()

    assert output == "ch_123\n"
    assert [event["type"] for event in events] == ["start", "start", "end", "end"]
    assert events[0]["environment"] == "staging"


@pytest.mark.parametrize("setup", ["", "irimi.init()"])
def test_without_a_store_the_app_runs_as_before(setup: str) -> None:
    assert run_app(setup, teardown="print(irimi.flush())") == "ch_123\nTrue\n"


@pytest.mark.parametrize(
    "answer", [b'{"refused": [{"index": 0, "reason": "events[0].id: missing"}]}', b"<html>"]
)
def test_events_the_store_did_not_keep_count_as_not_sent(fake_store: Any, answer: bytes) -> None:
    fake_store.answer = answer

    output = run_app(f"irimi.init(store_url={fake_store.url!r})", teardown="print(irimi.flush())")

    assert output == "ch_123\nFalse\n"


CALL_THAT_CANNOT_BE_RECORDED = """
class Unprintable(Exception):
    def __str__(self):
        raise RuntimeError

@irimi.workflow
def fail():
    raise Unprintable()

try:
    fail()
except Unprintable:
    print("raised unchanged")
print(irimi.flush())
"""


def test_a_call_that_could_not_be_recorded_counts_as_not_sent(fake_store: Any) -> None:
    output = run_app(
        f"irimi.init(store_url={fake_store.url!r})", teardown=CALL_THAT_CANNOT_BE_RECORDED
    )

    assert output == "ch_123\nraised unchanged\nFalse\n"


A_RUN_TOO_LARGE_TO_WAIT = """
@irimi.workflow
def measure(text):
    return len(text)

measure("x" * 70_000_000)
measure("x")
print(irimi.flush())
"""


def test_events_beyond_the_memory_limit_are_dropped_and_counted(fake_store: Any) -> None:
    output = run_app(f"irimi.init(store_url={fake_store.url!r})", teardown=A_RUN_TOO_LARGE_TO_WAIT)

    starts = [event for event in fake_store.take_events() if event.get("name") == "measure"]

    assert output == "ch_123\nFalse\n"
    assert [start["input"]["args"] for start in starts] == [["x"]]


SIX_LARGE_ANSWERS = """
import os
import httpx

@irimi.workflow
def download_exports():
    with httpx.Client() as client:
        for _ in range(6):
            client.get(os.environ["UPSTREAM_URL"] + "/export")

download_exports()
print(irimi.flush())
"""


def test_large_calls_are_sent_in_batches_no_larger_than_the_limit(
    fake_store: Any, upstream: Any
) -> None:
    upstream.set_answer("/export", chunks=[b"x" * THREE_MEGABYTES])

    output = run_app(
        f"irimi.init(store_url={fake_store.url!r})",
        teardown=SIX_LARGE_ANSWERS,
        UPSTREAM_URL=upstream.url,
    )

    bodies_per_batch = [
        sum("body" in event.get("response", {}) for event in batch["events"])
        for batch in fake_store.batches
    ]

    assert output.endswith("True\n")
    assert len(fake_store.batches) >= 2
    assert sum(bodies_per_batch) == 6
    assert max(bodies_per_batch) <= 3


def test_an_unreachable_store_never_affects_the_app(unused_url: str) -> None:
    output = run_app(f"irimi.init(store_url={unused_url!r})", teardown="print(irimi.flush())")

    assert output == "ch_123\nFalse\n"
