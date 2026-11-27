from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from irimi._wire import EndEvent, Event, StartEvent

from irimi_store.pages import Page

DEFAULT_PROJECT = "default"

RootSet = Literal["runs", "unattached_calls"]


@dataclass(frozen=True)
class StoredRoot:
    start: StartEvent
    end: EndEvent | None
    events_held: int


class NotHeldError(LookupError):
    pass


class StorageEngine(Protocol):
    def add_events(self, project: str, events: Sequence[Event]) -> None: ...

    def read_run_events(
        self, project: str, trace_id: str, cursor: str | None, limit: int
    ) -> Page[Event]: ...

    def read_call_events(self, project: str, trace_id: str, span_id: str) -> tuple[Event, ...]: ...

    def list_roots(
        self, project: str, root_set: RootSet, cursor: str | None, limit: int
    ) -> Page[StoredRoot]: ...
