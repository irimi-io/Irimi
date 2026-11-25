from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, cast, overload

from irimi._recording import ActiveCall, CallLabel
from irimi._sdk import SDK

DecoratedFunction = TypeVar("DecoratedFunction", bound=Callable[..., Any])


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
                _end_call_with_error(call, error)
                raise
            _end_call_with_output(call, output)
            return output

        return cast("DecoratedFunction", record_async_call)

    @functools.wraps(function)
    def record_call(*arguments: Any, **keyword_arguments: Any) -> Any:
        call = _begin_call(label, arguments, keyword_arguments)
        try:
            output = function(*arguments, **keyword_arguments)
        except BaseException as error:
            _end_call_with_error(call, error)
            raise
        _end_call_with_output(call, output)
        return output

    return cast("DecoratedFunction", record_call)


def _begin_call(
    label: CallLabel, arguments: Sequence[object], keyword_arguments: Mapping[str, object]
) -> ActiveCall | None:
    if SDK.recorder is None:
        return None
    try:
        return SDK.recorder.begin_call(label, arguments, keyword_arguments)
    except Exception:
        SDK.report_failure("record the start of a call")
        return None


def _end_call_with_output(call: ActiveCall | None, output: object) -> None:
    if call is None or SDK.recorder is None:
        return
    try:
        SDK.recorder.end_call_with_output(call, output)
    except Exception:
        SDK.report_failure("record the end of a call")


def _end_call_with_error(call: ActiveCall | None, error: BaseException) -> None:
    if call is None or SDK.recorder is None:
        return
    try:
        SDK.recorder.end_call_with_error(call, error)
    except Exception:
        SDK.report_failure("record the end of a call")


def _import_path(function: Callable[..., Any]) -> str:
    return f"{function.__module__}:{function.__qualname__}"
