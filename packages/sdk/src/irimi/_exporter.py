from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from irimi._atomic_total import AtomicTotal
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


@dataclass(frozen=True)
class _Offer:
    event: Event
    approximate_size: int


@dataclass(frozen=True)
class _FlushRequest:
    is_reached: threading.Event = field(default_factory=threading.Event)


class Exporter:
    def __init__(
        self, send_batch: Callable[[tuple[Event, ...]], Delivery], drops: DropTally
    ) -> None:
        self._send_batch = send_batch
        self._drops = drops
        self._offers: queue.SimpleQueue[_Offer | _FlushRequest]
        self._offers_made: AtomicTotal
        self._sender_claims: AtomicTotal
        self._waiting_count: AtomicTotal
        self._waiting_size: AtomicTotal
        self._flushes_waiting: AtomicTotal
        self._sender_wakes: queue.SimpleQueue[None]
        self._received: deque[_Offer | _FlushRequest]
        self._reset()
        atexit.register(self.flush, EXIT_FLUSH_SECONDS)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def enqueue(self, event: Event, approximate_size: int) -> None:
        waiting_count = self._waiting_count.add(1)
        waiting_size = self._waiting_size.add(approximate_size)
        if waiting_count > QUEUE_LIMIT or waiting_size > QUEUE_SIZE_LIMIT:
            self._waiting_count.add(-1)
            self._waiting_size.add(-approximate_size)
            self._drops.record(1, "irimi events were dropped: too many are waiting to be sent")
            return
        self._offers.put(_Offer(event, approximate_size))
        self._offers_made.add(1)
        self._start_sender_if_none_runs()
        if waiting_count >= BATCH_SIZE or waiting_size >= BATCH_SIZE_LIMIT:
            self._wake_sender()

    def flush(self, timeout: float | None = None) -> bool:
        if self._offers_made.read() == 0:
            return True
        self._start_sender_if_none_runs()
        request = _FlushRequest()
        self._flushes_waiting.add(1)
        try:
            self._offers.put(request)
            self._wake_sender()
            return request.is_reached.wait(timeout)
        finally:
            self._flushes_waiting.add(-1)

    def _reset(self) -> None:
        self._offers = queue.SimpleQueue()
        self._offers_made = AtomicTotal()
        self._sender_claims = AtomicTotal()
        self._waiting_count = AtomicTotal()
        self._waiting_size = AtomicTotal()
        self._flushes_waiting = AtomicTotal()
        self._sender_wakes = queue.SimpleQueue()
        self._received = deque()

    def _start_sender_if_none_runs(self) -> None:
        if self._sender_claims.read() == 0:
            threading.Thread(
                target=self._send_as_the_only_sender, name="irimi-export", daemon=True
            ).start()

    def _send_as_the_only_sender(self) -> None:
        if self._sender_claims.add(1) == 1:
            self._send_forever()

    def _wake_sender(self) -> None:
        if self._sender_wakes.empty():
            self._sender_wakes.put(None)

    def _send_forever(self) -> None:
        while True:
            self._wait_until_send_is_due()
            self._receive_offers()
            events, flushes_reached = self._take_batch()
            if events:
                self._deliver(events)
            for flush in flushes_reached:
                flush.is_reached.set()

    def _wait_until_send_is_due(self) -> None:
        if not self._received:
            self._received.append(self._offers.get())
        deadline = time.monotonic() + SEND_INTERVAL_SECONDS
        while not self._is_send_due():
            try:
                self._sender_wakes.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return

    def _is_send_due(self) -> bool:
        return (
            self._waiting_count.read() >= BATCH_SIZE
            or self._waiting_size.read() >= BATCH_SIZE_LIMIT
            or self._flushes_waiting.read() > 0
        )

    def _receive_offers(self) -> None:
        while True:
            try:
                self._received.append(self._offers.get_nowait())
            except queue.Empty:
                return

    def _take_batch(self) -> tuple[tuple[Event, ...], tuple[_FlushRequest, ...]]:
        events: list[Event] = []
        flushes_reached: list[_FlushRequest] = []
        batch_size = 0
        while self._received:
            offer = self._received[0]
            if isinstance(offer, _FlushRequest):
                flushes_reached.append(offer)
            elif _has_room(len(events), batch_size, offer.approximate_size):
                events.append(offer.event)
                batch_size += offer.approximate_size
            else:
                break
            self._received.popleft()
        self._waiting_count.add(-len(events))
        self._waiting_size.add(-batch_size)
        return tuple(events), tuple(flushes_reached)

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


def _has_room(batch_count: int, batch_size: int, next_size: int) -> bool:
    if batch_count == 0:
        return True
    return batch_count < BATCH_SIZE and batch_size + next_size <= BATCH_SIZE_LIMIT
