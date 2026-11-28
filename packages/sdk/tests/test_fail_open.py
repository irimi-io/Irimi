import os
import subprocess
import sys
from typing import Any

import pytest

THREE_MEGABYTES = 3 * 1024 * 1024
FOUR_MEGABYTES = 4 * 1024 * 1024
BATCH_SIZE_LIMIT = 8 * 1024 * 1024
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


OUTBOUND_APP = """
import os
import httpx2
import requests
import irimi

client = httpx2.Client()
session = requests.Session()
{setup}
import httpx

@irimi.workflow
def look_up_both():
    print(client.get(os.environ["UPSTREAM_URL"] + "/a").text)
    print(httpx.get(os.environ["UPSTREAM_URL"] + "/b").text)
    print(session.get(os.environ["UPSTREAM_URL"] + "/c").text)

look_up_both()
print(irimi.flush())
"""

AIOHTTP_APP = """
import asyncio
import os
import aiohttp
import irimi

@irimi.workflow
async def look_up_both(session):
    for path in ("/a", "/b", "/c"):
        async with session.get(os.environ["UPSTREAM_URL"] + path) as answer:
            print(await answer.text())

async def main():
    async with aiohttp.ClientSession() as session:
        {setup}
        await look_up_both(session)

asyncio.run(main())
print(irimi.flush())
"""

LARGE_UPLOAD_APP = """
import os
import tracemalloc
import urllib3
import irimi

irimi.init(store_url=os.environ["STORE_URL"])
report = bytearray(b"x") * (48 * 1024 * 1024)

def upload_report():
    return urllib3.request("POST", os.environ["UPSTREAM_URL"] + "/reports", body=report).status

tracemalloc.start()
print({upload}())
print(tracemalloc.get_traced_memory()[1] < len(report) // 2)
print(irimi.flush())
"""

UNKNOWN_AIOHTTP_APP = """
import asyncio
import os
import aiohttp
import irimi

aiohttp.StreamReader.read_into = lambda reader, buffer: 0
print(irimi.init(store_url=os.environ["STORE_URL"]).unpatched)

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.get(os.environ["UPSTREAM_URL"] + "/a") as answer:
            print(await answer.text())

asyncio.run(main())
"""


CALLS_RECORDED_INSIDE_EVERY_STEP_OF_IRIMI = """
import os
import sys
import threading
import urllib3
import irimi

irimi_directory = os.path.dirname(irimi.__file__)
steps_interrupted = set()

@irimi.function
def note():
    return None

def record_a_call_inside_each_step_of_irimi(frame, event, argument):
    if not frame.f_code.co_filename.startswith(irimi_directory):
        return None
    frame.f_trace_opcodes = True
    step = (frame.f_code, frame.f_lasti)
    if event == "opcode" and step not in steps_interrupted:
        steps_interrupted.add(step)
        note()
    return record_a_call_inside_each_step_of_irimi

@irimi.workflow
def handle_ticket():
    note()
    return urllib3.request("GET", os.environ["UPSTREAM_URL"] + "/orders").status

irimi.init(store_url=os.environ["STORE_URL"])
threading.settrace(record_a_call_inside_each_step_of_irimi)
sys.settrace(record_a_call_inside_each_step_of_irimi)
print(handle_ticket())
print(irimi.flush())
sys.settrace(None)
print(irimi.flush())
"""


def run_app(setup: str, teardown: str = "", **environment: str) -> str:
    return run_script(APP.format(setup=setup, teardown=teardown), **environment)


