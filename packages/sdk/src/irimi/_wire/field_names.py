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

Flag = Literal["not_replayable", "unserialisable_output", "unattached"]


def write_flags(*flags: Flag) -> list[JsonValue]:
    return list(flags)
