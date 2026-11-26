from __future__ import annotations

from pathlib import Path

REFERENCE_PREFIX = "ref: "


def find_commit(directory: Path) -> str | None:
    for candidate in (directory, *directory.parents):
        git_directory = candidate / ".git"
        if git_directory.is_dir():
            return _read_head_commit(git_directory)
    return None


def _read_head_commit(git_directory: Path) -> str | None:
    try:
        head = (git_directory / "HEAD").read_text().strip()
        if not head.startswith(REFERENCE_PREFIX):
            return head or None
        return _read_reference(git_directory, head[len(REFERENCE_PREFIX) :])
    except OSError:
        return None


def _read_reference(git_directory: Path, reference: str) -> str | None:
    reference_file = git_directory / reference
    if reference_file.is_file():
        return reference_file.read_text().strip() or None
    packed_references = git_directory / "packed-refs"
    if not packed_references.is_file():
        return None
    for line in packed_references.read_text().splitlines():
        commit, _, name = line.partition(" ")
        if name == reference:
            return commit
    return None