def run_script(script: str, **environment: str) -> str:
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("IRIMI_")}
    completed = subprocess.run(
        [sys.executable, "-c", script],
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


OBJECT_THAT_CANNOT_BE_READ = """
import logging
import sys
from dataclasses import dataclass, field

logging.basicConfig(stream=sys.stdout)

@dataclass
class Session:
    user: str
    token: str = field(init=False)

@irimi.workflow
def keep(session, note):
    return session

print(keep(Session("ada"), "kept").user)
print(irimi.flush())
"""


def test_an_object_whose_fields_cannot_be_read_is_recorded_by_its_type_and_still_sent(
    fake_store: Any,
) -> None:
    output = run_app(
        f"irimi.init(store_url={fake_store.url!r})", teardown=OBJECT_THAT_CANNOT_BE_READ
    )

    session = {"$irimi": "unserialisable", "type": "__main__.Session"}
    *_, run_start, run_end = fake_store.take_events()

    assert output == "ch_123\nada\nTrue\n"
    assert run_start["input"]["args"] == [session, "kept"]
    assert run_start["flags"] == ["not_replayable"]
    assert (run_end["span_id"], run_end["output"]) == (run_start["span_id"], session)
    assert run_end["flags"] == ["unserialisable_output"]


TWO_RUNS_TOO_LARGE_TO_WAIT_TOGETHER = """
import os
import urllib.request

@irimi.workflow
def measure(text):
    return len(text)

text = "x" * 40_000_000
measure(text)
measure(text)
urllib.request.urlopen(urllib.request.Request(os.environ["RELEASE_URL"], b"", method="POST"))
print(irimi.flush())
"""


def test_events_beyond_the_memory_limit_are_dropped_and_counted(fake_store: Any) -> None:
    fake_store.hold_answers()

    output = run_app(
        f"irimi.init(store_url={fake_store.url!r})",
        teardown=TWO_RUNS_TOO_LARGE_TO_WAIT_TOGETHER,
        RELEASE_URL=fake_store.release_url,
    )

    starts = [event for event in fake_store.take_events() if event.get("name") == "measure"]

    assert output == "ch_123\nFalse\n"
    assert len(starts) == 1


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

    body_sizes_per_batch = [
        [
            len(event["response"]["body"]["data"])
            for event in batch["events"]
            if "body" in event.get("response", {})
        ]
        for batch in fake_store.batches
    ]

    assert output.endswith("True\n")
    assert len(fake_store.batches) >= 2
    assert sum(map(len, body_sizes_per_batch)) == 6
    for body_sizes in body_sizes_per_batch:
        assert sum(body_sizes) <= BATCH_SIZE_LIMIT or len(body_sizes) == 1


def test_a_call_recorded_while_irimi_is_recording_another_neither_blocks_nor_is_miscounted(
    fake_store: Any, upstream: Any
) -> None:
    output = run_script(
        CALLS_RECORDED_INSIDE_EVERY_STEP_OF_IRIMI,
        STORE_URL=fake_store.url,
        UPSTREAM_URL=upstream.url,
    )

    events = fake_store.take_events()
    starts = [event for event in events if event["type"] == "start"]
    ends = [event["span_id"] for event in events if event["type"] == "end"]
    numbers_per_run: dict[str, list[int]] = {}
    for start in starts:
        numbers_per_run.setdefault(start["trace_id"], []).append(start["seq"])

    assert output == "200\nTrue\nTrue\n"
    assert len(starts) > 100
    assert sorted(ends) == sorted(start["span_id"] for start in starts)
    for numbers in numbers_per_run.values():
        assert sorted(numbers) == list(range(len(numbers)))


def test_an_unreachable_store_never_affects_the_app(unused_url: str) -> None:
    output = run_app(f"irimi.init(store_url={unused_url!r})", teardown="print(irimi.flush())")

    assert output == "ch_123\nFalse\n"


@pytest.mark.parametrize("app", [OUTBOUND_APP, AIOHTTP_APP])
def test_a_client_made_before_init_and_a_library_imported_after_it_are_both_recorded(
    fake_store: Any, upstream: Any, app: str
) -> None:
    output = run_script(
        app.format(setup=f"irimi.init(store_url={fake_store.url!r})"),
        UPSTREAM_URL=upstream.url,
    )

    call_starts = [event for event in fake_store.take_events() if event.get("kind") == "http"]

    assert output == '{"ok": true}\n' * 3 + "True\n"
    assert [start["name"] for start in call_starts] == [
        "GET 127.0.0.1/a",
        "GET 127.0.0.1/b",
        "GET 127.0.0.1/c",
    ]


@pytest.mark.parametrize(
    ("setup", "is_flushed"),
    [
        ("", True),
        ("irimi.init()", True),
        ("irimi.init(store_url=os.environ['UNUSED_URL'])", False),
    ],
)
@pytest.mark.parametrize("app", [OUTBOUND_APP, AIOHTTP_APP])
def test_outbound_calls_reach_the_app_unchanged_whatever_the_store_does(
    upstream: Any, unused_url: str, setup: str, is_flushed: bool, app: str
) -> None:
    output = run_script(app.format(setup=setup), UPSTREAM_URL=upstream.url, UNUSED_URL=unused_url)

    assert output == '{"ok": true}\n' * 3 + f"{is_flushed}\n"


def test_an_aiohttp_irimi_does_not_know_is_left_unpatched_and_works(
    fake_store: Any, upstream: Any
) -> None:
    output = run_script(UNKNOWN_AIOHTTP_APP, STORE_URL=fake_store.url, UPSTREAM_URL=upstream.url)

    assert "'aiohttp'" in output.splitlines()[0]
    assert output.splitlines()[1] == '{"ok": true}'


@pytest.mark.parametrize(
    ("upload", "flags", "recorded_size"),
    [
        ("upload_report", ["unattached"], 0),
        ("irimi.workflow(upload_report)", ["request_body_truncated"], FOUR_MEGABYTES),
    ],
    ids=["outside a workflow", "inside a workflow"],
)
def test_a_large_request_body_is_not_copied_beyond_what_is_recorded(
    fake_store: Any, upstream: Any, upload: str, flags: list[str], recorded_size: int
) -> None:
    output = run_script(
        LARGE_UPLOAD_APP.format(upload=upload), STORE_URL=fake_store.url, UPSTREAM_URL=upstream.url
    )

    (call_start,) = [event for event in fake_store.take_events() if event.get("kind") == "http"]

    assert output == "200\nTrue\nTrue\n"
    assert call_start["flags"] == flags
    assert len(call_start["request"].get("body", {}).get("data", "")) == recorded_size
