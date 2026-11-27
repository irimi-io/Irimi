import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import irimi
import pytest
from irimi._wire import parse_batch

TakeEvents = Callable[[], list[dict[str, Any]]]
REDACTED = {"$irimi": "redacted", "reason": "credential"}

INIT_TWICE = """
import irimi
print(irimi.init({arguments}))
print(irimi.init({arguments}))
"""


@irimi.function
def note_refund(refund: dict[str, Any]) -> str:
    return str(refund["id"])


def test_an_outbound_call_is_recorded_as_a_step_of_its_run(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/v1/refunds", chunks=[b'{"id": "re_456"}'])

    @irimi.workflow
    def refund_ticket(amount: int) -> str:
        with http_library.Client() as client:
            answer = client.post(
                f"{upstream.url}/v1/refunds?expand=charge", json={"amount": amount}
            )
        return note_refund(answer.json())

    assert refund_ticket(5) == "re_456"

    run_start, call_start, call_end, note_start, _, run_end = recorded_events()

    assert parse_batch({"events": [call_start, call_end]}).refused == ()
    assert (call_start["kind"], call_start["name"], call_start["seq"]) == (
        "http",
        "POST 127.0.0.1/v1/refunds",
        1,
    )
    assert call_start["parent_span_id"] == run_start["span_id"]
    assert call_start["request"]["method"] == "POST"
    assert call_start["request"]["url"] == f"{upstream.url}/v1/refunds?expand=charge"
    assert call_start["request"]["body"] == {
        "encoding": "text",
        "content_type": "application/json",
        "data": '{"amount":5}',
    }
    assert (call_end["span_id"], call_end["status"], call_end["answered"]) == (
        call_start["span_id"],
        "ok",
        "live",
    )
    assert call_end["response"]["status"] == 200
    assert call_end["response"]["headers"]["Content-Type"] == "application/json"
    assert call_end["response"]["body"]["data"] == '{"id": "re_456"}'
    assert note_start["parent_span_id"] == run_start["span_id"]
    assert run_end["events_produced"] == 6


def test_an_async_client_is_recorded_the_same_way(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    @irimi.workflow
    async def look_up_both() -> list[int]:
        async with http_library.AsyncClient(base_url=upstream.url) as client:
            answers = await asyncio.gather(client.get("/a"), client.get("/b"))
        return [answer.status_code for answer in answers]

    assert asyncio.run(look_up_both()) == [200, 200]

    events = recorded_events()
    run_start = events[0]
    call_starts = [event for event in events if event.get("kind") == "http"]
    call_ends = [event for event in events if "response" in event]

    assert sorted(start["name"] for start in call_starts) == ["GET 127.0.0.1/a", "GET 127.0.0.1/b"]
    assert {start["parent_span_id"] for start in call_starts} == {run_start["span_id"]}
    assert sorted(end["span_id"] for end in call_ends) == sorted(
        start["span_id"] for start in call_starts
    )
    assert [end["response"]["status"] for end in call_ends] == [200, 200]


def test_a_call_outside_any_workflow_is_recorded_as_unattached_without_bodies(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/v1/logs", chunks=[b"first line\n", b"second line\n"])

    with http_library.Client() as client:
        client.post(f"{upstream.url}/v1/metrics", json={"cpu": 0.4})
        with client.stream("GET", f"{upstream.url}/v1/logs") as logs:
            next(logs.iter_raw())

    call_start, call_end, logs_start, logs_end = recorded_events()

    assert (call_start["kind"], call_start["seq"], call_start["parent_span_id"]) == (
        "http",
        0,
        None,
    )
    assert call_start["flags"] == ["unattached"]
    assert call_start["request"]["url"] == f"{upstream.url}/v1/metrics"
    assert "body" not in call_start["request"]
    assert call_end["response"]["status"] == 200
    assert "body" not in call_end["response"]
    assert "flags" not in call_end
    assert "events_produced" not in call_end
    assert logs_start["flags"] == ["unattached"]
    assert logs_end["response"]["status"] == 200
    assert "body" not in logs_end["response"]
    assert logs_end["flags"] == ["response_body_incomplete"]


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Cookie",
        "X-Api-Key",
        "x-api-key",
        "Api-Key",
        "Apikey",
        "X-Goog-Api-Key",
        "X-Auth-Token",
        "X-Auth-Key",
        "X-Amz-Security-Token",
        "Ocp-Apim-Subscription-Key",
        "X-Functions-Key",
        "Private-Token",
        "X-Vault-Token",
        "X-Shopify-Access-Token",
        "DD-API-KEY",
    ],
)
def test_the_value_of_a_credential_header_is_never_recorded(
    recorded_events: TakeEvents, upstream: Any, http_library: Any, name: str
) -> None:
    upstream.set_answer(
        "/charges",
        headers=[
            ("Set-Cookie", "session=abc"),
            ("Set-Cookie", "session=abc"),
            ("X-Note", "caf\xe9"),
        ],
    )

    @irimi.workflow
    def look_up_charges() -> Any:
        with http_library.Client() as client:
            return client.get(
                f"{upstream.url}/charges", headers={name: "sk_live_123", "Idempotency-Key": "k-1"}
            )

    answer = look_up_charges()

    events = recorded_events()
    _, call_start, call_end, _ = events

    assert call_start["request"]["headers"][name] == REDACTED
    assert call_start["request"]["headers"]["Idempotency-Key"] == "k-1"
    assert call_end["response"]["headers"]["Set-Cookie"] == REDACTED
    assert call_end["response"]["headers"]["X-Note"] == "caf\xe9"
    assert call_end["status"] == "ok"
    assert "sk_live_123" not in json.dumps(events)
    assert "session=abc" not in json.dumps(events)
    assert upstream.requests[-1] == ("GET", "/charges", b"")
    assert answer.headers["set-cookie"] == "session=abc, session=abc"


def test_a_header_sent_twice_is_recorded_once_with_its_values_joined(
    recorded_events: TakeEvents, upstream: Any, http_library: Any
) -> None:
    upstream.set_answer("/catalog", headers=[("Vary", "Accept"), ("Vary", "Origin")])

    @irimi.workflow
    def read_catalog() -> int:
        with http_library.Client() as client:
            return int(client.get(f"{upstream.url}/catalog").status_code)

    assert read_catalog() == 200

    _, _, call_end, _ = recorded_events()

    assert call_end["response"]["headers"]["Vary"] == "Accept, Origin"


def print_init_results(arguments: str) -> str:
    inherited = {name: value for name, value in os.environ.items() if not name.startswith("IRIMI_")}
    completed = subprocess.run(
        [sys.executable, "-c", INIT_TWICE.format(arguments=arguments)],
        env=inherited,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def test_init_reports_the_libraries_it_patched(fake_store: Any) -> None:
    patched = "patched=('httpx', 'httpx2'), unpatched=('urllib3', 'aiohttp')"

    assert print_init_results(f"store_url={fake_store.url!r}") == (
        f"InitResult(mode='normal', {patched})\n" * 2
    )
    assert print_init_results("") == "InitResult(mode='normal', patched=(), unpatched=())\n" * 2
