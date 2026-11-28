import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import irimi
import pytest
from irimi._wire import parse_batch
from pydantic import BaseModel, ConfigDict

TakeEvents = Callable[[], list[dict[str, Any]]]


class Session:
    pass


@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    email: str


class Ticket(BaseModel):
    id: str
    tags: list[str]


class OpenTicket(Ticket):
    model_config = ConfigDict(extra="allow")
    urgency: int


@dataclass
class Refund:
    ticket: Ticket
    customers: list[Customer]
    amount: int
    reason: object = None


@dataclass
class Step:
    name: str
    next_step: object = None


@dataclass(init=False)
class TaggedEntries(dict[object, object]):
    tag: str = "urgent"

    def __init__(self, entries: dict[object, object]) -> None:
        super().__init__(entries)


@dataclass(init=False)
class TaggedItems(list[object]):
    tag: str = "urgent"

    def __init__(self, items: list[object]) -> None:
        super().__init__(items)


@dataclass(init=False)
class TaggedMembers(set[object]):
    tag: str = "urgent"

    def __init__(self, members: set[object]) -> None:
        super().__init__(members)


@dataclass
class TaggedText(str):
    tag: str


@dataclass
class TaggedNumber(int):
    tag: int


class ComparedByIdentity(type):
    def __eq__(cls, other: object) -> bool:
        return cls is other


class Connection(metaclass=ComparedByIdentity):
    pass


def repeat_a_step_forever() -> Step:
    step = Step("retry")
    step.next_step = step
    return step


def as_object(type_name: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"$irimi": "object", "type": f"{__name__}.{type_name}", "fields": fields}


ADA = Customer(id="C-7", email="ada@example.com")
RECORDED_ADA = as_object("Customer", {"id": "C-7", "email": "ada@example.com"})


@irimi.function(write=True, fake={"row_id": 0})
def record_refund(session: Session, row: dict[str, str]) -> dict[str, int]:
    return {"row_id": 9917}


@irimi.workflow
def handle_ticket(ticket: dict[str, str]) -> str:
    record_refund(Session(), {"ticket": ticket["id"]})
    return "Refund issued"


@irimi.function
async def look_up_charge(ticket_id: str) -> str:
    await asyncio.sleep(0)
    return "ch_123"


@irimi.workflow(name="tickets.triage")
async def triage_ticket(ticket_id: str) -> list[str]:
    return list(await asyncio.gather(look_up_charge(ticket_id), look_up_charge(ticket_id)))


@irimi.workflow
def fail_on_ticket(ticket_id: str) -> None:
    raise LookupError(f"no ticket {ticket_id}")


@irimi.workflow
def open_session(session: object) -> object:
    return session


@irimi.workflow
def hand_back(value: object) -> object:
    return value


def test_a_workflow_run_is_recorded_as_a_tree_of_start_and_end_events(
    recorded_events: TakeEvents, recording_store: Any
) -> None:
    assert handle_ticket({"id": "T-1042"}) == "Refund issued"

    run_start, step_start, step_end, run_end = recorded_events()

    assert parse_batch({"events": [run_start, step_start, step_end, run_end]}).refused == ()
    assert set(recording_store.paths) == {"/v1/events"}
    assert [event["type"] for event in (run_start, step_start, step_end, run_end)] == [
        "start",
        "start",
        "end",
        "end",
    ]
    assert len({event["trace_id"] for event in (run_start, step_start, step_end, run_end)}) == 1
    assert (run_start["kind"], run_start["name"], run_start["seq"]) == (
        "workflow",
        "handle_ticket",
        0,
    )
    assert run_start["parent_span_id"] is None
    assert run_start["input"] == {"args": [{"id": "T-1042"}], "kwargs": {}}
    assert run_start["mode"] == "normal"
    assert run_start["environment"] == "test"
    assert run_start["deployment"]["version"] == "2026.11.3"
    assert run_start["started_by"] == {
        "kind": "decorated_function",
        "import_path": f"{__name__}:handle_ticket",
    }
    assert (step_start["kind"], step_start["name"], step_start["seq"]) == (
        "function",
        "record_refund",
        1,
    )
    assert step_start["parent_span_id"] == run_start["span_id"]
    assert step_start["write"] is True
    assert step_start["input"]["args"] == [
        {"$irimi": "unserialisable", "type": f"{__name__}.Session"},
        {"ticket": "T-1042"},
    ]
    assert (step_end["span_id"], step_end["output"]) == (step_start["span_id"], {"row_id": 9917})
    assert (run_end["span_id"], run_end["status"]) == (run_start["span_id"], "ok")
    assert (run_end["output"], run_end["events_produced"]) == ("Refund issued", 4)


def test_an_async_workflow_keeps_concurrent_steps_under_their_run(
    recorded_events: TakeEvents,
) -> None:
    assert asyncio.run(triage_ticket("T-1042")) == ["ch_123", "ch_123"]

    events = recorded_events()
    run_start = events[0]
    step_starts = [event for event in events if event.get("kind") == "function"]

    assert run_start["name"] == "tickets.triage"
    assert sorted(step["seq"] for step in step_starts) == [1, 2]
    assert {step["parent_span_id"] for step in step_starts} == {run_start["span_id"]}
    assert events[-1]["events_produced"] == 6


