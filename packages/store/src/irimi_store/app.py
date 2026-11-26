import json

from irimi._wire import (
    EVENTS_PATH,
    WIRE_VERSION,
    ParsedBatch,
    parse_batch,
    write_refused_events,
)
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from irimi_store.storage import StorageEngine

ACCEPTED_VERSIONS = (WIRE_VERSION,)
DEFAULT_PROJECT = "default"


def create_app(storage: StorageEngine) -> Starlette:
    async def ingest_events(request: Request) -> JSONResponse:
        try:
            parsed = await run_in_threadpool(_store_batch, storage, await request.body())
        except (ValueError, RecursionError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return JSONResponse(write_refused_events(parsed.refused))

    return Starlette(
        routes=[
            Route(EVENTS_PATH, ingest_events, methods=["POST"]),
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
