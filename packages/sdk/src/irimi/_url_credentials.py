from __future__ import annotations

from urllib.parse import urlsplit


def remove_userinfo(url: str) -> str:
    address = urlsplit(url).netloc
    _, at_sign, host_and_port = address.rpartition("@")
    if not at_sign:
        return url
    return url.replace(address, host_and_port, 1)
