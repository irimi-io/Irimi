from __future__ import annotations

import threading

from irimi._bodies import BodyCopy, CopiedBody
from irimi._interception import Interception, OpenCall
from irimi._outbound_calls import CallOutcome, OutboundResponse


class ResponseRecording:
    def __init__(
        self, interception: Interception, open_call: OpenCall, response: OutboundResponse
    ) -> None:
        self._interception = interception
        self._open_call = open_call
        self._response = response
        self._body: BodyCopy | None = BodyCopy() if open_call.is_attached else None
        self._has_copy_failed = False
        self._is_ended = False
        self._end_lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        body_copy = self._body
        if body_copy is None:
            return
        try:
            body_copy.add(chunk)
        except Exception:
            self._body = None
            self._has_copy_failed = True
            self._interception.report_failure("copy an outbound response body")

    def end(self, was_read_to_end: bool, error: BaseException | None) -> None:
        if not self._claim_end():
            return
        try:
            outcome = CallOutcome(
                response=self._response,
                body=self._release_body(),
                was_read_to_end=was_read_to_end and not self._has_copy_failed,
                error=error,
            )
        except Exception:
            self._interception.report_failure("take the copy of an outbound response body")
            return
        self._interception.end(self._open_call, outcome)

    def _release_body(self) -> CopiedBody | None:
        body_copy, self._body = self._body, None
        return body_copy.take() if body_copy is not None else None

    def _claim_end(self) -> bool:
        with self._end_lock:
            if self._is_ended:
                return False
            self._is_ended = True
            return True


def end_without_response(
    interception: Interception, open_call: OpenCall | None, error: BaseException
) -> None:
    outcome = CallOutcome(response=None, body=None, was_read_to_end=False, error=error)
    interception.end(open_call, outcome)
