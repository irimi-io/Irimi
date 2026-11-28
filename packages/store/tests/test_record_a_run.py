import os
import subprocess
import sys
from typing import Any

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


def test_a_run_recorded_in_an_app_arrives_in_the_store(store: Any) -> None:
    subprocess.run(
        [sys.executable, "-c", APP],
        env={**os.environ, "IRIMI_STORE_URL": store.url, "IRIMI_ENVIRONMENT": "production"},
        timeout=60,
        check=True,
    )

    (run,) = store.get("/api/v1/runs")[1]["runs"]
    run_start, step_start, step_end, run_end = store.get(f"/v1/runs/{run['id']}/events")[1][
        "events"
    ]

    assert (run["name"], run["environment"], run["conditions"]) == (
        "handle_ticket",
        "production",
        [],
    )
    assert (run_start["type"], run_start["kind"], run_start["name"]) == (
        "start",
        "workflow",
        "handle_ticket",
    )
    assert run_start["input"] == {"args": [{"id": "T-1042", "text": "Charged twice"}], "kwargs": {}}
    assert (step_start["kind"], step_start["name"]) == ("function", "record_refund")
    assert (step_start["parent_span_id"], step_start["write"]) == (run_start["span_id"], True)
    assert (step_end["type"], step_end["output"]) == ("end", {"row_id": 9917})
    assert (run_end["output"], run_end["events_produced"]) == ("Refund issued", 4)


APP_GIVEN_OBJECTS = """
from dataclasses import dataclass

import irimi
from pydantic import BaseModel

irimi.init()

@dataclass(frozen=True)
class Customer:
    id: str

class Ticket(BaseModel):
    id: str
    text: str

@irimi.workflow
def handle_ticket(ticket, customer):
    return customer

handle_ticket(Ticket(id="T-1042", text="Charged twice"), customer=Customer(id="C-7"))
"""


def test_a_run_given_objects_arrives_with_their_fields_and_is_replayable(store: Any) -> None:
    subprocess.run(
        [sys.executable, "-c", APP_GIVEN_OBJECTS],
        env={**os.environ, "IRIMI_STORE_URL": store.url},
        timeout=60,
        check=True,
    )

    (run,) = store.get("/api/v1/runs")[1]["runs"]
    call = store.get(f"/api/v1/runs/{run['id']}/calls/{run['span_id']}")[1]["call"]
    customer = {"$irimi": "object", "type": "__main__.Customer", "fields": {"id": "C-7"}}

    assert (run["flags"], run["conditions"]) == ([], [])
    assert call["input"] == {
        "args": [
            {
                "$irimi": "object",
                "type": "__main__.Ticket",
                "fields": {"id": "T-1042", "text": "Charged twice"},
            }
        ],
        "kwargs": {"customer": customer},
    }
    assert call["output"] == customer
