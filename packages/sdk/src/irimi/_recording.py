from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass

from irimi._wire import EndEvent, Event, JsonValue, StartEvent, field_names


@dataclass(frozen=True)
class Payload:
    fields: Mapping[str, JsonValue]
    approximate_size: int
    unread_objects: int = 0


class Run:
    def __init__(self, trace_id: str, is_workflow: bool) -> None:
        self.trace_id = trace_id
        self.is_workflow = is_workflow
        self._lock = threading.Lock()
        self._calls_started = 0
        self._events_produced = 0

    def count_start(self) -> int:
        with self._lock:
            seq = self._calls_started
            self._calls_started += 1
            self._events_produced += 1
            return seq

    def count_end(self) -> int:
        with self._lock:
            self._events_produced += 1
            return self._events_produced


@dataclass(frozen=True)
class ActiveCall:
    run: Run
    span_id: str
    parent: ActiveCall | None


class Recorder:
    def __init__(
        self, enqueue: Callable[[Event, int], None], read_clock: Callable[[], int]
    ) -> None:
        self._enqueue = enqueue
        self._read_clock = read_clock
        self._current_call: ContextVar[ActiveCall | None] = ContextVar(
            "irimi_current_call", default=None
        )

    def find_current_call(self) -> ActiveCall | None:
        return self._current_call.get()

    def begin_call(self, kind: str, name: str, payload: Payload) -> ActiveCall:
        call = self.begin_detached_call(kind, name, payload)
        self._current_call.set(call)
        return call

    def end_call(self, call: ActiveCall, payload: Payload) -> None:
        self._current_call.set(call.parent)
        self._write_end(call, payload, is_run_end=call.parent is None and call.run.is_workflow)

    def begin_detached_call(self, kind: str, name: str, payload: Payload) -> ActiveCall:
        parent = self._current_call.get()
        run = parent.run if parent else Run(_new_trace_id(), is_workflow=kind == "workflow")
        call = ActiveCall(run=run, span_id=_new_span_id(), parent=parent)
        self._enqueue(
            StartEvent(
                id=_new_event_id(),
                type="start",
                trace_id=run.trace_id,
                span_id=call.span_id,
                time_unix_nano=self._read_clock(),
                parent_span_id=parent.span_id if parent else None,
                kind=kind,
                name=name,
                seq=run.count_start(),
                fields=payload.fields,
            ),
            payload.approximate_size,
        )
        return call

    def end_detached_call(self, call: ActiveCall, payload: Payload) -> None:
        self._write_end(call, payload, is_run_end=False)

    def _write_end(self, call: ActiveCall, payload: Payload, is_run_end: bool) -> None:
        events_produced = call.run.count_end()
        fields = payload.fields
        if is_run_end:
            fields = {**fields, field_names.EVENTS_PRODUCED: events_produced}
        self._enqueue(
            EndEvent(
                id=_new_event_id(),
                type="end",
                trace_id=call.run.trace_id,
                span_id=call.span_id,
                time_unix_nano=self._read_clock(),
                fields=fields,
            ),
            payload.approximate_size,
        )


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


def _new_event_id() -> str:
    return secrets.token_hex(16)
