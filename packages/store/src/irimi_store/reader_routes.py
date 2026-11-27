from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from irimi_store.assembly import summarise_root
from irimi_store.page_requests import read_page_request
from irimi_store.pages import Page
from irimi_store.reader_documents import (
    READER_API_VERSION,
    RUNS_PATH,
    UNATTACHED_CALLS_PATH,
    RunSummary,
    write_run_page,
    write_unattached_call_page,
)
from irimi_store.storage import DEFAULT_PROJECT, RootSet, StorageEngine

ACCEPTED_READER_API_VERSIONS = (READER_API_VERSION,)
DEFAULT_LIST_PAGE_LIMIT = 50
LARGEST_LIST_PAGE_LIMIT = 200


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

    return [
        Route(RUNS_PATH, list_runs, methods=["GET"]),
        Route(UNATTACHED_CALLS_PATH, list_unattached_calls, methods=["GET"]),
        Route("/api/{version}/{rest:path}", refuse_unknown_reader_api_version, methods=["GET"]),
    ]


async def refuse_unknown_reader_api_version(request: Request) -> JSONResponse:
    version = request.path_params["version"]
    return JSONResponse(
        {
            "error": f"unknown reader API version: {version}",
            "accepted_versions": list(ACCEPTED_READER_API_VERSIONS),
        },
        status_code=404,
    )
