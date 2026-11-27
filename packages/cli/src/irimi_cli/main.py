import argparse
import json
import os
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

from irimi_store.reader_documents import RUN_PATH, RUNS_PATH, parse_run, parse_run_page
from irimi_store.server import LOCAL_HOST, serve_local_store

from irimi_cli.printing import print_run_list, print_run_tree
from irimi_cli.store_client import read_from_store

STORE_URL_VARIABLE = "IRIMI_STORE_URL"
DEFAULT_STORE_PORT = 7474
DEFAULT_STORE_DATABASE = Path("irimi-store.db")
USAGE_ERROR = 2


def main(arguments: Sequence[str] | None = None) -> int:
    options = _build_parser().parse_args(arguments)
    if options.command == "store":
        return _start_store(options.database, options.port)
    store_url = options.store_url or os.environ.get(STORE_URL_VARIABLE)
    if not store_url:
        print(f"no store address: pass --store-url or set {STORE_URL_VARIABLE}", file=sys.stderr)
        return USAGE_ERROR
    if options.run_id is None:
        return _list_runs(store_url, options.limit, options.cursor, options.json)
    return _show_run(store_url, options.run_id, options.json)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="irimi")
    commands = parser.add_subparsers(dest="command", required=True)
    runs = commands.add_parser("runs", help="list recorded runs, or show one")
    runs.add_argument("run_id", nargs="?", help="show this run as a tree")
    runs.add_argument("--json", action="store_true", help="print the store's own document")
    runs.add_argument("--limit", help="how many runs to list")
    runs.add_argument("--cursor", help="the next page, as a list's last line names it")
    runs.add_argument("--store-url", help=f"the store's address, else {STORE_URL_VARIABLE}")
    store = commands.add_parser("store", help="start a local store")
    store.add_argument("--port", type=int, default=DEFAULT_STORE_PORT)
    store.add_argument("--database", type=Path, default=DEFAULT_STORE_DATABASE)
    return parser


def _start_store(database_path: Path, port: int) -> int:
    store_url = f"http://{LOCAL_HOST}:{port}"
    print(store_url)
    print(f"set {STORE_URL_VARIABLE}={store_url} to record to this store and read from it")
    sys.stdout.flush()
    serve_local_store(database_path, port)
    return 0


def _list_runs(store_url: str, limit: str | None, cursor: str | None, as_json: bool) -> int:
    query = {name: value for name, value in (("limit", limit), ("cursor", cursor)) if value}
    document = read_from_store(store_url, RUNS_PATH, query)
    if as_json:
        return _write_document(document)
    print(print_run_list(parse_run_page(json.loads(document))))
    return 0


def _show_run(store_url: str, run_id: str, as_json: bool) -> int:
    path = RUN_PATH.format(trace_id=urllib.parse.quote(run_id, safe=""))
    document = read_from_store(store_url, path, {})
    if as_json:
        return _write_document(document)
    print(print_run_tree(parse_run(json.loads(document))))
    return 0


def _write_document(document: bytes) -> int:
    sys.stdout.buffer.write(document)
    sys.stdout.buffer.flush()
    return 0
