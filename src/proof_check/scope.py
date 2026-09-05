"""Declared-scope grammar, fail-closed refusals, and path-set handling.

The entry grammar is the one the policy schema adopts verbatim from prior art:
one entry per bullet line under a single ``## Allowlist`` markdown section
when the source is a PR body; each entry is an exact repository-relative path,
an ``fnmatch`` glob, or a directory prefix ending in ``/**``.

Fail-closed refusals (each raises ``ScopeRefusal``): bare ``*`` or ``**``; a
leading ``/``; a ``..`` segment; an empty prefix before ``/**``; any entry
broad enough to swallow the universal improbable probe path; no section, an
empty section, or duplicate sections in a PR body. Nothing here ever widens
what a declaration lets through.

Changed paths get the same suspicion: empty, absolute, traversing, NUL-bearing,
non-normal, or not-valid-Unicode paths and normalization collisions raise
``PathRefusal`` so the caller reports ``INDETERMINATE`` instead of matching.
"""

from __future__ import annotations

import enum
import fnmatch
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "ALLOWLIST_HEADING",
    "PathRefusal",
    "PathRefusalCode",
    "RefusalCode",
    "Scope",
    "ScopeEntry",
    "ScopeRefusal",
    "UNIVERSAL_PROBE",
    "changed_path_set",
    "compile_scope",
    "matches",
    "outside_paths",
    "parse_manifest",
    "parse_pr_body_allowlist",
    "refuse_entry",
]

#: An improbable path. Any entry that matches it is broad enough to swallow
#: arbitrary paths and is refused at runtime, whatever its surface shape.
UNIVERSAL_PROBE = "zz-improbable-probe/zz-deep/zz-path-7f3a.bin"

ALLOWLIST_HEADING = "## Allowlist"

