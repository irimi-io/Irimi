from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from typing import Literal

from irimi._wire import Batch, Event, write_batch

EVENTS_PATH = "/v1/events"
REQUEST_TIMEOUT_SECONDS = 5.0

Delivery = Literal["delivered", "refused", "failed"]


@dataclass(frozen=True)
class Destination:
    store_url: str
    api_key: str | None


def post_batch(destination: Destination, events: tuple[Event, ...]) -> Delivery:
    batch = Batch(events=events, fields={"sdk": {"name": "irimi-python", "version": _version()}})
    request = urllib.request.Request(
        destination.store_url.rstrip("/") + EVENTS_PATH,
        data=json.dumps(write_batch(batch)).encode(),
        headers=_headers(destination),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
            return "delivered"
    except urllib.error.HTTPError as error:
        return "refused" if 400 <= error.code < 500 else "failed"
    except (OSError, http.client.HTTPException):
        return "failed"


def _headers(destination: Destination) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if destination.api_key:
        headers["Authorization"] = f"Bearer {destination.api_key}"
    return headers


def _version() -> str:
    try:
        return metadata.version("irimi")
    except metadata.PackageNotFoundError:
        return "unknown"
