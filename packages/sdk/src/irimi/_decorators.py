from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, cast, overload

from irimi._labeled_calls import CallLabel, describe_error, describe_output, describe_start
from irimi._recording import ActiveCall, Payload
from irimi._sdk import SDK

DecoratedFunction = TypeVar("DecoratedFunction", bound=Callable[..., Any])
Outcome = TypeVar("Outcome")


@overload
def workflow(fn: DecoratedFunction, /) -> DecoratedFunction: ...
@overload
def workflow(*, name: str | None = None) -> Callable[[DecoratedFunction], DecoratedFunction]: ...
def workflow(fn: Any = None, *, name: str | None = None) -> Any:
    """Mark a function as a workflow: each call is recorded as one run.

    The function keeps its signature and behaviour, sync or async. Its arguments, its return
    value or exception, and every labeled function called inside it are recorded. A workflow
    called inside another workflow is a step of the outer run. Without `irimi.init` it does
    nothing.

    `name` identifies the workflow and defaults to the function's name.
    """

    def decorate(function: DecoratedFunction) -> DecoratedFunction:
        label = CallLabel(
            kind="workflow", name=name or function.__name__, import_path=_import_path(function)
        )
        SDK.register_workflow(label.name, label.import_path)
        return _record_calls(function, label)

    return decorate if fn is None else decorate(fn)


@overload
def function(fn: DecoratedFunction, /) -> DecoratedFunction: ...
@overload
def function(
    *, name: str | None = None, write: bool = False, read: bool = False, fake: Any = ...
) -> Callable[[DecoratedFunction], DecoratedFunction]: ...
def function(
    fn: Any = None,
    *,
    name: str | None = None,
    write: bool = False,
    read: bool = False,
    fake: Any = ...,
) -> Any:
    """Record each call to a function as a step of the run it is called in.

    The function keeps its signature and behaviour, sync or async. `name` identifies the step
    and defaults to the function's name.

    `write=True` marks a side effect irimi cannot see, such as a database write: in a replay
    the function is not executed. `fake` is the value it returns in a replay when the recording
    has no such call. `read=True` marks a read whose recorded value is served in a replay.
    `write` with `read`, or `fake` without `write`, raises `TypeError`.
    """
    if write and read:
        raise TypeError("irimi.function: write=True and read=True cannot be combined")
    if fake is not ... and not write:
        raise TypeError("irimi.function: fake= needs write=True")

    def decorate(function: DecoratedFunction) -> DecoratedFunction:
        label = CallLabel(
            kind="function",
            name=name or function.__name__,
            import_path=_import_path(function),
            is_write=write,
            is_read=read,
        )
        return _record_calls(function, label)

    return decorate if fn is None else decorate(fn)


def _record_calls(function: DecoratedFunction, label: CallLabel) -> DecoratedFunction:
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def record_async_call(*arguments: Any, **keyword_arguments: Any) -> Any:
            call = _begin_call(label, arguments, keyword_arguments)
            try:
                output = await function(*arguments, **keyword_arguments)
            except BaseException as error:
                _end_call(call, describe_error, error)
                raise
            _end_call(call, describe_output, output)
            return output

        return cast("DecoratedFunction", record_async_call)

    @functools.wraps(function)
    def record_call(*arguments: Any, **keyword_arguments: Any) -> Any:
        call = _begin_call(label, arguments, keyword_arguments)
        try:
            output = function(*arguments, **keyword_arguments)
        except BaseException as error:
            _end_call(call, describe_error, error)
            raise
        _end_call(call, describe_output, output)
        return output

    return cast("DecoratedFunction", record_call)


def _begin_call(
    label: CallLabel, arguments: Sequence[object], keyword_arguments: Mapping[str, object]
) -> ActiveCall | None:
    recording = SDK.recording
    if recording is None:
        return None
    try:
        is_root = recording.recorder.find_current_call() is None
        payload = describe_start(label, arguments, keyword_arguments, recording.settings, is_root)
        return recording.recorder.begin_call(label.kind, label.name, payload)
    except Exception:
        SDK.report_failure("record the start of a call")
        return None


def _end_call(
    call: ActiveCall | None,
    describe_end: Callable[[Outcome], Payload],
    outcome: Outcome,
) -> None:
    recording = SDK.recording
    if call is None or recording is None:
        return
    try:
        recording.recorder.end_call(call, describe_end(outcome))
    except Exception:
        SDK.report_failure("record the end of a call")


def _import_path(function: Callable[..., Any]) -> str:
    return f"{function.__module__}:{function.__qualname__}"
