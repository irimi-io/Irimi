import argparse
import json
import os
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from irimi_store.reader_documents import (
    RUN_PATH,
    RUNS_PATH,
    ReaderDocumentError,
    parse_run,
    parse_run_page,
)
from irimi_store.server import LocalStoreError, serve_local_store

from irimi_cli.printing import print_run_list, print_run_tree
from irimi_cli.store_client import StoreError, read_from_store

Answer = TypeVar("Answer")

STORE_URL_VARIABLE = "IRIMI_STORE_URL"
DEFAULT_STORE_PORT = 7717
DEFAULT_STORE_DATABASE = Path("irimi-store.db")
USAGE_ERROR = 2
STORE_ERROR = 1


def main(arguments: Sequence[str] | None = None) -> int:
    options = _build_parser().parse_args(arguments)
    try:
        return _run_command(options)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (StoreError, LocalStoreError) as error:
        print(error, file=sys.stderr)
        return STORE_ERROR


def _run_command(options: argparse.Namespace) -> int:
    if options.command == "store":
        serve_local_store(
            options.database, options.port, partial(_announce_store, options.database)
        )
        return 0
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


def _announce_store(database_path: Path, store_url: str) -> None:
    how_to_use_it = f"set {STORE_URL_VARIABLE}={store_url} to record to this store and read from it"
    _write_text(f"{store_url}\n{how_to_use_it}\nkeeping runs in {database_path.resolve()}")


def _list_runs(store_url: str, limit: str | None, cursor: str | None, as_json: bool) -> int:
    asked = (("limit", limit), ("cursor", cursor))
    query = {name: value for name, value in asked if value is not None}
    document = read_from_store(store_url, RUNS_PATH, query)
    if as_json:
        return _write_document(document)
    return _write_text(_print_answer(store_url, document, parse_run_page, print_run_list))


def _show_run(store_url: str, run_id: str, as_json: bool) -> int:
    path = RUN_PATH.format(trace_id=urllib.parse.quote(run_id, safe=""))
    document = read_from_store(store_url, path, {})
    if as_json:
        return _write_document(document)
    return _write_text(_print_answer(store_url, document, parse_run, print_run_tree))


def _print_answer(
    store_url: str,
    document: bytes,
    parse: Callable[[Any], Answer],
    print_answer: Callable[[Answer], str],
) -> str:
    try:
        return print_answer(parse(json.loads(document)))
    except (ReaderDocumentError, ValueError, OverflowError, OSError, RecursionError) as error:
        raise StoreError(f"{store_url} did not answer as a store does: {error}") from error


def _write_text(text: str) -> int:
    print(text)
    sys.stdout.flush()
    return 0


def _write_document(document: bytes) -> int:
    sys.stdout.buffer.write(document)
    sys.stdout.buffer.flush()
    return 0
