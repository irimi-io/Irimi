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

Flag = Literal[
    "not_replayable",
    "unserialisable_output",
    "unattached",
    "request_body_truncated",
    "response_body_truncated",
    "request_body_not_recorded",
    "response_body_incomplete",
]


def write_flags(*flags: Flag) -> list[JsonValue]:
    return list(flags)
