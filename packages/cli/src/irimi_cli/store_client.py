import urllib.parse
import urllib.request
from collections.abc import Mapping

SECONDS_TO_WAIT_FOR_THE_STORE = 30.0


def read_from_store(store_url: str, path: str, query: Mapping[str, str]) -> bytes:
    address = store_url.rstrip("/") + path
    if query:
        address += "?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(address, timeout=SECONDS_TO_WAIT_FOR_THE_STORE) as answer:
        body: bytes = answer.read()
    return body
