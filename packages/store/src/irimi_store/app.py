import json

from irimi._wire import ParsedBatch, parse_batch
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from irimi_store.storage import StorageEngine

ACCEPTED_VERSIONS = ("v1",)
DEFAULT_PROJECT = "default"


def create_app(storage: StorageEngine) -> Starlette:
    async def ingest_events(request: Request) -> JSONResponse:
        try:
            parsed = parse_batch(json.loads(await request.body()))
        except (ValueError, RecursionError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        await run_in_threadpool(storage.add_events, DEFAULT_PROJECT, parsed.batch.events)
        return JSONResponse(_describe_refused_events(parsed))

    return Starlette(
        routes=[
            Route("/v1/events", ingest_events, methods=["POST"]),
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


def _describe_refused_events(parsed: ParsedBatch) -> dict[str, list[dict[str, str | int]]]:
    return {
        "refused": [
            {"index": refused.index, "reason": refused.reason} for refused in parsed.refused
        ]
    }
