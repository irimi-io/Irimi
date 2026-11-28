from __future__ import annotations

from typing import Literal

from irimi._wire.json_value import JsonValue

INPUT = "input"
OUTPUT = "output"
STATUS = "status"
ERROR = "error"
FLAGS = "flags"
WRITE = "write"
READ = "read"
MODE = "mode"
ENVIRONMENT = "environment"
DEPLOYMENT = "deployment"
STARTED_BY = "started_by"
EVENTS_PRODUCED = "events_produced"
NEXT_CURSOR = "next_cursor"
REQUEST = "request"
RESPONSE = "response"
ANSWERED = "answered"
REPLAY = "replay"
MATCHED = "matched"
CHANGED = "changed"
BODY = "body"
DATA = "data"
ENCODING = "encoding"
CONTENT_ENCODING = "content_encoding"
METHOD = "method"
URL = "url"
HEADERS = "headers"
SPAN_ID = "span_id"
RULE = "rule"

Flag = Literal[
    "not_replayable",
    "unserialisable_output",
    "unattached",
    "request_body_truncated",
    "response_body_truncated",
    "request_body_not_recorded",
    "response_body_incomplete",
    "new_write",
    "unmatched_read",
    "undescribed_call",
    "recorded_value_unservable",
    "caught_by_proxy",
    "after_divergence",
]

NOT_REPLAYABLE: Flag = "not_replayable"
UNSERIALISABLE_OUTPUT: Flag = "unserialisable_output"
RESPONSE_BODY_TRUNCATED: Flag = "response_body_truncated"
RESPONSE_BODY_INCOMPLETE: Flag = "response_body_incomplete"
UNDESCRIBED_CALL: Flag = "undescribed_call"
UNMATCHED_READ: Flag = "unmatched_read"
NEW_WRITE: Flag = "new_write"
RECORDED_VALUE_UNSERVABLE: Flag = "recorded_value_unservable"


def write_flags(*flags: Flag) -> list[JsonValue]:
    return list(flags)
