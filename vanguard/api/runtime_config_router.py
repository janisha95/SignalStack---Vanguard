"""
runtime_config_router.py — FastAPI router for config read/write + runtime state (Phase 5).

Single responsibility: all /api/v1/config/* and /api/v1/runtime/* and /api/v1/lifecycle/*
endpoints. Mounted by the top-level FastAPI app in server.py.

Hard rules enforced here:
  - Every PUT validates before write (returns 400 on schema error, never corrupts file).
  - Every PUT writes atomically (.tmp → fsync → rename).
  - Every PUT bumps config_version and appends to vanguard_config_audit_log.
  - Placeholder auth: X-Auth header accepted with any non-empty value.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH: Path | None = None
_DB_PATH: str | None = None
_RELOAD_FLAG = Path("/tmp/vanguard_config_reload")


def configure(config_path: str, db_path: str) -> None:
    """Called at startup by server.py to inject paths."""
    global _CONFIG_PATH, _DB_PATH
    _CONFIG_PATH = Path(config_path)
    _DB_PATH = db_path


def _get_config_path() -> Path:
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH
    from vanguard.config.runtime_config import _DEFAULT_CONFIG_PATH
    return _DEFAULT_CONFIG_PATH


def _get_db_path() -> str:
    if _DB_PATH is not None:
        return _DB_PATH
    from vanguard.config.runtime_config import get_shadow_db_path
    return get_shadow_db_path()


def _load_raw_config() -> dict:
    """Load raw JSON from disk (not through runtime_config cache)."""
    p = _get_config_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _current_version(cfg: dict) -> str:
    return str(cfg.get("config_version") or "0.0.0.0")


def _next_version(cfg: dict) -> str:
    """Generate {YYYY.MM.DD}.{seq} version string."""
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    current = _current_version(cfg)
    # Try to increment today's sequence
    if current.startswith(today + "."):
        try:
            seq = int(current.split(".")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{today}.{seq}"


def _atomic_write(path: Path, content: str) -> None:
    """Write to .tmp, fsync, rename. Never leaves partial writes."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(content)
    with open(tmp_path, "r") as f:
        os.fsync(f.fileno())
    tmp_path.rename(path)


def _validate_and_merge(section: str, new_value: Any) -> dict:
    """
    Merge new_value into the current config under `section`, validate, return new full config.
    Raises HTTPException(400) if validation fails.
    """
    from vanguard.config.runtime_config import _base_runtime_config, _validate_runtime_config

    raw = _load_raw_config()
    merged = dict(_base_runtime_config())
    for k, v in raw.items():
        merged[k] = v
    merged[section] = new_value

    errors = []
    try:
        _validate_runtime_config(merged)
    except ValueError as exc:
        errors.append(str(exc))

    if errors:
        raise HTTPException(status_code=400, detail={"success": False, "errors": errors})

    return merged


def _require_auth(x_auth: str | None) -> None:
    """Placeholder auth: any non-empty X-Auth header passes."""
    if not x_auth:
        raise HTTPException(status_code=401, detail="X-Auth header required")


def _ensure_tables() -> None:
    from vanguard.execution.shadow_log import ensure_tables
    try:
        ensure_tables(_get_db_path())
    except Exception as exc:
        logger.warning("_ensure_tables: %s", exc)


# ---------------------------------------------------------------------------
# Config read endpoints
# ---------------------------------------------------------------------------

@router.get("/api/v1/config/full")
async def get_full_config() -> JSONResponse:
    """Return entire config JSON + config_version."""
    cfg = _load_raw_config()
    return JSONResponse({"config_version": _current_version(cfg), **cfg})


@router.get("/api/v1/config/runtime")
async def get_runtime() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("runtime") or {})


@router.get("/api/v1/config/profiles")
async def get_profiles() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("profiles") or [])


@router.get("/api/v1/config/universes")
async def get_universes() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("universes") or {})


@router.get("/api/v1/config/policy-templates")
async def get_policy_templates() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("policy_templates") or {})


@router.get("/api/v1/config/overrides")
async def get_overrides() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("temporary_overrides") or {})


