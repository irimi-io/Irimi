from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol

from irimi._recorded_calls import CallRole, RecordedCall
from irimi._wire import Flag, JsonValue, field_names

MatchRule = Literal["same request", "same name, same position", "same kind and name"]
Answer = Literal["live", "recorded", "faked"]

WRITE_ROLES = frozenset({"http_write", "write_function"})


@dataclass(frozen=True)
class CandidateCall:
    role: CallRole
    name: str
    match_key: JsonValue


@dataclass(frozen=True)
class Decision:
    role: CallRole
    answer: Answer
    matched: RecordedCall | None
    rule: MatchRule | None
    is_changed: bool
    flags: tuple[Flag, ...]


class Decides(Protocol):
    def decide(self, call: CandidateCall) -> Decision: ...


FAKED_UNDESCRIBED_CALL = Decision(
    "http_write", "faked", None, None, False, (field_names.UNDESCRIBED_CALL,)
)


class ReplayDecider:
    def __init__(self, calls: tuple[RecordedCall, ...]) -> None:
        self._calls = calls
        self._claimed: set[str] = set()
        self._lock = Lock()

    def decide(self, call: CandidateCall) -> Decision:
        with self._lock:
            decision = self._choose(call)
            if decision.matched is not None:
                self._claimed.add(decision.matched.span_id)
            return decision

    def _choose(self, call: CandidateCall) -> Decision:
        if call.role == "plain":
            return self._choose_live_by_name(call)
        exact = self._find_exact(call)
        if exact is not None:
            return _serve_exact(call, exact)
        if call.role in WRITE_ROLES:
            return self._choose_for_new_write(call)
        if call.role == "llm":
            return self._choose_live_by_name(call)
        return Decision(call.role, "live", None, None, False, (field_names.UNMATCHED_READ,))

    def _choose_live_by_name(self, call: CandidateCall) -> Decision:
        following = self._find_next(call)
        if following is None:
            return Decision(call.role, "live", None, None, False, ())
        return Decision(call.role, "live", following, "same kind and name", False, ())

    def _choose_for_new_write(self, call: CandidateCall) -> Decision:
        following = self._find_next(call)
        if following is None:
            return Decision(call.role, "faked", None, None, False, (field_names.NEW_WRITE,))
        if following.result is None:
            flags: tuple[Flag, ...] = (field_names.RECORDED_VALUE_UNSERVABLE,)
            return Decision(call.role, "faked", following, "same name, same position", True, flags)
        return Decision(call.role, "recorded", following, "same name, same position", True, ())

    def _find_exact(self, call: CandidateCall) -> RecordedCall | None:
        return next(
            (
                recorded
                for recorded in self._unclaimed_named(call)
                if recorded.match_key == call.match_key
            ),
            None,
        )

    def _find_next(self, call: CandidateCall) -> RecordedCall | None:
        return next(self._unclaimed_named(call), None)

    def _unclaimed_named(self, call: CandidateCall) -> Iterator[RecordedCall]:
        return (
            recorded
            for recorded in self._calls
            if recorded.span_id not in self._claimed
            and recorded.role == call.role
            and recorded.name == call.name
        )


def _serve_exact(call: CandidateCall, exact: RecordedCall) -> Decision:
    if exact.result is not None:
        return Decision(call.role, "recorded", exact, "same request", False, ())
    answer: Answer = "faked" if call.role in WRITE_ROLES else "live"
    return Decision(
        call.role, answer, exact, "same request", False, (field_names.RECORDED_VALUE_UNSERVABLE,)
    )
