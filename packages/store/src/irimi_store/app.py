import json

from irimi._wire import (
    EVENTS_PATH,
    RUN_EVENTS_PATH,
    WIRE_VERSION,
    Batch,
    ParsedBatch,
    parse_batch,
    write_batch,
    write_refused_events,
)
from irimi._wire.field_names import NEXT_CURSOR
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from irimi_store.page_requests import InvalidPageRequestError, read_page_request
from irimi_store.pages import InvalidCursorError
from irimi_store.storage import StorageEngine

ACCEPTED_VERSIONS = (WIRE_VERSION,)
DEFAULT_PROJECT = "default"
DEFAULT_EVENT_PAGE_LIMIT = 500
LARGEST_EVENT_PAGE_LIMIT = 1000


def create_app(storage: StorageEngine) -> Starlette:
    async def ingest_events(request: Request) -> JSONResponse:
        try:
            parsed = await run_in_threadpool(_store_batch, storage, await request.body())
        except (ValueError, RecursionError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return JSONResponse(write_refused_events(parsed.refused))

    async def read_run_events(request: Request) -> JSONResponse:
        trace_id: str = request.path_params["trace_id"]
        try:
            page_request = read_page_request(
                request.query_params, DEFAULT_EVENT_PAGE_LIMIT, LARGEST_EVENT_PAGE_LIMIT
            )
            page = await run_in_threadpool(
                storage.read_run_events,
                DEFAULT_PROJECT,
                trace_id,
                page_request.cursor,
                page_request.limit,
            )
        except (InvalidPageRequestError, InvalidCursorError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        if not page.items and page_request.cursor is None:
            return JSONResponse({"error": f"no run with id {trace_id}"}, status_code=404)
        batch = Batch(events=page.items, fields={NEXT_CURSOR: page.next_cursor})
        return JSONResponse(write_batch(batch))

    return Starlette(
        routes=[
            Route(EVENTS_PATH, ingest_events, methods=["POST"]),
            Route(RUN_EVENTS_PATH, read_run_events, methods=["GET"]),
            Route("/{version}/events", refuse_unknown_version, methods=["POST"]),
        ]
    )


async def refuse_unknown_version(request: Request) -> JSONResponse:
    version = request.path_params["version"]
    return JSONResponse(
        {
            "error": f"unknown wire format version: {version}",
            "accepted_versions": list(ACCEPTED_VERSIONS),
        },
        status_code=404,
    )


def _store_batch(storage: StorageEngine, body: bytes) -> ParsedBatch:
    parsed = parse_batch(json.loads(body))
    storage.add_events(DEFAULT_PROJECT, parsed.batch.events)
    return parsed
