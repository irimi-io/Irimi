import os
import socket
import subprocess
import sys
from typing import Any

import pytest

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


def unused_store_url() -> str:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{unused.getsockname()[1]}"


def test_events_still_queued_at_exit_are_sent(fake_store: Any) -> None:
    output = run_app("irimi.init()", IRIMI_STORE_URL=fake_store.url, IRIMI_ENVIRONMENT="staging")

    events = fake_store.take_events()

    assert output == "ch_123\n"
    assert [event["type"] for event in events] == ["start", "start", "end", "end"]
    assert events[0]["environment"] == "staging"


@pytest.mark.parametrize("setup", ["", "irimi.init()"])
def test_without_a_store_the_app_runs_as_before(setup: str) -> None:
    assert run_app(setup, teardown="print(irimi.flush())") == "ch_123\nTrue\n"


def test_an_unreachable_store_never_affects_the_app() -> None:
    output = run_app(
        f"irimi.init(store_url={unused_store_url()!r})", teardown="print(irimi.flush())"
    )

    assert output == "ch_123\nFalse\n"
