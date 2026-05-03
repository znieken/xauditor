"""`xauditor-portal` helper CLI.

Offers ops verbs that run inside the backend container or a developer shell:
- `migrate`: run Alembic migrations to head (Phase 5).
- `seed`: create the default `auditor` user if none exists (Phase 5).
- `serve`: boot the FastAPI app with Uvicorn (Phase 7).
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xauditor-portal")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Run Alembic migrations to head.")
    sub.add_parser("seed", help="Seed the default auditor user if none exists.")
    serve = sub.add_parser("serve", help="Boot the FastAPI app with Uvicorn.")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _run_migrate()
    if args.command == "seed":
        return _run_seed()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "xauditor_portal.app:create_app",
            host=args.host,
            port=args.port,
            factory=True,
        )
        return 0
    raise ValueError(f"Unsupported command: {args.command}")


def _run_migrate() -> int:
    """Run Alembic migrations to head.

    Resolution order:

    1. ``XAUDITOR_PORTAL_DATABASE_URL`` — takes the URL as-is and runs
       migrations directly, with no dependency on the ``xauditor``
       package. This is the path used inside the portal Docker image,
       where the managed runtime injects this env var on container
       create.
    2. Otherwise fall back to the main ``xauditor`` package's
       ``load_config()`` so developers running the CLI from a full
       xauditor checkout pick up the same config the audit flow uses.
    """

    import os
    from pathlib import Path

    url_override = os.environ.get("XAUDITOR_PORTAL_DATABASE_URL")
    if url_override:
        from xauditor_portal.db.migrations import upgrade_to_head_for_url

        upgrade_to_head_for_url(url_override)
        return 0
    from xauditor.config import load_config
    from xauditor_portal.db.migrations import upgrade_to_head

    config = load_config(repo_root=Path.cwd(), env=dict(os.environ))
    upgrade_to_head(config.reportdb)
    return 0


def _run_seed() -> int:
    """Create the default auditor user if `auth.users` is empty."""

    import os
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from xauditor_portal.db.base import build_engine_url
    from xauditor_portal.db.seed import seed_default_user

    url_override = os.environ.get("XAUDITOR_PORTAL_DATABASE_URL")
    if url_override:
        url = url_override
    else:
        from xauditor.config import load_config

        config = load_config(repo_root=Path.cwd(), env=dict(os.environ))
        url = build_engine_url(config.reportdb)
    # Use a sync engine for seed (simpler than async).
    sync_url = url.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    try:
        with Session(engine) as session:
            seed_default_user(session)
            session.commit()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
