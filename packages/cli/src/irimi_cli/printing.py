from collections.abc import Iterator
from datetime import datetime, timezone

from irimi_store.pages import Page
from irimi_store.reader_documents import Node, RunSummary, RunTree

MISSING = "-"
COLUMN_GAP = "  "
INDENT = "  "
NANOSECONDS_IN_A_SECOND = 1_000_000_000
NANOSECONDS_IN_A_MILLISECOND = 1_000_000


def print_run_list(page: Page[RunSummary]) -> str:
    if not page.items:
        return "no runs"
    lines = [_print_run_summary(summary) for summary in page.items]
    if page.next_cursor is not None:
        lines.append(f"next page: --cursor {page.next_cursor}")
    return "\n".join(lines)


def print_run_tree(run: RunTree) -> str:
    lines = [_join_columns("run", run.id, _print_words(run.conditions))]
    if run.root is not None:
        lines.extend(_print_subtree(run.root, depth=0))
    if run.detached:
        lines.append("detached:")
    for node in run.detached:
        lines.extend(_print_subtree(node, depth=1))
    return "\n".join(lines)


def _print_run_summary(summary: RunSummary) -> str:
    return _join_columns(
        summary.id,
        summary.name,
        _print_time(summary.start_time_unix_nano),
        _print_duration(summary.start_time_unix_nano, summary.end_time_unix_nano),
        _print_value(summary.status),
        _print_words(summary.conditions),
    )


def _print_subtree(node: Node, depth: int) -> Iterator[str]:
    yield INDENT * depth + _join_columns(
        f"{_print_value(node.kind)} {_print_value(node.name)}",
        _print_value(node.status),
        _print_duration(node.start_time_unix_nano, node.end_time_unix_nano),
        _print_words(node.flags),
    )
    for child in node.children:
        yield from _print_subtree(child, depth + 1)


def _join_columns(*columns: str) -> str:
    return COLUMN_GAP.join(columns)


def _print_value(value: object) -> str:
    return MISSING if value is None else str(value)


def _print_words(words: tuple[object, ...]) -> str:
    return ",".join(str(word) for word in words) if words else MISSING


def _print_time(time_unix_nano: int) -> str:
    moment = datetime.fromtimestamp(time_unix_nano // NANOSECONDS_IN_A_SECOND, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_duration(start_time_unix_nano: int | None, end_time_unix_nano: int | None) -> str:
    if start_time_unix_nano is None or end_time_unix_nano is None:
        return MISSING
    nanoseconds = end_time_unix_nano - start_time_unix_nano
    if abs(nanoseconds) < NANOSECONDS_IN_A_SECOND:
        return f"{nanoseconds / NANOSECONDS_IN_A_MILLISECOND:.1f}ms"
    return f"{nanoseconds / NANOSECONDS_IN_A_SECOND:.2f}s"
