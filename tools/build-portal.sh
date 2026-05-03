#!/usr/bin/env bash
# Build the xauditor-portal sdist + wheel without paying the cost of
# setuptools walking frontend/node_modules.
#
# Why: on slow filesystems (e.g. VMware HGFS shared folders), setuptools'
# SOURCES.txt enumeration walks the entire src/ tree, which on this package
# means stat'ing every file under frontend/node_modules (~13k files, 158 MB).
# That walk dominates the build time. We work around it by renaming
# node_modules out of the package tree (in-fs metadata-only operation) before
# the build, then restoring it afterwards.
#
# The MANIFEST.in / pyproject.toml don't actually include node_modules in the
# resulting wheel or sdist, but setuptools still walks it during file
# enumeration. Stashing node_modules avoids that walk entirely.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$REPO_ROOT/packages/xauditor-portal"
FRONTEND_DIR="$PKG_DIR/src/xauditor_portal/frontend"
NM_LIVE="$FRONTEND_DIR/node_modules"
NM_STASH="$PKG_DIR/.node_modules_stash"

if [[ ! -d "$PKG_DIR" ]]; then
  echo "build-portal: $PKG_DIR not found" >&2
  exit 1
fi

restored=0
restore() {
  if [[ "$restored" -eq 1 ]]; then
    return
  fi
  restored=1
  if [[ -d "$NM_STASH" ]]; then
    if [[ -e "$NM_LIVE" ]]; then
      echo "build-portal: WARNING — both $NM_LIVE and $NM_STASH exist; leaving stash in place for manual review" >&2
      return
    fi
    mv "$NM_STASH" "$NM_LIVE"
    echo "build-portal: restored node_modules"
  fi
}
trap restore EXIT INT TERM

if [[ -d "$NM_STASH" ]]; then
  echo "build-portal: $NM_STASH already exists from a previous interrupted run; aborting so you can resolve manually" >&2
  exit 2
fi

if [[ -d "$NM_LIVE" ]]; then
  echo "build-portal: stashing node_modules (in-fs rename)…"
  mv "$NM_LIVE" "$NM_STASH"
fi

echo "build-portal: running uv build…"
uv build "$PKG_DIR" "$@"

echo "build-portal: built artifacts:"
ls -lh "$PKG_DIR/dist" | tail -n +2
