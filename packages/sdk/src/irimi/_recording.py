from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from irimi._ids import new_event_id, new_span_id, new_trace_id
from irimi._serialisation import serialise, serialise_arguments
from irimi._wire import EndEvent, Event, JsonValue, StartEvent

CallKind = Literal["workflow", "function"]


@dataclass(frozen=True)
class CallLabel:
    kind: CallKind
    name: str
    import_path: str
    is_write: bool = False
    is_read: bool = False


@dataclass(frozen=True)
class RunSettings:
    environment: str | None
    version: str | None
    commit: str | None


class Run:
    def __init__(self, trace_id: str, is_workflow: bool) -> None:
        self.trace_id = trace_id
        self.is_workflow = is_workflow
        self._lock = threading.Lock()
        self._next_seq = 0
        self._events_produced = 0

    def take_seq(self) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    def count_event(self) -> int:
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
        self,
        settings: RunSettings,
        enqueue: Callable[[Event], None],
        read_clock: Callable[[], int],
    ) -> None:
        self._settings = settings
        self._enqueue = enqueue
        self._read_clock = read_clock
        self._current_call: ContextVar[ActiveCall | None] = ContextVar(
            "irimi_current_call", default=None
        )

    def begin_call(
        self,
        label: CallLabel,
        arguments: Sequence[object],
        keyword_arguments: Mapping[str, object],
    ) -> ActiveCall:
        parent = self._current_call.get()
        run = parent.run if parent else Run(new_trace_id(), is_workflow=label.kind == "workflow")
        span_id = new_span_id()
        fields = self._describe_call(label, arguments, keyword_arguments, is_root=parent is None)
        seq = run.take_seq()
        run.count_event()
        self._enqueue(
            StartEvent(
                id=new_event_id(),
                type="start",
                trace_id=run.trace_id,
                span_id=span_id,
                time_unix_nano=self._read_clock(),
                parent_span_id=parent.span_id if parent else None,
                kind=label.kind,
                name=label.name,
                seq=seq,
                fields=fields,
            )
        )
        call = ActiveCall(run=run, span_id=span_id, parent=parent)
        self._current_call.set(call)
        return call

    def end_call_with_output(self, call: ActiveCall, output: object) -> None:
        serialised = serialise(output)
        fields: dict[str, JsonValue] = {"status": "ok", "output": serialised.value}
        if not serialised.is_complete:
            fields["flags"] = ["unserialisable_output"]
        self._end_call(call, fields)

    def end_call_with_error(self, call: ActiveCall, error: BaseException) -> None:
        error_type = type(error)
        self._end_call(
            call,
            {
                "status": "error",
                "error": {
                    "type": f"{error_type.__module__}.{error_type.__qualname__}",
                    "message": str(error),
                },
            },
        )

    def _end_call(self, call: ActiveCall, fields: dict[str, JsonValue]) -> None:
        self._current_call.set(call.parent)
        events_produced = call.run.count_event()
        if call.parent is None and call.run.is_workflow:
            fields["events_produced"] = events_produced
        self._enqueue(
            EndEvent(
                id=new_event_id(),
                type="end",
                trace_id=call.run.trace_id,
                span_id=call.span_id,
                time_unix_nano=self._read_clock(),
                fields=fields,
            )
        )

    def _describe_call(
        self,
        label: CallLabel,
        arguments: Sequence[object],
        keyword_arguments: Mapping[str, object],
        is_root: bool,
    ) -> dict[str, JsonValue]:
        call_input = serialise_arguments(arguments, keyword_arguments)
        fields: dict[str, JsonValue] = {"input": call_input.value}
        if label.is_write:
            fields["write"] = True
        if label.is_read:
            fields["read"] = True
        if is_root:
            fields.update(self._describe_root(label, call_input.is_complete))
        return fields

    def _describe_root(self, label: CallLabel, is_input_complete: bool) -> dict[str, JsonValue]:
        if label.kind == "function":
            return {"flags": ["unattached"]}
        deployment: dict[str, JsonValue] = {
            name: value
            for name, value in (
                ("version", self._settings.version or self._settings.commit),
                ("commit", self._settings.commit),
            )
            if value is not None
        }
        fields: dict[str, JsonValue] = {
            "mode": "normal",
            "started_by": {"kind": "decorated_function", "import_path": label.import_path},
            "deployment": deployment,
        }
        if self._settings.environment is not None:
            fields["environment"] = self._settings.environment
        if not is_input_complete:
            fields["flags"] = ["not_replayable"]
        return fields
