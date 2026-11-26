import os
import subprocess
import sys
from collections.abc import Sequence

import pytest
from irimi._wire import EndEvent, Event, StartEvent

APP = """
import irimi

irimi.init()

@irimi.function(write=True)
def record_refund(ticket_id):
    return {"row_id": 9917}

@irimi.workflow
def handle_ticket(ticket):
    record_refund(ticket["id"])
    return "Refund issued"

handle_ticket({"id": "T-1042", "text": "Charged twice"})
"""


class MemoryStorageEngine:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def add_events(self, project: str, events: Sequence[Event]) -> None:
        self.events.extend(events)

    def read_run_events(self, project: str, trace_id: str) -> tuple[Event, ...]:
        return tuple(event for event in self.events if event.trace_id == trace_id)


@pytest.fixture
def storage() -> MemoryStorageEngine:
    return MemoryStorageEngine()


def test_a_run_recorded_in_an_app_arrives_in_the_store(
    store_url: str, storage: MemoryStorageEngine
) -> None:
    subprocess.run(
        [sys.executable, "-c", APP],
        env={**os.environ, "IRIMI_STORE_URL": store_url, "IRIMI_ENVIRONMENT": "production"},
        timeout=60,
        check=True,
    )

    run_start, step_start, step_end, run_end = storage.events

    assert isinstance(run_start, StartEvent)
    assert isinstance(step_start, StartEvent)
    assert isinstance(step_end, EndEvent)
    assert isinstance(run_end, EndEvent)
    assert (run_start.kind, run_start.name) == ("workflow", "handle_ticket")
    assert run_start.fields["environment"] == "production"
    assert run_start.fields["input"] == {
        "args": [{"id": "T-1042", "text": "Charged twice"}],
        "kwargs": {},
    }
    assert (step_start.kind, step_start.name) == ("function", "record_refund")
    assert (step_start.parent_span_id, step_start.fields["write"]) == (run_start.span_id, True)
    assert step_end.fields["output"] == {"row_id": 9917}
    assert (run_end.fields["output"], run_end.fields["events_produced"]) == ("Refund issued", 4)
    assert storage.read_run_events("default", run_start.trace_id) == tuple(storage.events)
