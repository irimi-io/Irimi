from irimi._wire import Event
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from irimi_store.assembly import assemble_call, assemble_run, summarise_root
from irimi_store.page_requests import read_page_request
from irimi_store.pages import Page
from irimi_store.reader_documents import (
    CALL_PATH,
    READER_API_VERSION,
    RUN_PATH,
    RUNS_PATH,
    UNATTACHED_CALLS_PATH,
    RunSummary,
    write_call,
    write_refusal,
    write_run,
    write_run_page,
    write_unattached_call_page,
)
from irimi_store.storage import DEFAULT_PROJECT, NotHeldError, RootSet, StorageEngine

ACCEPTED_READER_API_VERSIONS = (READER_API_VERSION,)
DEFAULT_LIST_PAGE_LIMIT = 50
LARGEST_LIST_PAGE_LIMIT = 200
ASSEMBLY_PAGE_LIMIT = 1000


def create_reader_routes(storage: StorageEngine) -> list[Route]:
    async def list_summaries(request: Request, root_set: RootSet) -> Page[RunSummary]:
        page_request = read_page_request(
            request.query_params, DEFAULT_LIST_PAGE_LIMIT, LARGEST_LIST_PAGE_LIMIT
        )
        page = await run_in_threadpool(
            storage.list_roots, DEFAULT_PROJECT, root_set, page_request.cursor, page_request.limit
        )
        return Page(
            items=tuple(summarise_root(root) for root in page.items), next_cursor=page.next_cursor
        )

    async def list_runs(request: Request) -> JSONResponse:
        return JSONResponse(write_run_page(await list_summaries(request, "runs")))

    async def list_unattached_calls(request: Request) -> JSONResponse:
        page = await list_summaries(request, "unattached_calls")
        return JSONResponse(write_unattached_call_page(page))

    async def read_run(request: Request) -> JSONResponse:
        trace_id: str = request.path_params["trace_id"]
        events = await run_in_threadpool(_read_every_run_event, storage, trace_id)
        if not events:
            raise NotHeldError(f"no run with id {trace_id}")
        return JSONResponse(write_run(assemble_run(trace_id, events)))

    async def read_call(request: Request) -> JSONResponse:
        trace_id: str = request.path_params["trace_id"]
        span_id: str = request.path_params["span_id"]
        events = await run_in_threadpool(
            storage.read_call_events, DEFAULT_PROJECT, trace_id, span_id
        )
        call = assemble_call(events)
        if call is None:
            raise NotHeldError(f"no call with id {span_id} in run {trace_id}")
        return JSONResponse(write_call(call))

    return [
        Route(RUNS_PATH, list_runs, methods=["GET"]),
        Route(RUN_PATH, read_run, methods=["GET"]),
        Route(CALL_PATH, read_call, methods=["GET"]),
        Route(UNATTACHED_CALLS_PATH, list_unattached_calls, methods=["GET"]),
        Route("/api/{version}/{rest:path}", refuse_unknown_reader_api_path, methods=["GET"]),
    ]


def _read_every_run_event(storage: StorageEngine, trace_id: str) -> tuple[Event, ...]:
    events: list[Event] = []
    cursor: str | None = None
    while True:
        page = storage.read_run_events(DEFAULT_PROJECT, trace_id, cursor, ASSEMBLY_PAGE_LIMIT)
        events.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return tuple(events)


async def refuse_unknown_reader_api_path(request: Request) -> JSONResponse:
    version = request.path_params["version"]
    if version in ACCEPTED_READER_API_VERSIONS:
        refusal = write_refusal(f"nothing is served at {request.url.path}")
        return JSONResponse(refusal, status_code=404)
    return JSONResponse(
        {
            **write_refusal(f"unknown reader API version: {version}"),
            "accepted_versions": list(ACCEPTED_READER_API_VERSIONS),
        },
        status_code=404,
    )
