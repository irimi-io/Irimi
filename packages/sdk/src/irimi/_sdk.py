from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from irimi._delivery import Destination, StoreClient
from irimi._drops import DropTally
from irimi._exporter import Exporter
from irimi._git import find_commit
from irimi._interception import Interception
from irimi._labeled_calls import RunSettings
from irimi._patching import patch_installed_libraries
from irimi._recording import Recorder

logger = logging.getLogger("irimi")


@dataclass(frozen=True)
class InitResult:
    """What `irimi.init` set up.

    `mode` is "normal" when runs are recorded and "shadow" inside a replay. `patched` names the
    client libraries irimi intercepts, and `unpatched` names the ones it saw and does not.
    """

    mode: Literal["normal", "shadow"]
    patched: tuple[str, ...]
    unpatched: tuple[str, ...]


@dataclass(frozen=True)
class InitArguments:
    store_url: str | None
    api_key: str | None
    version: str | None
    environment: str | None


@dataclass(frozen=True)
class Recording:
    recorder: Recorder
    settings: RunSettings


NORMAL_RUN = InitResult(mode="normal", patched=(), unpatched=())


class Sdk:
    def __init__(self) -> None:
        self.recording: Recording | None = None
        self._lock = threading.Lock()
        self._exporter: Exporter | None = None
        self._first_arguments: InitArguments | None = None
        self._first_result = NORMAL_RUN
        self._workflow_import_paths: dict[str, str] = {}
        self._drops = DropTally()

    def init(
        self,
        arguments: InitArguments,
        environment_variables: Mapping[str, str],
        working_directory: Path,
    ) -> InitResult:
        with self._lock:
            if self._first_arguments is not None:
                if arguments != self._first_arguments:
                    logger.warning("irimi.init was called again with different arguments; ignored")
                return self._first_result
            self._first_arguments = arguments
            self._first_result = self._start_recording(
                arguments, environment_variables, working_directory
            )
            return self._first_result

    def flush(self, timeout: float | None) -> bool:
        is_drained = self._exporter is None or self._exporter.flush(timeout)
        return is_drained and self._drops.take_new_count() == 0

    def register_workflow(self, name: str, import_path: str) -> None:
        with self._lock:
            registered_path = self._workflow_import_paths.setdefault(name, import_path)
        if registered_path != import_path:
            logger.warning(
                "two irimi workflows are named %r: %s and %s", name, registered_path, import_path
            )

    def report_failure(self, action: str) -> None:
        self._drops.record(1, f"irimi failed to {action}; the app is unaffected", True)

    def report_unread_objects(self, count: int) -> None:
        if count == 0:
            return
        self._drops.record(
            count,
            "irimi could not read the fields of an object, so it is recorded by its type; "
            "the app is unaffected",
        )

    def _start_recording(
        self,
        arguments: InitArguments,
        environment_variables: Mapping[str, str],
        working_directory: Path,
    ) -> InitResult:
        store_url = arguments.store_url or environment_variables.get("IRIMI_STORE_URL")
        if not store_url:
            logger.warning("irimi has no store URL, so nothing is recorded; set IRIMI_STORE_URL")
            return NORMAL_RUN
        destination = Destination(
            store_url=store_url,
            api_key=arguments.api_key or environment_variables.get("IRIMI_API_KEY"),
        )
        settings = RunSettings(
            environment=arguments.environment or environment_variables.get("IRIMI_ENVIRONMENT"),
            version=arguments.version or environment_variables.get("IRIMI_VERSION"),
            commit=find_commit(working_directory),
        )
        self._exporter = Exporter(StoreClient(destination).send_batch, self._drops)
        recorder = Recorder(self._exporter.enqueue, time.time_ns)
        self.recording = Recording(recorder, settings)
        report = patch_installed_libraries(Interception(recorder, self.report_failure))
        return InitResult(mode="normal", patched=report.patched, unpatched=report.unpatched)


SDK = Sdk()


def init(
    *,
    store_url: str | None = None,
    api_key: str | None = None,
    version: str | None = None,
    environment: str | None = None,
) -> InitResult:
    """Start recording workflow runs and sending them to the run store.

    Call it once, at startup. Each argument falls back to an environment variable:
    `IRIMI_STORE_URL`, `IRIMI_API_KEY`, `IRIMI_VERSION` and `IRIMI_ENVIRONMENT`. Without a store
    URL nothing is recorded. `version` names the deployment and falls back to the git commit.

    It never raises and never blocks: if the store cannot be reached, recording is dropped and
    the app carries on. A second call is ignored.
    """
    arguments = InitArguments(
        store_url=store_url, api_key=api_key, version=version, environment=environment
    )
    try:
        return SDK.init(arguments, os.environ, Path.cwd())
    except Exception:
        SDK.report_failure("initialise")
        return NORMAL_RUN


def flush(timeout: float | None = None) -> bool:
    """Wait until everything recorded so far has been sent to the run store.

    Returns whether everything was sent within `timeout` seconds; with no timeout it waits until
    sending finishes or fails. It never raises. Sending already happens in the background and
    at exit, so this is only needed where the process is frozen after a handler returns, as on
    serverless platforms.
    """
    try:
        return SDK.flush(timeout)
    except Exception:
        SDK.report_failure("flush")
        return False
