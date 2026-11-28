from __future__ import annotations

import atexit
import itertools
import math
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from irimi._delivery import Delivery
from irimi._drops import DropTally
from irimi._wire import Event

QUEUE_LIMIT = 10_000
QUEUE_SIZE_LIMIT = 64 * 1024 * 1024
BATCH_SIZE = 100
BATCH_SIZE_LIMIT = 8 * 1024 * 1024
SEND_INTERVAL_SECONDS = 1.0
SEND_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.2
EXIT_FLUSH_SECONDS = 5.0
KIBIBYTE = 1024


@dataclass(frozen=True)
class _Offer:
    event: Event
    approximate_size: int


class _WaitingSize:
    def __init__(self) -> None:
        self._kibibytes: list[None] = []

    def add(self, size: int) -> None:
        self._kibibytes.extend(itertools.repeat(None, _count_kibibytes(size)))

    def remove(self, size: int) -> None:
        del self._kibibytes[: _count_kibibytes(size)]

    def read(self) -> int:
        return len(self._kibibytes) * KIBIBYTE


def _count_kibibytes(size: int) -> int:
    return math.ceil(size / KIBIBYTE)


class Exporter:
    def __init__(
        self, send_batch: Callable[[tuple[Event, ...]], Delivery], drops: DropTally
    ) -> None:
        self._send_batch = send_batch
        self._drops = drops
        self._offers: queue.SimpleQueue[_Offer | threading.Event]
        self._offers_made: itertools.count[int]
        self._waiting_size: _WaitingSize
        self._sender: threading.Thread | None
        self._pending: deque[_Offer | threading.Event]
        self._pending_size: int
        self._pending_flush_count: int
        self._reset()
        atexit.register(self.flush, EXIT_FLUSH_SECONDS)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def enqueue(self, event: Event, approximate_size: int) -> None:
        if not self._take_room(approximate_size):
            self._drops.record(1, "irimi events were dropped: too many are waiting to be sent")
            return
        self._offers.put(_Offer(event, approximate_size))
        self._ensure_sender_is_running()

    def flush(self, timeout: float | None = None) -> bool:
        if self._sender is None:
            return True
        is_reached = threading.Event()
        self._offers.put(is_reached)
        return is_reached.wait(timeout)

    def _reset(self) -> None:
        self._offers = queue.SimpleQueue()
        self._offers_made = itertools.count()
        self._waiting_size = _WaitingSize()
        self._sender = None
        self._pending = deque()
        self._pending_size = 0
        self._pending_flush_count = 0

    def _take_room(self, size: int) -> bool:
        if self._offers.qsize() + len(self._pending) >= QUEUE_LIMIT:
            return False
        self._waiting_size.add(size)
        if self._waiting_size.read() <= QUEUE_SIZE_LIMIT:
            return True
        self._waiting_size.remove(size)
        return False

    def _ensure_sender_is_running(self) -> None:
        is_first_offer = next(self._offers_made) == 0
        if not is_first_offer:
            return
        self._sender = threading.Thread(target=self._send_forever, name="irimi-export", daemon=True)
        self._sender.start()

    def _send_forever(self) -> None:
        while True:
            self._receive_until_send_is_due()
            batch = self._take_events_within_limits()
            if batch:
                self._deliver(batch)
            self._answer_flushes_reached()

    def _receive_until_send_is_due(self) -> None:
        if not self._pending:
            self._receive(self._offers.get())
        deadline = time.monotonic() + SEND_INTERVAL_SECONDS
        while not self._is_send_due():
            try:
                self._receive(self._offers.get(timeout=max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                return

    def _receive(self, offer: _Offer | threading.Event) -> None:
        self._pending.append(offer)
        if isinstance(offer, threading.Event):
            self._pending_flush_count += 1
        else:
            self._pending_size += offer.approximate_size

    def _is_send_due(self) -> bool:
        return (
            len(self._pending) >= BATCH_SIZE
            or self._pending_size >= BATCH_SIZE_LIMIT
            or self._pending_flush_count > 0
        )

    def _take_events_within_limits(self) -> tuple[Event, ...]:
        events: list[Event] = []
        batch_size = 0
        while self._pending and len(events) < BATCH_SIZE:
            offer = self._pending[0]
            if isinstance(offer, threading.Event):
                break
            if events and batch_size + offer.approximate_size > BATCH_SIZE_LIMIT:
                break
            self._pending.popleft()
            self._waiting_size.remove(offer.approximate_size)
            events.append(offer.event)
            batch_size += offer.approximate_size
        self._pending_size -= batch_size
        return tuple(events)

    def _answer_flushes_reached(self) -> None:
        while self._pending:
            offer = self._pending[0]
            if not isinstance(offer, threading.Event):
                return
            self._pending.popleft()
            self._pending_flush_count -= 1
            offer.set()

    def _deliver(self, batch: tuple[Event, ...]) -> None:
        try:
            delivery = self._send_with_retries(batch)
        except Exception:
            self._drops.record(
                len(batch), "irimi could not send a batch of events; they were dropped", True
            )
            return
        if delivery.status != "delivered":
            self._drops.record(
                len(batch),
                f"irimi events were dropped: sending a batch to the store {delivery.status}",
            )
        elif delivery.refused_events:
            self._drops.record(
                len(delivery.refused_events),
                f"irimi events were dropped: the store refused {len(delivery.refused_events)}, "
                f"the first because {delivery.refused_events[0].reason}",
            )

    def _send_with_retries(self, batch: tuple[Event, ...]) -> Delivery:
        delivery = self._send_batch(batch)
        for attempt in range(1, SEND_ATTEMPTS):
            if delivery.status != "failed":
                break
            time.sleep(RETRY_DELAY_SECONDS * attempt)
            delivery = self._send_batch(batch)
        return delivery
