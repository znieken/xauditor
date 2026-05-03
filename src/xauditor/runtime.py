from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeLayout:
    root_dir: Path
    cache_dir: Path
    manifests_dir: Path
    reports_dir: Path

    @classmethod
    def from_root(cls, root_dir: Path) -> "RuntimeLayout":
        return cls(
            root_dir=root_dir,
            cache_dir=root_dir / "cache",
            manifests_dir=root_dir / "manifests",
            reports_dir=root_dir / "reports",
        )

    def ensure(self) -> "RuntimeLayout":
        for path in (self.root_dir, self.cache_dir, self.manifests_dir, self.reports_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self
