import json
import os
import subprocess
import sys
import urllib.request
from typing import Any

APP = """
import irimi

irimi.init()

@irimi.function(write=True)
def record_refund(ticket_id):
    return {"row_id": 9917}

@irimi.workflow
def handle_ticket(ticket):
    record_refund(ticket["id"])
    return "Refund issued"

handle_ticket({"id": "T-1042", "text": "Charged twice"})
"""


def test_a_run_recorded_in_an_app_is_listed_and_shown_by_the_cli(
    local_store_url: str, run_irimi: Any
) -> None:
    subprocess.run(
        [sys.executable, "-c", APP],
        env={**os.environ, "IRIMI_STORE_URL": local_store_url},
        timeout=60,
        check=True,
    )

    listed = run_irimi(local_store_url, "runs", "--json")
    run_id = json.loads(listed.stdout)["runs"][0]["id"]
    shown = run_irimi(local_store_url, "runs", run_id, "--json")
    root = json.loads(shown.stdout)["run"]["root"]
    table = run_irimi(local_store_url, "runs")
    tree = run_irimi(local_store_url, "runs", run_id)
    with urllib.request.urlopen(f"{local_store_url}/api/v1/runs") as answer:
        store_document = answer.read()

    assert (listed.returncode, shown.returncode, table.returncode, tree.returncode) == (0, 0, 0, 0)
    assert listed.stdout.encode() == store_document
    assert (root["name"], root["output"], root["children"][0]["name"]) == (
        "handle_ticket",
        "Refund issued",
        "record_refund",
    )
    assert run_id in table.stdout
    assert "handle_ticket" in table.stdout
    assert tree.stdout.index("handle_ticket") < tree.stdout.index("  function record_refund")
