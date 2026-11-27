import json

from irimi._wire import (
    EVENTS_PATH,
    RUN_EVENTS_PATH,
    WIRE_VERSION,
    Batch,
    Event,
    ParsedBatch,
    parse_batch,
    write_batch,
    write_refused_events,
)
from irimi._wire.field_names import NEXT_CURSOR
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

from irimi_store.answers import JsonAnswer
from irimi_store.assembly import RunTooDeepError
from irimi_store.page_requests import (
    InvalidPageRequestError,
    PageRequest,
    read_page_request,
)
from irimi_store.pages import InvalidCursorError, Page
from irimi_store.reader_documents import write_refusal
from irimi_store.reader_routes import create_reader_routes
from irimi_store.storage import DEFAULT_PROJECT, NotHeldError, StorageEngine

ACCEPTED_VERSIONS = (WIRE_VERSION,)
DEFAULT_EVENT_PAGE_LIMIT = 500
LARGEST_EVENT_PAGE_LIMIT = 1000


def create_app(storage: StorageEngine) -> Starlette:
    async def ingest_events(request: Request) -> JsonAnswer:
        try:
            parsed = await run_in_threadpool(_store_batch, storage, await request.body())
        except (ValueError, RecursionError) as error:
            return JsonAnswer(write_refusal(str(error)), status_code=400)
        return JsonAnswer(write_refused_events(parsed.refused))

    async def read_run_events(request: Request) -> JsonAnswer:
        trace_id: str = request.path_params["trace_id"]
        page_request = read_page_request(
            request.query_params, DEFAULT_EVENT_PAGE_LIMIT, LARGEST_EVENT_PAGE_LIMIT
        )
        page = await run_in_threadpool(_read_event_page, storage, trace_id, page_request)
        batch = Batch(events=page.items, fields={NEXT_CURSOR: page.next_cursor})
        return JsonAnswer(write_batch(batch))

    return Starlette(
        routes=[
            Route(EVENTS_PATH, ingest_events, methods=["POST"]),
            Route(RUN_EVENTS_PATH, read_run_events, methods=["GET"]),
            Route("/{version}/events", refuse_unknown_version, methods=["POST"]),
            Route("/{version}/runs/{trace_id}/events", refuse_unknown_version, methods=["GET"]),
            *create_reader_routes(storage),
        ],
        exception_handlers={
            InvalidPageRequestError: refuse_request,
            InvalidCursorError: refuse_request,
            NotHeldError: answer_not_held,
            RunTooDeepError: refuse_run_too_deep,
        },
    )


async def refuse_request(request: Request, error: Exception) -> JsonAnswer:
    return JsonAnswer(write_refusal(str(error)), status_code=400)


async def answer_not_held(request: Request, error: Exception) -> JsonAnswer:
    return JsonAnswer(write_refusal(str(error)), status_code=404)


async def refuse_run_too_deep(request: Request, error: Exception) -> JsonAnswer:
    events_path = RUN_EVENTS_PATH.format(**request.path_params)
    reason = f"this run is {error}; read its events at {events_path}"
    return JsonAnswer(write_refusal(reason), status_code=422)


async def refuse_unknown_version(request: Request) -> JsonAnswer:
    version = request.path_params["version"]
    return JsonAnswer(
        {
            **write_refusal(f"unknown wire format version: {version}"),
            "accepted_versions": list(ACCEPTED_VERSIONS),
        },
        status_code=404,
    )


def _store_batch(storage: StorageEngine, body: bytes) -> ParsedBatch:
    parsed = parse_batch(json.loads(body, parse_constant=_refuse_number_that_is_not_json))
    storage.add_events(DEFAULT_PROJECT, parsed.batch.events)
    return parsed


def _refuse_number_that_is_not_json(number: str) -> None:
    raise ValueError(f"{number} is not JSON")


def _read_event_page(
    storage: StorageEngine, trace_id: str, page_request: PageRequest
) -> Page[Event]:
    page = storage.read_run_events(
        DEFAULT_PROJECT, trace_id, page_request.cursor, page_request.limit
    )
    if page.items:
        return page
    is_first_page = page_request.cursor is None
    if is_first_page or not storage.read_run_events(DEFAULT_PROJECT, trace_id, None, 1).items:
        raise NotHeldError(f"no run with id {trace_id}")
    return page
