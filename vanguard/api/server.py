"""
server.py — FastAPI application for Vanguard QA config & runtime API (Phase 5).

Single responsibility: create the FastAPI app, mount the runtime_config_router,
inject paths, and serve on port 8090.

Run:
    python3 -m vanguard.api.server
    # or
    uvicorn vanguard.api.server:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vanguard.api.runtime_config_router import configure, router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Vanguard QA Config & Runtime API",
    description="Phase 5 — read/write runtime config, shadow execution log, lifecycle control.",
    version="5.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
async def _startup() -> None:
    """Inject config and DB paths, ensure Phase 5 tables exist."""
    from vanguard.config.runtime_config import _DEFAULT_CONFIG_PATH, get_shadow_db_path
    db_path = os.environ.get("VANGUARD_SOURCE_DB") or get_shadow_db_path()
    configure(str(_DEFAULT_CONFIG_PATH), db_path)

    from vanguard.execution.shadow_log import ensure_tables
    try:
        ensure_tables(db_path)
        logger.info("[server] Phase 5 tables ensured at %s", db_path)
    except Exception as exc:
        logger.warning("[server] ensure_tables failed: %s", exc)

    logger.info("[server] Vanguard API started — config=%s db=%s", _DEFAULT_CONFIG_PATH, db_path)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    parser = argparse.ArgumentParser(description="Vanguard QA API server (Phase 5)")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run("vanguard.api.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
