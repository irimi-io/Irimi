import json
import urllib.request
from typing import Any

import pytest

NOTHING_LISTENS_HERE = "http://127.0.0.1:1"


def start(trace: str, span: str, name: str, time: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": f"{trace}-{span}-start",
        "type": "start",
        "trace_id": trace,
        "span_id": span,
        "time_unix_nano": str(time),
        "parent_span_id": None,
        "kind": "workflow",
        "name": name,
        "seq": 0,
        **fields,
    }


def post_events(store_url: str, *events: dict[str, Any]) -> None:
    body = json.dumps({"events": events}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{store_url}/v1/events", data=body)).close()


def test_a_run_with_gaps_and_odd_values_is_printed(local_store_url: str, run_irimi: Any) -> None:
    ended_only = {
        "id": "e9",
        "type": "end",
        "trace_id": "t1",
        "span_id": "s3",
        "time_unix_nano": "130",
        "status": 7,
    }
    post_events(
        local_store_url,
        start("t1", "s1", "run", 100, flags="odd"),
        start("t1", "s2", "step", 110, parent_span_id="s1", kind="added_later", seq=1),
        ended_only,
    )

    tree = run_irimi(local_store_url, "runs", "t1")
    listed = run_irimi(local_store_url, "runs")

    assert (tree.returncode, tree.stderr) == (0, "")
    assert tree.stdout.splitlines() == [
        "run  t1  unfinished",
        "workflow run  -  -  -",
        "  added_later step  -  -  -",
        "detached:",
        "  - -  7  -  -",
    ]
    assert listed.stdout.splitlines() == ["t1  run  1970-01-01T00:00:00Z  -  -  unfinished"]


def test_a_run_whose_id_has_url_characters_is_shown(local_store_url: str, run_irimi: Any) -> None:
    post_events(local_store_url, start("a b#1", "s1", "run", 100))

    shown = run_irimi(local_store_url, "runs", "a b#1")

    assert (shown.returncode, shown.stdout.splitlines()[0]) == (0, "run  a b#1  unfinished")


def test_the_next_page_is_named(local_store_url: str, run_irimi: Any) -> None:
    post_events(local_store_url, *(start(f"t{n}", "s1", "run", n) for n in (1, 2, 3)))

    first = run_irimi(local_store_url, "runs", "--limit", "2")
    cursor = first.stdout.splitlines()[-1].removeprefix("next page: --cursor ")
    second = run_irimi(local_store_url, "runs", "--limit", "2", "--cursor", cursor)

    assert [line.split()[0] for line in first.stdout.splitlines()[:2]] == ["t3", "t2"]
    assert [line.split()[0] for line in second.stdout.splitlines()] == ["t1"]


def test_an_empty_store_lists_no_runs(local_store_url: str, run_irimi: Any) -> None:
    listed = run_irimi(local_store_url, "runs")

    assert (listed.stdout, listed.returncode) == ("no runs\n", 0)


@pytest.mark.parametrize(
    ("arguments", "store_url"),
    [
        (["runs", "missing"], None),
        (["runs"], NOTHING_LISTENS_HERE),
        (["runs", "--limit", "0"], None),
        (["runs"], ""),
    ],
)
def test_a_failure_is_one_line_on_stderr_and_a_non_zero_exit(
    local_store_url: str, run_irimi: Any, arguments: list[str], store_url: str | None
) -> None:
    result = run_irimi(local_store_url if store_url is None else store_url, *arguments)

    assert result.returncode != 0
    assert (result.stdout, len(result.stderr.splitlines())) == ("", 1)


def test_an_answer_that_is_not_the_stores_is_a_failure(
    local_store_url: str, run_irimi: Any
) -> None:
    post_events(local_store_url, start("t1", "s1", "run", 100))

    result = run_irimi(f"{local_store_url}/v1/runs/t1/events?ignored=", "runs")

    assert result.returncode != 0
    assert (result.stdout, len(result.stderr.splitlines())) == ("", 1)