@router.get("/api/v1/config/blackouts")
async def get_blackouts() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse(cfg.get("calendar_blackouts") or [])


@router.get("/api/v1/config/audit-log")
async def get_audit_log(limit: int = Query(default=20, ge=1, le=500)) -> JSONResponse:
    _ensure_tables()
    import sqlite3
    db = _get_db_path()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, updated_at_utc, old_config_version, new_config_version, "
                "section_changed, diff_summary FROM vanguard_config_audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Config write endpoints
# ---------------------------------------------------------------------------

def _write_section(section: str, new_value: Any, x_auth: str | None) -> JSONResponse:
    _require_auth(x_auth)
    _ensure_tables()
    raw = _load_raw_config()
    old_version = _current_version(raw)

    merged = _validate_and_merge(section, new_value)
    new_version = _next_version(merged)
    merged["config_version"] = new_version

    out = json.dumps(merged, indent=2, sort_keys=False) + "\n"
    _atomic_write(_get_config_path(), out)

    # Audit log
    diff_summary = f"section={section} old_version={old_version} new_version={new_version}"
    try:
        from vanguard.execution.shadow_log import insert_audit_row
        insert_audit_row(
            _get_db_path(),
            old_version=old_version,
            new_version=new_version,
            section_changed=section,
            diff_summary=diff_summary,
            full_new_config=merged,
        )
    except Exception as exc:
        logger.warning("audit log insert failed: %s", exc)

    # Signal orchestrator to reload
    _RELOAD_FLAG.touch()

    return JSONResponse({
        "success": True,
        "new_config_version": new_version,
        "reload_required": True,
    })


