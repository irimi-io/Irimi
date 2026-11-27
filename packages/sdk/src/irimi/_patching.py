from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass

from irimi._httpx_patch import patch_httpx
from irimi._interception import Interception

logger = logging.getLogger("irimi")

LIBRARY_NAMES = ("httpx", "httpx2", "urllib3", "aiohttp")
LIBRARIES_LIKE_HTTPX = frozenset({"httpx", "httpx2"})
UNPATCHED_WARNING = "irimi could not patch %s, so calls made through it are not recorded"


@dataclass(frozen=True)
class PatchReport:
    patched: tuple[str, ...]
    unpatched: tuple[str, ...]


def patch_installed_libraries(interception: Interception) -> PatchReport:
    patched: list[str] = []
    unpatched: list[str] = []
    for name in LIBRARY_NAMES:
        if importlib.util.find_spec(name) is None:
            continue
        if _patch_library(name, interception):
            patched.append(name)
        else:
            unpatched.append(name)
    return PatchReport(patched=tuple(patched), unpatched=tuple(unpatched))


def _patch_library(name: str, interception: Interception) -> bool:
    if name not in LIBRARIES_LIKE_HTTPX:
        return False
    try:
        is_patched = patch_httpx(importlib.import_module(name), interception)
    except Exception:
        logger.warning(UNPATCHED_WARNING, name, exc_info=True)
        return False
    if not is_patched:
        logger.warning(UNPATCHED_WARNING, name)
    return is_patched
