from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable

from irimi._delivery import Delivery
from irimi._wire import Event

QUEUE_LIMIT = 10_000
BATCH_SIZE = 100
SEND_INTERVAL_SECONDS = 1.0
SEND_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.2
EXIT_FLUSH_SECONDS = 5.0

logger = logging.getLogger("irimi")


class Exporter:
    def __init__(self, send_batch: Callable[[tuple[Event, ...]], Delivery]) -> None:
        self._send_batch = send_batch
        self._has_warned = False
        self._condition: threading.Condition
        self._pending: deque[Event]
        self._unsettled_count: int
        self._dropped_count: int
        self._dropped_count_at_last_flush: int
        self._waiting_flush_count: int
        self._sender: threading.Thread | None
        self._reset()
        atexit.register(self.flush, EXIT_FLUSH_SECONDS)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    def enqueue(self, event: Event) -> None:
        with self._condition:
            if len(self._pending) >= QUEUE_LIMIT:
                self._dropped_count += 1
                return
            self._pending.append(event)
            self._unsettled_count += 1
            self._ensure_sender_is_running()
            if len(self._pending) >= BATCH_SIZE:
                self._condition.notify_all()

    def flush(self, timeout: float | None = None) -> bool:
        with self._condition:
            self._waiting_flush_count += 1
            self._condition.notify_all()
            is_drained = self._condition.wait_for(lambda: self._unsettled_count == 0, timeout)
            self._waiting_flush_count -= 1
            dropped_since_last_flush = self._dropped_count - self._dropped_count_at_last_flush
            self._dropped_count_at_last_flush = self._dropped_count
            return is_drained and dropped_since_last_flush == 0

    def _reset(self) -> None:
        self._condition = threading.Condition()
        self._pending = deque()
        self._unsettled_count = 0
        self._dropped_count = 0
        self._dropped_count_at_last_flush = 0
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
            is_delivered = self._deliver(batch) if batch else True
            with self._condition:
                self._unsettled_count -= len(batch)
                if not is_delivered:
                    self._dropped_count += len(batch)
                self._condition.notify_all()

    def _take_batch(self) -> tuple[Event, ...]:
        with self._condition:
            self._condition.wait_for(self._is_send_due, SEND_INTERVAL_SECONDS)
            size = min(BATCH_SIZE, len(self._pending))
            return tuple(self._pending.popleft() for _ in range(size))

    def _is_send_due(self) -> bool:
        if not self._pending:
            return False
        return len(self._pending) >= BATCH_SIZE or self._waiting_flush_count > 0

    def _deliver(self, batch: tuple[Event, ...]) -> bool:
        try:
            delivery = self._send_with_retries(batch)
        except Exception:
            self._warn_once("irimi could not send a batch of events; they were dropped", True)
            return False
        if delivery != "delivered":
            self._warn_once(
                f"irimi events were dropped: sending a batch to the store {delivery}", False
            )
        return delivery == "delivered"

    def _send_with_retries(self, batch: tuple[Event, ...]) -> Delivery:
        delivery = self._send_batch(batch)
        for attempt in range(1, SEND_ATTEMPTS):
            if delivery != "failed":
                break
            time.sleep(RETRY_DELAY_SECONDS * attempt)
            delivery = self._send_batch(batch)
        return delivery

    def _warn_once(self, message: str, has_exception: bool) -> None:
        if self._has_warned:
            return
        self._has_warned = True
        logger.warning(message, exc_info=has_exception)
