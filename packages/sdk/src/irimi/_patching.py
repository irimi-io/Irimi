from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Literal

from irimi._httpx_patch import patch_httpx
from irimi._interception import Interception

logger = logging.getLogger("irimi")

Patch = Callable[[ModuleType, Interception], bool]

LIBRARY_PATCHES: dict[str, Patch | None] = {
    "httpx": patch_httpx,
    "httpx2": patch_httpx,
    "urllib3": None,
    "aiohttp": None,
}
UNPATCHED_WARNING = "irimi could not patch %s, so calls made through it are not recorded"

PatchResult = Literal["patched", "unpatched", "not_installed"]


@dataclass(frozen=True)
class PatchReport:
    patched: tuple[str, ...]
    unpatched: tuple[str, ...]


def patch_installed_libraries(interception: Interception) -> PatchReport:
    results = {name: _patch_library(name, interception) for name in LIBRARY_PATCHES}
    return PatchReport(
        patched=tuple(name for name, result in results.items() if result == "patched"),
        unpatched=tuple(name for name, result in results.items() if result == "unpatched"),
    )


def _patch_library(name: str, interception: Interception) -> PatchResult:
    try:
        if importlib.util.find_spec(name) is None:
            return "not_installed"
        patch = LIBRARY_PATCHES[name]
        if patch is None:
            return "unpatched"
        is_patched = patch(importlib.import_module(name), interception)
    except Exception:
        logger.warning(UNPATCHED_WARNING, name, exc_info=True)
        return "unpatched"
    if not is_patched:
        logger.warning(UNPATCHED_WARNING, name)
        return "unpatched"
    return "patched"