@router.put("/api/v1/config/runtime")
async def put_runtime(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    return _write_section("runtime", body, x_auth)


@router.put("/api/v1/config/profiles")
async def put_profiles(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail={"success": False, "errors": ["profiles must be a list"]})
    # Validate required profile fields
    errors = []
    for i, p in enumerate(body):
        for field in ("id", "is_active", "instrument_scope", "policy_id"):
            if field not in p:
                errors.append(f"profiles[{i}] missing required field: {field!r}")
    if errors:
        raise HTTPException(status_code=400, detail={"success": False, "errors": errors})
    return _write_section("profiles", body, x_auth)


@router.put("/api/v1/config/universes")
async def put_universes(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    return _write_section("universes", body, x_auth)


@router.put("/api/v1/config/policy-templates")
async def put_policy_templates(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    return _write_section("policy_templates", body, x_auth)


@router.put("/api/v1/config/overrides")
async def put_overrides(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    return _write_section("temporary_overrides", body, x_auth)


@router.put("/api/v1/config/blackouts")
async def put_blackouts(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail={"success": False, "errors": ["blackouts must be a list"]})
    return _write_section("calendar_blackouts", body, x_auth)


# ---------------------------------------------------------------------------
# Runtime state endpoints
# ---------------------------------------------------------------------------

def _is_no_such_table(exc: Exception) -> bool:
    return "no such table" in str(exc).lower()


@router.get("/api/v1/runtime/account-state")
async def get_account_state() -> JSONResponse:
    import sqlite3
    db = _get_db_path()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT profile_id, equity, starting_equity_today, daily_pnl_pct, "
                "trailing_dd_pct, paused_until_utc, pause_reason, updated_at_utc "
                "FROM vanguard_account_state ORDER BY profile_id"
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        if _is_no_such_table(exc):
            return JSONResponse([])
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/v1/runtime/open-positions")
async def get_open_positions() -> JSONResponse:
    import sqlite3
    db = _get_db_path()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM vanguard_open_positions ORDER BY profile_id, last_synced_at_utc DESC"
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        if _is_no_such_table(exc):
            return JSONResponse([])
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/v1/runtime/trade-journal")
async def get_trade_journal(
    profile: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> JSONResponse:
    import sqlite3
    db = _get_db_path()
    try:
        conditions = []
        params: list = []
        if profile:
            conditions.append("profile_id = ?")
            params.append(profile)
        if status:
            conditions.append("status = ?")
            params.append(status.upper())
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT * FROM vanguard_trade_journal {where} "
                f"ORDER BY last_synced_at_utc DESC LIMIT ?",
                params,
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        if _is_no_such_table(exc):
            return JSONResponse([])
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/v1/runtime/reconciliation-events")
async def get_reconciliation_events(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
    import sqlite3
    db = _get_db_path()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM vanguard_reconciliation_log ORDER BY detected_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        if _is_no_such_table(exc):
            return JSONResponse([])
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/v1/runtime/shadow-executions")
async def get_shadow_executions(
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> JSONResponse:
    _ensure_tables()
    import sqlite3
    db = _get_db_path()
    try:
        if since:
            with sqlite3.connect(db) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM vanguard_shadow_execution_log "
                    "WHERE would_have_submitted_at_utc >= ? "
                    "ORDER BY id DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
        else:
            with sqlite3.connect(db) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM vanguard_shadow_execution_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/v1/runtime/config-version")
async def get_config_version() -> JSONResponse:
    cfg = _load_raw_config()
    return JSONResponse({
        "config_version": _current_version(cfg),
        "config_path": str(_get_config_path()),
    })


@router.get("/api/v1/runtime/resolved-universe")
async def get_resolved_universe() -> JSONResponse:
    """Return the current GFT universe member list from vanguard_universe_members."""
    import sqlite3
    db = _get_db_path()
    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT symbol, asset_class, is_active FROM vanguard_universe_members "
                "WHERE is_active = 1 ORDER BY asset_class, symbol"
            ).fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Lifecycle control endpoints
# ---------------------------------------------------------------------------

@router.post("/api/v1/lifecycle/reload-config")
async def reload_config(x_auth: str | None = Header(default=None)) -> JSONResponse:
    """Write the reload flag file; orchestrator picks it up within 1 cycle."""
    _require_auth(x_auth)
    _RELOAD_FLAG.touch()
    return JSONResponse({"success": True, "message": "Reload flag written. Orchestrator will reload within 1 cycle."})


@router.post("/api/v1/lifecycle/pause-profile")
async def pause_profile(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    """
    Manually pause a profile. Body: {profile_id, until_utc, reason}.
    Writes a temporary_override with is_paused=True and expires_at_utc=until_utc.
    """
    _require_auth(x_auth)
    body = await request.json()
    profile_id = str(body.get("profile_id") or "")
    until_utc  = str(body.get("until_utc") or "")
    reason     = str(body.get("reason") or "manual_pause")

    if not profile_id or not until_utc:
        raise HTTPException(status_code=400, detail={"success": False, "errors": ["profile_id and until_utc required"]})

    raw = _load_raw_config()
    overrides = dict(raw.get("temporary_overrides") or {})
    overrides[profile_id] = {
        "is_paused": True,
        "expires_at_utc": until_utc,
        "reason": reason,
    }
    return _write_section("temporary_overrides", overrides, x_auth)


@router.post("/api/v1/lifecycle/unpause-profile")
async def unpause_profile(request: Request, x_auth: str | None = Header(default=None)) -> JSONResponse:
    """Remove the manual pause override for a profile."""
    _require_auth(x_auth)
    body = await request.json()
    profile_id = str(body.get("profile_id") or "")
    if not profile_id:
        raise HTTPException(status_code=400, detail={"success": False, "errors": ["profile_id required"]})

    raw = _load_raw_config()
    overrides = dict(raw.get("temporary_overrides") or {})
    overrides.pop(profile_id, None)
    return _write_section("temporary_overrides", overrides, x_auth)


# ---------------------------------------------------------------------------
# UI placeholder
# ---------------------------------------------------------------------------

@router.get("/config-admin", response_class=HTMLResponse)
async def config_admin_ui() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html><html><body>
<h1>Vanguard Config Admin (Phase 5 placeholder)</h1>
<p>Backend API ready. UI ships in a later phase.</p>
<p><a href="/api/v1/config/full">View current config JSON</a></p>
</body></html>""")
