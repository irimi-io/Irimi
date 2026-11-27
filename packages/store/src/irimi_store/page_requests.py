from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PageRequest:
    cursor: str | None
    limit: int


class InvalidPageRequestError(ValueError):
    pass


def read_page_request(
    query: Mapping[str, str], default_limit: int, largest_limit: int
) -> PageRequest:
    return PageRequest(
        cursor=query.get("cursor"),
        limit=_read_limit(query.get("limit"), default_limit, largest_limit),
    )


def _read_limit(text: str | None, default_limit: int, largest_limit: int) -> int:
    if text is None:
        return default_limit
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= largest_limit:
        raise InvalidPageRequestError(f"limit: expected a whole number from 1 to {largest_limit}")
    return int(text)