_HEADING_RE = re.compile(r"^##\s*Allowlist\s*$", re.M)
_SECTION_RE = re.compile(r"^##\s*Allowlist\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RefusalCode(enum.StrEnum):
    NO_SECTION = "NO_SECTION"
    DUPLICATE_SECTION = "DUPLICATE_SECTION"
    EMPTY_SECTION = "EMPTY_SECTION"
    EMPTY_ENTRY = "EMPTY_ENTRY"
    BARE_WILDCARD = "BARE_WILDCARD"
    LEADING_SLASH = "LEADING_SLASH"
    PARENT_TRAVERSAL = "PARENT_TRAVERSAL"
    EMPTY_PREFIX = "EMPTY_PREFIX"
    CONTROL_CHARACTER = "CONTROL_CHARACTER"
    INVALID_ENCODING = "INVALID_ENCODING"
    UNIVERSAL_SWALLOW = "UNIVERSAL_SWALLOW"


class ScopeRefusal(ValueError):
    """The declaration is refused; the caller must not treat it as a scope."""

    def __init__(self, code: RefusalCode, message: str, entry: str | None = None):
        self.code = code
        self.entry = entry
        super().__init__(message if entry is None else f"{message}: {entry!r}")


class PathRefusalCode(enum.StrEnum):
    EMPTY = "EMPTY"
    ABSOLUTE = "ABSOLUTE"
    PARENT_TRAVERSAL = "PARENT_TRAVERSAL"
    CONTROL_CHARACTER = "CONTROL_CHARACTER"
    INVALID_ENCODING = "INVALID_ENCODING"
    NON_NORMAL = "NON_NORMAL"
    NORMALIZATION_COLLISION = "NORMALIZATION_COLLISION"


class PathRefusal(ValueError):
    """A changed path cannot be matched honestly; the caller must not decide."""

    def __init__(self, code: PathRefusalCode, message: str, path: str | None = None):
        self.code = code
        self.path = path
        super().__init__(message if path is None else f"{message}: {_portable(path)!r}")


def _portable(text: str) -> str:
    return text.encode("utf-8", "backslashreplace").decode("ascii", "backslashreplace")


# --------------------------------------------------------------------------- declarations


def parse_pr_body_allowlist(body: str) -> tuple[str, ...]:
    """Return the entries of the single ``## Allowlist`` section of a PR body.

    Entries are bullet lines (``- `` or ``* ``), optionally wrapped in
    backticks. Prose lines inside the section are ignored. Zero sections,
    duplicate sections, or a section without entries raise ``ScopeRefusal``.
    """
    headings = _HEADING_RE.findall(body)
    if not headings:
        raise ScopeRefusal(RefusalCode.NO_SECTION, f"no `{ALLOWLIST_HEADING}` section in the declaration")
    if len(headings) > 1:
        raise ScopeRefusal(
            RefusalCode.DUPLICATE_SECTION,
            f"exactly one `{ALLOWLIST_HEADING}` section is allowed, found {len(headings)}",
        )
    match = _SECTION_RE.search(body)
    assert match is not None
    entries: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            entry = stripped[2:].strip().strip("`").strip()
            if entry:
                entries.append(entry)
    if not entries:
        raise ScopeRefusal(RefusalCode.EMPTY_SECTION, f"the `{ALLOWLIST_HEADING}` section carries no entries")
    return tuple(entries)


def parse_manifest(text: str) -> tuple[str, ...]:
    """Return the entries of a plain manifest: one entry per line.

    Blank lines and lines starting with ``#`` are ignored. A manifest without
    entries raises ``ScopeRefusal`` (``EMPTY_SECTION``).
    """
    entries = tuple(
        stripped for stripped in (line.strip() for line in text.splitlines()) if stripped and not stripped.startswith("#")
    )
    if not entries:
        raise ScopeRefusal(RefusalCode.EMPTY_SECTION, "the manifest carries no entries")
    return entries


# --------------------------------------------------------------------------- entries


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    """One accepted entry. ``kind`` is ``exact``, ``glob`` or ``prefix``."""

    text: str
    kind: str

    def covers(self, path: str) -> bool:
        return matches(path, self.text)


@dataclass(frozen=True, slots=True)
class Scope:
    entries: tuple[ScopeEntry, ...]

    def covers(self, path: str) -> bool:
        return any(entry.covers(path) for entry in self.entries)


def refuse_entry(entry: str) -> None:
    """Raise ``ScopeRefusal`` if ``entry`` violates the grammar; return nothing otherwise."""
    if not entry or not entry.strip():
        raise ScopeRefusal(RefusalCode.EMPTY_ENTRY, "empty entry")
    try:
        entry.encode("utf-8")
    except UnicodeEncodeError:
        raise ScopeRefusal(RefusalCode.INVALID_ENCODING, "entry is not valid Unicode") from None
    if _CONTROL_RE.search(entry):
        raise ScopeRefusal(RefusalCode.CONTROL_CHARACTER, "entry contains a control character", entry)
    if entry in {"*", "**"}:
        raise ScopeRefusal(RefusalCode.BARE_WILDCARD, "bare wildcard swallows every path", entry)
    if entry.startswith("/"):
        raise ScopeRefusal(RefusalCode.LEADING_SLASH, "leading slash is not a repository-relative path", entry)
    if ".." in entry.split("/"):
        raise ScopeRefusal(RefusalCode.PARENT_TRAVERSAL, "parent traversal is forbidden", entry)
    if entry.endswith("/**") and not entry[:-3].strip():
        raise ScopeRefusal(RefusalCode.EMPTY_PREFIX, "directory prefix must be non-empty", entry)
    if matches(UNIVERSAL_PROBE, entry):
        raise ScopeRefusal(RefusalCode.UNIVERSAL_SWALLOW, "entry is broad enough to swallow an arbitrary path", entry)


def compile_scope(entries: Iterable[str]) -> Scope:
    """Validate every entry and return a ``Scope``. The refusal of any entry refuses the whole scope."""
    compiled: list[ScopeEntry] = []
    for entry in entries:
        refuse_entry(entry)
        if entry.endswith("/**"):
            kind = "prefix"
        elif any(ch in entry for ch in "*?["):
            kind = "glob"
        else:
            kind = "exact"
        compiled.append(ScopeEntry(entry, kind))
    if not compiled:
        raise ScopeRefusal(RefusalCode.EMPTY_SECTION, "a scope needs at least one entry")
    return Scope(tuple(compiled))


def matches(path: str, entry: str) -> bool:
    """Prior-art matching: ``<prefix>/**`` is a directory prefix, otherwise exact or fnmatch.

    ``fnmatch`` wildcards match ``/`` as well, so ``*.md`` covers ``docs/a.md``.
    Matching is case-sensitive on every platform.
    """
    if entry.endswith("/**"):
        return path.startswith(entry[:-2])
    return path == entry or fnmatch.fnmatchcase(path, entry)


# --------------------------------------------------------------------------- changed paths


def _refuse_path(path: str) -> None:
    if path == "":
        raise PathRefusal(PathRefusalCode.EMPTY, "empty path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise PathRefusal(PathRefusalCode.INVALID_ENCODING, "path is not valid Unicode", path) from None
    if _CONTROL_RE.search(path):
        raise PathRefusal(PathRefusalCode.CONTROL_CHARACTER, "path contains a control character", path)
    if path.startswith("/"):
        raise PathRefusal(PathRefusalCode.ABSOLUTE, "absolute path is not repository-relative", path)
    segments = path.split("/")
    if ".." in segments:
        raise PathRefusal(PathRefusalCode.PARENT_TRAVERSAL, "path traverses a parent directory", path)
    if "." in segments or "" in segments:
        raise PathRefusal(PathRefusalCode.NON_NORMAL, "path is not in normal form", path)


def changed_path_set(records: Iterable[tuple[str, str | None]]) -> tuple[str, ...]:
    """Return the sorted, deduplicated set of every path a change touches.

    ``records`` yields ``(filename, previous_filename)`` pairs; a rename
    contributes both its old and its new path. Every path is checked with
    ``_refuse_path``; two distinct paths that agree after Unicode NFC
    normalization are a collision and raise ``PathRefusal``.
    """
    paths: set[str] = set()
    for filename, previous in records:
        paths.add(filename)
        if previous is not None:
            paths.add(previous)
    for path in sorted(paths):
        _refuse_path(path)
    seen: dict[str, str] = {}
    for path in sorted(paths):
        key = unicodedata.normalize("NFC", path)
        if key in seen and seen[key] != path:
            raise PathRefusal(
                PathRefusalCode.NORMALIZATION_COLLISION,
                f"paths {_portable(seen[key])!r} and {_portable(path)!r} collide after normalization",
            )
        seen[key] = path
    return tuple(sorted(paths))


def outside_paths(scope: Scope, paths: Iterable[str]) -> tuple[str, ...]:
    """Return, sorted, every path in ``paths`` that no scope entry covers."""
    return tuple(sorted(path for path in set(paths) if not scope.covers(path)))
