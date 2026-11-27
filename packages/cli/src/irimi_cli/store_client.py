import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

SECONDS_TO_WAIT_FOR_THE_STORE = 30.0


class StoreError(Exception):
    pass


def read_from_store(store_url: str, path: str, query: Mapping[str, str]) -> bytes:
    address = store_url.rstrip("/") + path
    if query:
        address += "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(address, timeout=SECONDS_TO_WAIT_FOR_THE_STORE) as answer:
            body: bytes = answer.read()
    except urllib.error.HTTPError as error:
        raise StoreError(_read_refusal(error, store_url)) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise StoreError(f"could not reach the store at {store_url}: {error}") from error
    return body


def _read_refusal(error: urllib.error.HTTPError, store_url: str) -> str:
    try:
        document = json.loads(error.read())
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("error"), str):
        return str(document["error"])
    return f"the store at {store_url} answered {error.code}"
