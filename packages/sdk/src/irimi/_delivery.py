from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Literal

from irimi._wire import (
    EVENTS_PATH,
    Batch,
    Event,
    JsonValue,
    RefusedEvent,
    parse_refused_events,
    write_batch,
)

REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Delivery:
    status: Literal["delivered", "refused", "failed"]
    refused_events: tuple[RefusedEvent, ...] = ()


@dataclass(frozen=True)
class Destination:
    store_url: str
    api_key: str | None


class StoreClient:
    def __init__(self, destination: Destination) -> None:
        self._events_url = destination.store_url.rstrip("/") + EVENTS_PATH
        self._headers = {"Content-Type": "application/json"}
        if destination.api_key:
            self._headers["Authorization"] = f"Bearer {destination.api_key}"

    def send_batch(self, events: tuple[Event, ...]) -> Delivery:
        import http.client
        import json
        import urllib.error
        import urllib.request

        batch = Batch(events=events, fields={"sdk": self._sdk_description})
        request = urllib.request.Request(
            self._events_url,
            data=json.dumps(write_batch(batch)).encode(),
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as answer:
                return Delivery("delivered", parse_refused_events(json.loads(answer.read())))
        except urllib.error.HTTPError as error:
            return Delivery("refused" if 400 <= error.code < 500 else "failed")
        except ValueError:
            return Delivery("refused")
        except (OSError, http.client.HTTPException):
            return Delivery("failed")

    @functools.cached_property
    def _sdk_description(self) -> dict[str, JsonValue]:
        from importlib import metadata

        try:
            version = metadata.version("irimi")
        except metadata.PackageNotFoundError:
            version = "unknown"
        return {"name": "irimi-python", "version": version}
