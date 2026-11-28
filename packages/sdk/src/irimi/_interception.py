from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from irimi._outbound_calls import (
    CallOutcome,
    OutboundCall,
    describe_outcome,
    describe_request,
    name_http_call,
)
from irimi._recording import ActiveCall, Recorder


@dataclass(frozen=True)
class OpenCall:
    call: ActiveCall
    is_attached: bool


class Interception:
    def __init__(self, recorder: Recorder, report_failure: Callable[[str], None]) -> None:
        self._recorder = recorder
        self.report_failure = report_failure

    def begin(self, call: OutboundCall) -> OpenCall | None:
        try:
            current_call = self._recorder.find_current_call()
            is_attached = current_call is not None and current_call.run.is_workflow
            payload = describe_request(call, is_attached)
            active_call = self._recorder.begin_detached_call("http", name_http_call(call), payload)
        except Exception:
            self.report_failure("record the start of an outbound call")
            return None
        return OpenCall(call=active_call, is_attached=is_attached)

    def end(self, open_call: OpenCall | None, outcome: CallOutcome) -> None:
        if open_call is None:
            return
        try:
            payload = describe_outcome(outcome, open_call.is_attached)
            self._recorder.end_detached_call(open_call.call, payload)
        except Exception:
            self.report_failure("record the end of an outbound call")
