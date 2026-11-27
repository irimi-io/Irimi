from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from typing import Literal

from irimi._httpx_patch import patch_httpx
from irimi._interception import Interception

logger = logging.getLogger("irimi")

LIBRARY_NAMES = ("httpx", "httpx2", "urllib3", "aiohttp")
LIBRARIES_LIKE_HTTPX = frozenset({"httpx", "httpx2"})
UNPATCHED_WARNING = "irimi could not patch %s, so calls made through it are not recorded"

PatchResult = Literal["patched", "unpatched", "not_installed"]


@dataclass(frozen=True)
class PatchReport:
    patched: tuple[str, ...]
    unpatched: tuple[str, ...]


def patch_installed_libraries(interception: Interception) -> PatchReport:
    results = {name: _patch_library(name, interception) for name in LIBRARY_NAMES}
    return PatchReport(
        patched=tuple(name for name, result in results.items() if result == "patched"),
        unpatched=tuple(name for name, result in results.items() if result == "unpatched"),
    )


def _patch_library(name: str, interception: Interception) -> PatchResult:
    try:
        if importlib.util.find_spec(name) is None:
            return "not_installed"
        if name not in LIBRARIES_LIKE_HTTPX:
            return "unpatched"
        is_patched = patch_httpx(importlib.import_module(name), interception)
    except Exception:
        logger.warning(UNPATCHED_WARNING, name, exc_info=True)
        return "unpatched"
    if not is_patched:
        logger.warning(UNPATCHED_WARNING, name)
        return "unpatched"
    return "patched"
