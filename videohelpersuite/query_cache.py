"""Bounded, authorization-aware metadata cache for query routes."""

from __future__ import annotations

import copy
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from .path_policy import AuthorizedPath, PathAccessDenied, PathPolicy


@dataclass(frozen=True)
class _FileState:
    mtime_ns: int
    ctime_ns: int
    size: int
    inode: int


@dataclass(frozen=True)
class _CacheEntry:
    state: _FileState
    source: dict[str, Any]


def _identity(path: str) -> str:
    """Return a comparison-only identity without changing presentation paths."""
    return os.path.normcase(os.path.abspath(path))


def _state(path: str) -> _FileState:
    stat_result = os.stat(path)
    return _FileState(
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_size,
        stat_result.st_ino,
    )


class AuthorizedMetadataCache:
    """Small LRU whose hits and writes require current path authorization."""

    def __init__(self, max_entries: int = 128):
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def get(
        self,
        authorized: AuthorizedPath,
        policy: PathPolicy,
    ) -> dict[str, Any] | None:
        prior_identity = _identity(authorized.canonical)
        try:
            current = policy.reauthorize_path(authorized)
        except (PathAccessDenied, OSError):
            # CRITICAL: a denied or vanished cache hit must never retain reusable metadata.
            self._entries.pop(prior_identity, None)
            raise

        identity = _identity(current.canonical)
        if identity != prior_identity:
            self._entries.pop(prior_identity, None)
        entry = self._entries.get(identity)
        if entry is None:
            return None
        try:
            current_state = _state(current.canonical)
        except OSError:
            self._entries.pop(identity, None)
            raise
        if entry.state != current_state:
            self._entries.pop(identity, None)
            return None
        self._entries.move_to_end(identity)
        return copy.deepcopy(entry.source)

    def put(
        self,
        authorized: AuthorizedPath,
        source: Mapping[str, Any],
        policy: PathPolicy,
    ) -> None:
        prior_identity = _identity(authorized.canonical)
        try:
            current = policy.reauthorize_path(authorized)
        except (PathAccessDenied, OSError):
            self._entries.pop(prior_identity, None)
            raise

        identity = _identity(current.canonical)
        if identity != prior_identity:
            self._entries.pop(prior_identity, None)
        if self._max_entries == 0:
            self._entries.pop(identity, None)
            return
        self._entries[identity] = _CacheEntry(
            state=_state(current.canonical),
            source=copy.deepcopy(dict(source)),
        )
        self._entries.move_to_end(identity)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