def test_an_exception_is_recorded_and_raised_unchanged(recorded_events: TakeEvents) -> None:
    with pytest.raises(LookupError, match="no ticket T-7"):
        fail_on_ticket("T-7")

    _, run_end = recorded_events()

    assert run_end["status"] == "error"
    assert run_end["error"] == {"type": "builtins.LookupError", "message": "no ticket T-7"}


@pytest.mark.parametrize(
    ("unserialisable", "type_name"),
    [
        (Session(), f"{__name__}.Session"),
        (float("nan"), "builtins.float"),
        ("cut \ud83d short", "builtins.str"),
        ({"cut \ud83d short": 1}, "builtins.dict"),
        (Customer, "builtins.type"),
        (Connection(), f"{__name__}.Connection"),
        (repeat_a_step_forever(), f"{__name__}.Step"),
        (TaggedEntries({1: 2}), f"{__name__}.TaggedEntries"),
        (TaggedEntries({"amount": 2}), f"{__name__}.TaggedEntries"),
        (TaggedItems([1, 2]), f"{__name__}.TaggedItems"),
        (TaggedMembers({1, 2}), f"{__name__}.TaggedMembers"),
        (TaggedText("refund"), f"{__name__}.TaggedText"),
        (TaggedNumber(1250), f"{__name__}.TaggedNumber"),
    ],
)
def test_a_run_with_unserialisable_input_is_recorded_and_flagged(
    recorded_events: TakeEvents, unserialisable: object, type_name: str
) -> None:
    assert open_session(unserialisable) is unserialisable

    run_start, run_end = recorded_events()

    assert run_start["flags"] == ["not_replayable"]
    assert run_end["flags"] == ["unserialisable_output"]
    assert run_end["output"] == {"$irimi": "unserialisable", "type": type_name}


@pytest.mark.parametrize(
    ("value", "recorded"),
    [
        (ADA, RECORDED_ADA),
        (
            Ticket(id="T-1042", tags=["billing"]),
            as_object("Ticket", {"id": "T-1042", "tags": ["billing"]}),
        ),
        (
            Refund(
                ticket=OpenTicket.model_validate(
                    {"id": "T-1042", "tags": [], "urgency": 2, "note": "call back"}
                ),
                customers=[ADA],
                amount=1250,
            ),
            as_object(
                "Refund",
                {
                    "ticket": as_object(
                        "OpenTicket",
                        {"id": "T-1042", "tags": [], "urgency": 2, "note": "call back"},
                    ),
                    "customers": [RECORDED_ADA],
                    "amount": 1250,
                    "reason": None,
                },
            ),
        ),
        (
            OpenTicket.model_validate({"id": "T-1042", "tags": [], "urgency": 2, "$irimi": "kept"}),
            as_object(
                "OpenTicket",
                {
                    "$irimi": "literal",
                    "value": {"id": "T-1042", "tags": [], "urgency": 2, "$irimi": "kept"},
                },
            ),
        ),
    ],
)
def test_a_dataclass_or_pydantic_model_is_recorded_as_its_fields(
    recorded_events: TakeEvents, value: object, recorded: dict[str, Any]
) -> None:
    assert hand_back(value) is value

    run_start, run_end = recorded_events()

    assert parse_batch({"events": [run_start, run_end]}).refused == ()
    assert run_start["input"] == {"args": [recorded], "kwargs": {}}
    assert run_end["output"] == recorded
    assert "flags" not in run_start
    assert "flags" not in run_end


def test_an_unserialisable_field_is_marked_in_place_and_the_run_is_flagged(
    recorded_events: TakeEvents,
) -> None:
    hand_back(Refund(ticket=Ticket(id="T-1", tags=[]), customers=[], amount=5, reason=Session()))

    run_start, run_end = recorded_events()

    assert run_end["output"]["type"] == f"{__name__}.Refund"
    assert run_end["output"]["fields"]["amount"] == 5
    assert run_end["output"]["fields"]["reason"] == {
        "$irimi": "unserialisable",
        "type": f"{__name__}.Session",
    }
    assert run_start["flags"] == ["not_replayable"]
    assert run_end["flags"] == ["unserialisable_output"]


def test_a_labeled_function_outside_a_workflow_is_recorded_as_unattached(
    recorded_events: TakeEvents,
) -> None:
    record_refund(Session(), {"ticket": "T-1"})

    step_start, step_end = recorded_events()

    assert step_start["flags"] == ["unattached"]
    assert step_start["parent_span_id"] is None
    assert "events_produced" not in step_end


@pytest.mark.parametrize(
    "labels", [{"write": True, "read": True}, {"fake": None}, {"read": True, "fake": 0}]
)
def test_labels_that_contradict_each_other_are_refused_at_decoration(labels: Any) -> None:
    with pytest.raises(TypeError):
        irimi.function(**labels)
