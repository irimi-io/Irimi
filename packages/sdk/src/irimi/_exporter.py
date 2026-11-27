from __future__ import annotations

import atexit
import os
import threading
import time
from collections import deque
from collections.abc import Callable

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


class Exporter:
    def __init__(
        self, send_batch: Callable[[tuple[Event, ...]], Delivery], drops: DropTally
    ) -> None:
        self._send_batch = send_batch
        self._drops = drops
        self._condition: threading.Condition
        self._pending: deque[tuple[Event, int]]
        self._pending_size: int
        self._unsettled_count: int
        self._waiting_flush_count: int
        self._sender: threading.Thread | None
        self._reset()
        atexit.register(self.flush, EXIT_FLUSH_SECONDS)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def enqueue(self, event: Event, approximate_size: int) -> None:
        with self._condition:
            is_full = (
                len(self._pending) >= QUEUE_LIMIT
                or self._pending_size + approximate_size > QUEUE_SIZE_LIMIT
            )
            pending_size_before = self._pending_size
            if not is_full:
                self._pending.append((event, approximate_size))
                self._pending_size += approximate_size
                self._unsettled_count += 1
                self._ensure_sender_is_running()
            has_reached_size_limit = pending_size_before < BATCH_SIZE_LIMIT <= self._pending_size
            if len(self._pending) in (1, BATCH_SIZE) or has_reached_size_limit:
                self._condition.notify_all()
        if is_full:
            self._drops.record(1, "irimi events were dropped: too many are waiting to be sent")

    def flush(self, timeout: float | None = None) -> bool:
        with self._condition:
            self._waiting_flush_count += 1
            self._condition.notify_all()
            is_drained = self._condition.wait_for(lambda: self._unsettled_count == 0, timeout)
            self._waiting_flush_count -= 1
            return is_drained

    def _reset(self) -> None:
        self._condition = threading.Condition()
        self._pending = deque()
        self._pending_size = 0
        self._unsettled_count = 0
        self._waiting_flush_count = 0
        self._sender = None

    def _ensure_sender_is_running(self) -> None:
        if self._sender is not None:
            return
        self._sender = threading.Thread(target=self._send_forever, name="irimi-export", daemon=True)
        self._sender.start()

    def _send_forever(self) -> None:
        while True:
            batch = self._take_batch()
            self._deliver(batch)
            with self._condition:
                self._unsettled_count -= len(batch)
                self._condition.notify_all()

    def _take_batch(self) -> tuple[Event, ...]:
        with self._condition:
            self._condition.wait_for(lambda: len(self._pending) > 0)
            self._condition.wait_for(self._is_send_due, SEND_INTERVAL_SECONDS)
            events: list[Event] = []
            batch_size = 0
            while self._pending and len(events) < BATCH_SIZE and batch_size < BATCH_SIZE_LIMIT:
                event, approximate_size = self._pending.popleft()
                events.append(event)
                batch_size += approximate_size
            self._pending_size -= batch_size
            return tuple(events)

    def _is_send_due(self) -> bool:
        return (
            len(self._pending) >= BATCH_SIZE
            or self._pending_size >= BATCH_SIZE_LIMIT
            or self._waiting_flush_count > 0
        )

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
