"""Shared bounded recursive walk for the coder workspace.

Both the in-container `_list_workspace_projects` and the host-side
`CoderRuntimeManager._discover_projects` must agree on which directories
under `coder.workspace_root` count as audit-able projects. This module
holds the single implementation; both call sites import `walk_projects`
so the two views cannot drift.

Discovery rules:

- Walk depth-first, descending the entire tree. The walk has no depth
  cap — operators with deeply nested layouts (org/team/sub/repo and
  beyond) are not arbitrarily truncated.
- Cap total entries inspected at ``entry_budget`` (default ``10_000``)
  so a pathological tree cannot stall the scan.
- Cap total wall-clock time at ``time_budget_seconds`` (default
  ``3.0`` seconds). On slow filesystems (vmhgfs/9p shared mounts,
  remote NFS, FUSE), a single ``os.scandir`` can cost ~40 ms; without
  a wall-clock cap a deep tree on such a mount easily exceeds the
  HTTP preflight timeout. When the time budget is exhausted, the walk
  returns the projects it found so far with ``truncated=True``.
- At each entry: skip dotfile-prefixed and ``__``-prefixed names;
  require ``entry.is_dir(follow_symlinks=True)``; track visited
  symlink entries by ``os.path.realpath`` to break symlink loops
  (non-symlink entries cannot form loops, so realpath is skipped on
  them — saves N stat() calls on the common path).
- Every directory at every depth is yielded as a project, identified
  by its ``/``-joined path relative to ``root`` (e.g. ``"alpha"`` for
  depth-1, ``"team/repo"`` for depth-2, ``"org/team/sub/repo"`` for
  depth-4). The walk recurses into every yielded directory, so a
  nested directory is both addressable on its own (`team/repo`) and
  via its parent (`team`). Operators choose which path to set
  ``coder.project_name`` to.
- Project names are returned sorted lexicographically.
"""

from __future__ import annotations

import os
import time
from typing import TypedDict


_DEFAULT_ENTRY_BUDGET = 10_000
_DEFAULT_TIME_BUDGET_SECONDS = 3.0


class WalkInfo(TypedDict):
    """Structured-warning payload returned alongside the project list.

    ``truncated`` is ``True`` when the entry budget OR the wall-clock
    budget was exhausted before the walk completed; callers SHALL log a
    rate-limited structured warning when this flag is set so operators
    know the listing may be incomplete.
    """

    truncated: bool
    entries_visited: int
    elapsed_seconds: float


def _name_is_filtered(name: str) -> bool:
    """Mirror ``_filter_project_entries`` policy: drop dotfiles + ``__*``."""

    return not name or name.startswith(".") or name.startswith("__")


def walk_projects(
    root: str,
    *,
    entry_budget: int = _DEFAULT_ENTRY_BUDGET,
    time_budget_seconds: float = _DEFAULT_TIME_BUDGET_SECONDS,
) -> tuple[list[str], WalkInfo]:
    """Walk ``root`` for audit-able projects; return (names, info).

    ``names`` is a sorted list of ``/``-joined project paths relative to
    ``root``. Every directory at every depth (subject to the dotfile /
    ``__``-prefix / symlink-loop filters and the entry/time budgets) is
    yielded. ``info`` carries the structured-warning payload so callers
    can log uniformly. Returns ``([], {...})`` when ``root`` does not
    exist or is not a directory.
    """

    started = time.monotonic()
    info: WalkInfo = {
        "truncated": False,
        "entries_visited": 0,
        "elapsed_seconds": 0.0,
    }
    try:
        root_real = os.path.realpath(root)
    except OSError:
        info["elapsed_seconds"] = time.monotonic() - started
        return [], info
    if not os.path.isdir(root_real):
        info["elapsed_seconds"] = time.monotonic() - started
        return [], info

    deadline = started + max(0.0, float(time_budget_seconds))
    visited_realpaths: set[str] = {root_real}
    found: list[str] = []

    # ---- Phase 1: enumerate every top-level directory ------------------
    # Bounded by ONE os.scandir + N top-level children — no recursion.
    # Even if Phase 2 below exhausts the budget mid-descent, every
    # depth-1 directory is guaranteed to appear in `found`. This is
    # load-bearing on slow filesystems (vmhgfs/9p shared mounts) where
    # one deep top-level subtree could otherwise starve its siblings.
    try:
        with os.scandir(root_real) as iterator:
            top_entries = list(iterator)
    except OSError:
        info["elapsed_seconds"] = time.monotonic() - started
        return [], info

    descend_targets: list[tuple[str, str]] = []
    for entry in top_entries:
        if info["entries_visited"] >= entry_budget:
            info["truncated"] = True
            break
        info["entries_visited"] += 1
        if _name_is_filtered(entry.name):
            continue
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
        except OSError:
            continue
        try:
            real = os.path.realpath(entry.path)
        except OSError:
            continue
        if real in visited_realpaths:
            continue
        visited_realpaths.add(real)
        found.append(entry.name)
        descend_targets.append((entry.path, entry.name))

    # ---- Phase 2: recurse into each top-level subtree ------------------
    # Shared budget. If a deep subtree exhausts the budget, remaining
    # subtrees are not descended but their depth-1 entries are still in
    # `found` from Phase 1.
    def _descend(current_path: str, prefix: str) -> bool:
        if info["entries_visited"] >= entry_budget:
            info["truncated"] = True
            return False
        if time.monotonic() >= deadline:
            info["truncated"] = True
            return False
        try:
            iterator = os.scandir(current_path)
        except OSError:
            return True
        with iterator as entries:
            for entry in entries:
                if info["entries_visited"] >= entry_budget:
                    info["truncated"] = True
                    return False
                if time.monotonic() >= deadline:
                    info["truncated"] = True
                    return False
                info["entries_visited"] += 1
                if _name_is_filtered(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                try:
                    real = os.path.realpath(entry.path)
                except OSError:
                    continue
                if real in visited_realpaths:
                    continue
                visited_realpaths.add(real)
                rel = f"{prefix}/{entry.name}"
                found.append(rel)
                if not _descend(entry.path, rel):
                    return False
        return True

    for descend_path, descend_prefix in descend_targets:
        if not _descend(descend_path, descend_prefix):
            break

    found.sort()
    info["elapsed_seconds"] = time.monotonic() - started
    return found, info


__all__ = ["walk_projects", "WalkInfo"]
