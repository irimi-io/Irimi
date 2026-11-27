from collections.abc import Sequence
from typing import Protocol

from irimi._wire import Event

from irimi_store.pages import Page


class StorageEngine(Protocol):
    def add_events(self, project: str, events: Sequence[Event]) -> None: ...

    def read_run_events(
        self, project: str, trace_id: str, cursor: str | None, limit: int
    ) -> Page[Event]: ...
