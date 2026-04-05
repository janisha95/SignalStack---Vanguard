"""
unified_api.py — SignalStack Unified API (port 8090).

Serves unified candidate data from Meridian, S1, and Vanguard.
Handles trade execution, userviews, and report configs.

Usage:
    cd ~/SS/Vanguard
    python3 -m uvicorn vanguard.api.unified_api:app --host 0.0.0.0 --port 8090

Location: ~/SS/Vanguard/vanguard/api/unified_api.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .field_registry import FIELD_REGISTRY
from . import userviews as uv_module
from . import trade_desk as td_module
from . import reports as rpt_module
from . import accounts as acc_module
from .adapters import meridian_adapter, s1_adapter, vanguard_adapter
from vanguard.helpers.clock import now_et
from vanguard.helpers.db import sqlite_conn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("unified_api")
_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "vanguard_universe.db"
_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3007",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3007",
    "https://signalstack-app.vercel.app",
]

app = FastAPI(
    title="SignalStack Unified API",
    version="1.0.0",
    description="Unified candidate data from Meridian, S1, and Vanguard.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    """Initialize DB tables and seed system views."""
    uv_module.init_table()
    uv_module.seed_system_views()
    td_module.init_table()
    rpt_module.init_table()
    acc_module.init_table()
    acc_module.seed_profiles()
    logger.info("Unified API started — DB tables ready")


def _get_config_value(name: str) -> str:
    """Read an env value directly, with `.env` fallback for local desktop runs."""
    value = os.environ.get(name, "")
    if value:
        return value
    env_file = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def _load_latest_session_log() -> dict[str, Any] | None:
    try:
        with sqlite_conn(str(_DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM vanguard_session_log ORDER BY date DESC LIMIT 1"
            ).fetchone()
    except Exception as exc:
        logger.warning("system status session-log query failed: %s", exc)
        return None
    return dict(row) if row else None


def _load_source_health_rows() -> list[dict[str, Any]]:
    try:
        with sqlite_conn(str(_DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM vanguard_source_health ORDER BY data_source, asset_class"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("system status source-health query failed: %s", exc)
        return []


def _load_portfolio_state_rows(date_str: str | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        with sqlite_conn(str(_DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            if date_str:
                rows = con.execute(
                    "SELECT * FROM vanguard_portfolio_state WHERE date = ? ORDER BY account_id",
                    (date_str,),
                ).fetchall()
                resolved_date = date_str
            else:
                latest_row = con.execute(
                    "SELECT MAX(date) FROM vanguard_portfolio_state"
                ).fetchone()
                resolved_date = latest_row[0] if latest_row else None
                rows = []
                if resolved_date:
                    rows = con.execute(
                        "SELECT * FROM vanguard_portfolio_state WHERE date = ? ORDER BY account_id",
                        (resolved_date,),
                    ).fetchall()
        return resolved_date, [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("portfolio state query failed: %s", exc)
        return date_str, []


def _count_open_execution_rows(date_str: str) -> dict[str, Any]:
    try:
        with sqlite_conn(str(_DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT
                        COALESCE(account, 'unknown') AS account,
                        symbol,
                        UPPER(COALESCE(direction, '')) AS direction,
                        MAX(COALESCE(filled_at, executed_at, created_at)) AS last_seen_at
                    FROM execution_log
                    WHERE substr(COALESCE(filled_at, executed_at, created_at), 1, 10) = ?
                      AND (outcome IS NULL OR UPPER(outcome) = 'OPEN')
                    GROUP BY COALESCE(account, 'unknown'), symbol, UPPER(COALESCE(direction, ''))
                    """,
                    (date_str,),
                ).fetchall()
            ]
    except Exception as exc:
        logger.warning("portfolio state execution-log query failed: %s", exc)
        rows = []
    open_rows = rows
    long_count = sum(1 for r in open_rows if str(r.get("direction") or "").upper() == "LONG")
    short_count = sum(1 for r in open_rows if str(r.get("direction") or "").upper() == "SHORT")
    by_account: dict[str, int] = {}
    for row in open_rows:
        account_id = str(row.get("account") or "unknown")
        by_account[account_id] = by_account.get(account_id, 0) + 1
    return {
        "rows": open_rows,
        "total_open_positions": len(open_rows),
        "long_count": long_count,
        "short_count": short_count,
        "open_positions_by_account": by_account,
    }


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    """System health: source availability, webhook, telegram."""
    import os
    from pathlib import Path

    # Meridian
    mer_date  = meridian_adapter.get_latest_date()
    mer_count = meridian_adapter.count_candidates(mer_date) if mer_date else 0
    mer_ok    = bool(mer_date and mer_count > 0)

    # S1
    s1_date  = s1_adapter.get_latest_date()
    s1_count = s1_adapter.count_candidates(s1_date) if s1_date else 0
    s1_ok    = bool(s1_date and s1_count > 0)

    # Vanguard
    vg_date = vanguard_adapter.get_latest_date()
    vg_count = vanguard_adapter.count_candidates(vg_date) if vg_date else 0
    vg_ok = bool(vg_date and vg_count > 0)

    # Webhook
    wh_url = _get_config_value("SIGNALSTACK_WEBHOOK_URL")

    # Telegram
    tg_token = _get_config_value("TELEGRAM_BOT_TOKEN")

    return {
        "status": "ok",
        "sources": {
            "meridian": {
                "available":   mer_ok,
                "last_date":   mer_date,
                "candidates":  mer_count,
            },
            "s1": {
                "available":  s1_ok,
                "last_date":  s1_date,
                "candidates": s1_count,
            },
            "vanguard": {
                "available":  vg_ok,
                "last_date":  vg_date,
                "candidates": vg_count,
            },
        },
        "webhook":  {"configured": bool(wh_url), "url": wh_url[:40] + "..." if wh_url else None},
        "telegram": {"configured": bool(tg_token)},
    }


@app.get("/api/v1/system/status")
async def system_status() -> dict[str, Any]:
    """Current backend/system status snapshot for the React shell."""
    mer_date = meridian_adapter.get_latest_date()
    mer_count = meridian_adapter.count_candidates(mer_date) if mer_date else 0
    s1_date = s1_adapter.get_latest_date()
    s1_count = s1_adapter.count_candidates(s1_date) if s1_date else 0

    vg_payload = vanguard_adapter.get_candidates()
    vg_rows = vg_payload.get("candidates", []) if isinstance(vg_payload, dict) else []
    vg_date = vanguard_adapter.get_latest_date()
    vg_readiness = vg_payload.get("readiness", "data_ready") if isinstance(vg_payload, dict) else "data_ready"
    vg_lane_status = vg_payload.get("lane_status", "staged") if isinstance(vg_payload, dict) else "staged"

    source_health_rows = _load_source_health_rows()
    session_row = _load_latest_session_log()
    webhook_url = _get_config_value("SIGNALSTACK_WEBHOOK_URL")
    tg_token = _get_config_value("TELEGRAM_BOT_TOKEN")

    source_health_latest = max(
        (str(r.get("last_updated")) for r in source_health_rows if r.get("last_updated")),
        default=None,
    )
    any_lane_available = bool(mer_date and mer_count > 0) or bool(s1_date and s1_count > 0) or bool(vg_date and vg_rows)

    return {
        "status": "ok" if any_lane_available else "degraded",
        "backend_ok": True,
        "as_of": now_et().isoformat(),
        "lanes": {
            "meridian": {
                "available": bool(mer_date and mer_count > 0),
                "latest_timestamp": mer_date,
                "candidate_count": mer_count,
            },
            "s1": {
                "available": bool(s1_date and s1_count > 0),
                "latest_timestamp": s1_date,
                "candidate_count": s1_count,
            },
            "vanguard": {
                "available": bool(vg_date and vg_rows),
                "latest_timestamp": vg_date,
                "candidate_count": len(vg_rows),
                "readiness": vg_readiness,
                "lane_status": vg_lane_status,
            },
        },
        "latest_timestamps": {
            "meridian": mer_date,
            "s1": s1_date,
            "vanguard": vg_date,
            "source_health": source_health_latest,
            "session_log": (session_row or {}).get("updated_at"),
        },
        "route_readiness": {
            "signalstack_webhook_configured": bool(webhook_url),
            "telegram_configured": bool(tg_token),
        },
        "source_health": source_health_rows,
        "session": session_row,
    }


# ── Field Registry ────────────────────────────────────────────────────────────

@app.get("/api/v1/field-registry")
async def field_registry() -> dict[str, Any]:
    return {"fields": FIELD_REGISTRY, "count": len(FIELD_REGISTRY)}


@app.get("/api/v1/data/health")
async def data_health() -> dict[str, Any]:
    """Source-aware freshness for Stage 1 data."""
    with sqlite3.connect(_DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM vanguard_source_health ORDER BY data_source, asset_class"
        ).fetchall()
    return {"sources": [dict(r) for r in rows]}


@app.get("/api/v1/portfolio/state")
async def portfolio_state(
    date: Optional[str] = Query(None, description="YYYY-MM-DD override"),
) -> dict[str, Any]:
    """Lightweight current portfolio state for the React Portfolio page."""
    resolved_date, state_rows = _load_portfolio_state_rows(date)
    date_str = resolved_date or date or str(date_type.today())
    open_exec = _count_open_execution_rows(date_str)
    profiles = acc_module.list_profiles()
    state_by_account = {str(row.get("account_id")): row for row in state_rows}

    accounts: list[dict[str, Any]] = []
    for profile in profiles:
        account_id = str(profile.get("id"))
        state = state_by_account.get(account_id) or {}
        accounts.append({
            "account_id": account_id,
            "name": profile.get("name"),
            "prop_firm": profile.get("prop_firm"),
            "environment": profile.get("environment"),
            "instrument_scope": profile.get("instrument_scope"),
            "status": state.get("status"),
            "open_positions": int(open_exec["open_positions_by_account"].get(account_id, 0)),
            "trades_today": int(state.get("trades_today") or 0),
            "daily_realized_pnl": float(state.get("daily_realized_pnl") or 0.0),
            "daily_unrealized_pnl": float(state.get("daily_unrealized_pnl") or 0.0),
            "total_pnl": float(state.get("total_pnl") or 0.0),
            "heat_used_pct": float(state.get("heat_used_pct") or 0.0),
            "last_updated_utc": state.get("last_updated_utc"),
        })

    as_of = max(
        (str(row.get("last_updated_utc")) for row in state_rows if row.get("last_updated_utc")),
        default=None,
    )

    return {
        "date": date_str,
        "as_of": as_of,
        "summary": {
            "total_open_positions": open_exec["total_open_positions"],
            "long_count": open_exec["long_count"],
            "short_count": open_exec["short_count"],
            "total_unrealized_pnl": round(
                sum(float(row.get("daily_unrealized_pnl") or 0.0) for row in state_rows),
                2,
            ),
        },
        "accounts": accounts,
        "source_of_truth": {
            "open_positions": "execution_log",
            "account_state": "vanguard_portfolio_state",
            "profiles": "account_profiles",
        },
    }


# ── Candidates ────────────────────────────────────────────────────────────────

def _apply_filters(rows: list[dict], filters_json: str | None) -> list[dict]:
    """
    Apply JSON filter object to rows.

    Filter format: {"field": {"op": value}, ...}
    Operators: eq, ne, gt, gte, lt, lte, in, contains
    Looks up field in top-level row first, then in native dict.
    """
    if not filters_json:
        return rows
    try:
        filters: dict[str, dict[str, Any]] = (
            filters_json if isinstance(filters_json, dict)
            else __import__("json").loads(filters_json)
        )
    except Exception:
        return rows

    result: list[dict] = []
    for row in rows:
        passes = True
        for field, condition in filters.items():
            # Get value from row (top-level first, then native)
            val = row.get(field)
            if val is None:
                val = (row.get("native") or {}).get(field)

            for op, threshold in condition.items():
                try:
                    if op == "eq":
                        if val != threshold:
                            passes = False
                    elif op == "ne":
                        if val == threshold:
                            passes = False
                    elif op == "gt":
                        if val is None or float(val) <= float(threshold):
                            passes = False
                    elif op == "gte":
                        if val is None or float(val) < float(threshold):
                            passes = False
                    elif op == "lt":
                        if val is None or float(val) >= float(threshold):
                            passes = False
                    elif op == "lte":
                        if val is None or float(val) > float(threshold):
                            passes = False
                    elif op == "in":
                        if val not in threshold:
                            passes = False
                    elif op == "contains":
                        if not (isinstance(val, str) and threshold.lower() in val.lower()):
                            passes = False
                except (TypeError, ValueError):
                    passes = False
            if not passes:
                break
        if passes:
            result.append(row)
    return result


def _apply_sort(rows: list[dict], sort_str: str | None) -> list[dict]:
    """Sort rows by 'field:asc' or 'field:desc'."""
    if not sort_str:
        return rows
    parts = sort_str.split(":")
    field = parts[0]
    descending = len(parts) > 1 and parts[1].lower() == "desc"

    def sort_key(row: dict) -> Any:
        val = row.get(field)
        if val is None:
            val = (row.get("native") or {}).get(field)
        if val is None:
            return (1, "")  # nulls last
        if isinstance(val, (int, float)):
            return (0, -val if descending else val)
        return (0, str(val))

    return sorted(rows, key=sort_key, reverse=descending if isinstance(sort_key(rows[0]) if rows else None, tuple) else False)


def _sort_rows(rows: list[dict], sort_str: str | None) -> list[dict]:
    if not sort_str or not rows:
        return rows
    parts = sort_str.split(":")
    field = parts[0]
    descending = len(parts) > 1 and parts[1].lower() == "desc"

    def get_val(row: dict) -> Any:
        v = row.get(field)
        if v is None:
            v = (row.get("native") or {}).get(field)
        return v

    def sort_key(row: dict):
        v = get_val(row)
        if v is None:
            return (1, 0, "")
        if isinstance(v, (int, float)):
            return (0, float(v), "")
        return (0, 0, str(v))

    return sorted(rows, key=sort_key, reverse=descending)


@app.get("/api/v1/candidates")
async def get_candidates(
    source:    str = Query("meridian", description="meridian|s1|vanguard|combined"),
    direction: str = Query("all",      description="LONG|SHORT|all"),
    sort:      Optional[str] = Query(None, description="field:asc or field:desc"),
    filters:   Optional[str] = Query(None, description="JSON filter object"),
    page:      int = Query(1,   ge=1),
    page_size: int = Query(500,  ge=1, le=1000),
    date:      Optional[str] = Query(None, description="YYYY-MM-DD override"),
) -> dict[str, Any]:
    dir_filter = None if direction == "all" else direction.upper()
    rows: list[dict] = []

    _vanguard_readiness: str = "placeholder"
    _vanguard_lane_status: str = "staged"

    if source in ("meridian", "combined"):
        rows.extend(meridian_adapter.get_candidates(trade_date=date, direction=dir_filter))
    if source in ("s1", "combined"):
        rows.extend(s1_adapter.get_candidates(run_date=date, direction=dir_filter))
    if source in ("vanguard", "combined"):
        _vr = vanguard_adapter.get_candidates(trade_date=date, direction=dir_filter)
        if isinstance(_vr, dict):
            rows.extend(_vr.get("candidates", []))
            _vanguard_readiness = _vr.get("readiness", "shortlist_ready")
            _vanguard_lane_status = _vr.get("lane_status", "staged")
        else:
            rows.extend(_vr)
            _vanguard_readiness = "shortlist_ready" if _vr else "placeholder"

    # Apply filters
    rows = _apply_filters(rows, filters)

    # Apply sort
    rows = _sort_rows(rows, sort)

    total = len(rows)
    offset = (page - 1) * page_size
    page_rows = rows[offset: offset + page_size]
    as_of = max(
        (str(row.get("as_of")) for row in rows if row.get("as_of")),
        default=None,
    )

    resp: dict[str, Any] = {
        "candidates":     page_rows,
        "rows":           page_rows,
        "total":          total,
        "count":          total,
        "source":         source,
        "as_of":          as_of,
        "updated_at":     as_of,
        "page":           page,
        "page_size":      page_size,
        "total_pages":    (total + page_size - 1) // page_size if page_size else 1,
        "field_registry": FIELD_REGISTRY,
    }
    if source == "vanguard":
        resp["readiness"]   = _vanguard_readiness
        resp["lane_status"] = _vanguard_lane_status
    return resp


@app.get("/api/v1/candidates/{row_id:path}")
async def get_candidate(row_id: str) -> dict[str, Any]:
    """Return full detail for one candidate by row_id."""
    # row_id format: "source:SYMBOL:DIRECTION:DATE[:suffix]"
    parts = row_id.split(":")
    if len(parts) < 4:
        raise HTTPException(status_code=400, detail=f"Invalid row_id format: {row_id}")

    source    = parts[0].lower()
    symbol    = parts[1].upper()
    direction = parts[2].upper()
    trade_date = parts[3]

    if source == "meridian":
        rows = meridian_adapter.get_candidates(trade_date=trade_date, direction=direction)
    elif source == "s1":
        rows = s1_adapter.get_candidates(run_date=trade_date, direction=direction)
    elif source == "vanguard":
        _vr = vanguard_adapter.get_candidates(trade_date=trade_date, direction=direction)
        rows = _vr.get("candidates", []) if isinstance(_vr, dict) else _vr
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    for row in rows:
        if row.get("row_id") == row_id or (
            row["symbol"] == symbol and row["side"] == direction
        ):
            return row

    raise HTTPException(status_code=404, detail=f"Candidate not found: {row_id}")


# ── Userviews ─────────────────────────────────────────────────────────────────

class UserviewCreate(BaseModel):
    name: str
    source: str
    direction: Optional[str] = None
    filters: Optional[Any] = None
    sorts: Optional[Any] = None
    grouping: Optional[Any] = None
    visible_columns: Optional[Any] = None
    column_order: Optional[Any] = None
    display_mode: str = "table"


class UserviewUpdate(BaseModel):
    name: Optional[str] = None
    source: Optional[str] = None
    direction: Optional[str] = None
    filters: Optional[Any] = None
    sorts: Optional[Any] = None
    grouping: Optional[Any] = None
    visible_columns: Optional[Any] = None
    column_order: Optional[Any] = None
    display_mode: Optional[str] = None


@app.get("/api/v1/userviews")
async def list_userviews() -> dict[str, Any]:
    views = uv_module.list_views()
    return {"views": views, "count": len(views)}


@app.post("/api/v1/userviews", status_code=201)
async def create_userview(body: UserviewCreate) -> dict[str, Any]:
    created = uv_module.create_view(body.model_dump())
    return created


@app.put("/api/v1/userviews/{view_id}")
async def update_userview(view_id: str, body: UserviewUpdate) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = uv_module.update_view(view_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"View not found: {view_id}")
    return updated


@app.delete("/api/v1/userviews/{view_id}")
async def delete_userview(view_id: str) -> dict[str, Any]:
    success, error = uv_module.delete_view(view_id)
    if not success:
        status = 404 if "not found" in error.lower() else 403
        raise HTTPException(status_code=status, detail=error)
    return {"deleted": True, "id": view_id}


# ── Trade Desk ────────────────────────────────────────────────────────────────

class TradeItem(BaseModel):
    symbol: str
    direction: str
    shares: int = 100
    tier: str = "manual"
    scores: Optional[dict[str, float]] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    order_type: Optional[str] = None
    limit_buffer: Optional[float] = None
    client_order_key: Optional[str] = None


class ExecuteRequest(BaseModel):
    trades: list[TradeItem]
    profile_id: Optional[str] = None
    preview_only: bool = False


class ExecutionUpdate(BaseModel):
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    pnl_dollars: Optional[float] = None
    pnl_pct: Optional[float] = None
    outcome: Optional[str] = None   # WIN|LOSE|TIMEOUT|OPEN
    days_held: Optional[int] = None


class ExecutionFillUpdate(BaseModel):
    fill_price: Optional[float] = None
    filled_at: Optional[str] = None
    execution_fee: Optional[float] = None


@app.get("/api/v1/picks/today")
async def picks_today(
    date: Optional[str] = Query(None, description="YYYY-MM-DD override"),
) -> dict[str, Any]:
    return td_module.get_picks_today(trade_date=date)


@app.post("/api/v1/execute")
async def execute_trades(body: ExecuteRequest) -> dict[str, Any]:
    trades = [t.model_dump() for t in body.trades]
    return td_module.execute_trades(
        trades,
        profile_id=body.profile_id,
        preview_only=body.preview_only,
    )


@app.get("/api/v1/execution/log")
async def execution_log(
    date:    Optional[str] = Query(None, description="YYYY-MM-DD (default: today)"),
    limit:   int           = Query(100, ge=1, le=1000),
    tag:     Optional[str] = Query(None, description="Filter by tag (e.g. tcn, alpha)"),
    outcome: Optional[str] = Query(None, description="WIN|LOSE|TIMEOUT|OPEN"),
) -> dict[str, Any]:
    rows = td_module.get_execution_log(date=date, limit=limit, tag=tag, outcome=outcome)
    return {"rows": rows, "count": len(rows)}


@app.put("/api/v1/execution/{execution_id}")
async def update_execution(
    execution_id: int,
    body: ExecutionUpdate,
) -> dict[str, Any]:
    """Update forward-tracking fields (tags, notes, exit_price, outcome, etc.)."""
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = td_module.update_execution(execution_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return updated


@app.put("/api/v1/execution/{execution_id}/fill")
async def update_execution_fill(
    execution_id: int,
    body: ExecutionFillUpdate,
) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = td_module.update_execution_fill(execution_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return updated


@app.post("/api/v1/execution/{execution_id}/close")
async def close_execution_position(execution_id: int) -> dict[str, Any]:
    row = td_module.get_execution(execution_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    outcome = (row.get("outcome") or "").upper()
    if outcome and outcome != "OPEN":
        raise HTTPException(status_code=400, detail=f"Trade already closed: {outcome}")

    symbol = str(row["symbol"]).upper()
    direction = str(row.get("direction") or "LONG").upper()
    shares = int(row.get("shares") or 0)
    entry = float(row.get("fill_price") or row.get("entry_price") or 0.0)

    webhook_response = "NO_WEBHOOK_CONFIGURED"
    webhook_url = td_module._get_webhook_url()
    if webhook_url:
        from vanguard.execution.signalstack_adapter import SignalStackAdapter

        adapter = SignalStackAdapter(webhook_url=webhook_url)
        resp = adapter.send_order(
            symbol=symbol,
            direction=direction,
            quantity=shares,
            operation="close",
            broker="ttp",
        )
        webhook_response = json.dumps(resp)
        if not resp.get("success"):
            raise HTTPException(status_code=502, detail=f"Close rejected: {resp.get('error') or resp.get('response_body')}")

    live_price = _get_live_price(symbol) or entry
    if direction == "LONG":
        pnl_per = live_price - entry
    else:
        pnl_per = entry - live_price

    existing_fee = float(row.get("execution_fee") or 0.0)
    close_fee = td_module.estimate_ttp_fee(shares)
    total_fee = round(existing_fee + close_fee, 2)
    pnl_dollars = round(pnl_per * shares - total_fee, 2)
    pnl_pct = round((pnl_per / entry) * 100, 2) if entry else 0.0
    final_outcome = "WIN" if pnl_dollars > 0 else "LOSE"
    now = datetime.now(timezone.utc).isoformat()

    updated = td_module.update_execution(
        execution_id,
        {
            "exit_price": live_price,
            "exit_date": now,
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "outcome": final_outcome,
        },
    )
    td_module.update_execution_fill(
        execution_id,
        {
            "fill_price": live_price,
            "filled_at": now,
            "execution_fee": total_fee,
        },
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to update execution after close")

    return {
        "id": execution_id,
        "symbol": symbol,
        "direction": direction,
        "exit_price": live_price,
        "pnl_dollars": pnl_dollars,
        "pnl_pct": pnl_pct,
        "outcome": final_outcome,
        "execution_fee": total_fee,
        "webhook_response": webhook_response,
    }


class BulkDeleteBody(BaseModel):
    ids: Optional[List[int]] = None
    before_date: Optional[str] = None  # YYYY-MM-DD


@app.delete("/api/v1/execution/bulk", status_code=200)
async def bulk_delete_executions(body: BulkDeleteBody) -> dict[str, Any]:
    """
    Delete multiple execution log entries.

    Pass `ids` (list of ints) to delete specific rows, or
    `before_date` (YYYY-MM-DD) to delete all rows executed before that date,
    or both to intersect.
    """
    if not body.ids and not body.before_date:
        raise HTTPException(status_code=422, detail="Provide ids and/or before_date")

    deleted_count = td_module.delete_executions_bulk(
        ids=body.ids,
        before_date=body.before_date,
    )
    return {"deleted": deleted_count}


@app.delete("/api/v1/execution/{execution_id}", status_code=204)
async def delete_execution(execution_id: int) -> Response:
    deleted = td_module.delete_execution(execution_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return Response(status_code=204)


@app.get("/api/v1/execution/analytics")
async def execution_analytics(
    period: str = Query("last_30_days", description="last_7_days|last_30_days|last_90_days|all"),
) -> dict[str, Any]:
    """Signal attribution analytics — win rate by tag, tier, and source."""
    return td_module.get_analytics(period=period)


# ── Reports ───────────────────────────────────────────────────────────────────

class ReportCreate(BaseModel):
    name: str
    enabled: int = 1
    schedule: str
    delivery_channel: str = "telegram"
    blocks: Any = []
    telegram_bot_token: Optional[str] = None
    telegram_chat_id:   Optional[str] = None


class ReportUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[int] = None
    schedule: Optional[str] = None
    delivery_channel: Optional[str] = None
    blocks: Optional[Any] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id:   Optional[str] = None


@app.get("/api/v1/reports")
async def list_reports() -> dict[str, Any]:
    rpts = rpt_module.list_reports()
    return {"reports": rpts, "count": len(rpts)}


@app.post("/api/v1/reports", status_code=201)
async def create_report(body: ReportCreate) -> dict[str, Any]:
    return rpt_module.create_report(body.model_dump())


@app.put("/api/v1/reports/{report_id}")
async def update_report(report_id: str, body: ReportUpdate) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = rpt_module.update_report(report_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Report not found: {report_id}")
    return updated


@app.delete("/api/v1/reports/{report_id}")
async def delete_report(report_id: str) -> dict[str, Any]:
    success, error = rpt_module.delete_report(report_id)
    if not success:
        raise HTTPException(status_code=404, detail=error)
    return {"deleted": True, "id": report_id}


@app.post("/api/v1/reports/{report_id}/test")
async def test_report(
    report_id: str,
    date: Optional[str] = Query(None, description="YYYY-MM-DD override"),
) -> dict[str, Any]:
    """Generate and send the report NOW to Telegram."""
    report = rpt_module.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"Report not found: {report_id}")
    result = rpt_module.send_report(report, trade_date=date)
    return result


_MERIDIAN_BARS_DB = Path.home() / "SS" / "Meridian" / "data" / "v2_universe.db"


@app.get("/api/v1/sizing/{symbol}/{direction}")
async def get_sizing(symbol: str, direction: str) -> dict[str, Any]:
    """
    Return ATR(14) and position sizing stops for a symbol.

    Uses daily_bars from Meridian DB (high, low, close).
    Returns stop prices at 1.5x, 2.0x, and 2.5x ATR.
    """
    ticker = symbol.upper()
    is_long = direction.upper() == "LONG"

    if not _MERIDIAN_BARS_DB.exists():
        raise HTTPException(status_code=503, detail="Meridian bars DB unavailable")

    try:
        con = sqlite3.connect(f"file:{_MERIDIAN_BARS_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT high, low, close FROM daily_bars WHERE ticker = ? AND close IS NOT NULL ORDER BY date DESC LIMIT 15",
                (ticker,),
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if len(rows) < 2:
        raise HTTPException(status_code=404, detail=f"Not enough price bars for {ticker}")

    # Reverse to chronological order (oldest first)
    rows = list(reversed(rows))

    true_ranges = []
    for i in range(1, len(rows)):
        h, lo, prev_c = rows[i][0], rows[i][1], rows[i - 1][2]
        tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        true_ranges.append(tr)

    n = min(14, len(true_ranges))
    atr = sum(true_ranges[-n:]) / n if n > 0 else 0.0
    price = float(rows[-1][2])

    def stop_at(mult: float) -> dict[str, Any]:
        dist = round(atr * mult, 4)
        stop_price = round(price - dist, 2) if is_long else round(price + dist, 2)
        return {"distance": dist, "price": stop_price}

    return {
        "symbol": ticker,
        "direction": direction.upper(),
        "price": price,
        "atr_14": round(atr, 4),
        "atr_pct": round(atr / price * 100, 2) if price else 0.0,
        "stops": {
            "atr_1_5": stop_at(1.5),
            "atr_2_0": stop_at(2.0),
            "atr_2_5": stop_at(2.5),
        },
    }


# ── Live Portfolio ─────────────────────────────────────────────────────────────


def _get_live_price(symbol: str) -> float | None:
    try:
        import yfinance as yf
    except ImportError:
        return None

    ticker = yf.Ticker(symbol)

    try:
        fast = ticker.fast_info
        price = fast.get("last_price") or fast.get("lastPrice") or fast.get("previous_close")
        if price and float(price) > 0:
            return round(float(price), 2)
    except Exception:
        pass

    try:
        info = ticker.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price and float(price) > 0:
            return round(float(price), 2)
    except Exception:
        pass

    try:
        hist = ticker.history(period="1d")
        if not hist.empty:
            close = hist["Close"].iloc[-1]
            if close and float(close) > 0:
                return round(float(close), 2)
    except Exception:
        pass

    return None


def _get_live_prices_batch(symbols: list[str]) -> dict[str, float]:
    try:
        import yfinance as yf
    except ImportError:
        return {}

    if not symbols:
        return {}

    prices: dict[str, float] = {}

    try:
        data = yf.download(symbols, period="1d", group_by="ticker", progress=False, auto_adjust=False)
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    close = data["Close"].iloc[-1]
                else:
                    close = data[sym]["Close"].iloc[-1]
                if close and float(close) > 0:
                    prices[sym] = round(float(close), 2)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("yfinance batch download failed: %s", exc)

    for sym in symbols:
        if sym in prices:
            continue
        price = _get_live_price(sym)
        if price is not None:
            prices[sym] = price

    if prices:
        return prices

    key = os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET") or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        return {}

    try:
        import requests

        headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={','.join(symbols)}&feed=iex",
            headers=headers,
            timeout=10,
        )
        if resp.ok:
            payload = resp.json()
            snapshots = payload.get("snapshots", payload)
            for sym, snap in snapshots.items():
                trade = (snap or {}).get("latestTrade", {})
                price = trade.get("p")
                if price and float(price) > 0:
                    prices[sym] = round(float(price), 2)
    except Exception as exc:
        logger.warning("Alpaca snapshot fallback failed: %s", exc)

    return prices

@app.get("/api/v1/portfolio/live")
async def get_live_portfolio(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
) -> dict[str, Any]:
    """
    Return open positions from execution_log with live prices from yfinance.

    Only rows with outcome IS NULL or outcome = 'OPEN' for the given date
    are included. Each position shows entry_price, live_price, unrealized P&L.
    """
    date_str = date or str(date_type.today())

    all_rows = td_module.get_execution_log(date=date_str, limit=500)
    open_rows = [r for r in all_rows if not r.get("outcome") or r["outcome"] == "OPEN"]

    if not open_rows:
        return {
            "positions": [],
            "summary": {"total_unrealized_pnl": 0.0, "count": 0, "winners": 0, "losers": 0},
            "as_of": now_et().isoformat(),
        }

    symbols = list({r["symbol"] for r in open_rows})
    live_prices = _get_live_prices_batch(symbols)

    positions = []
    total_pnl = 0.0
    winners = 0
    losers = 0
    for r in open_rows:
        sym = r["symbol"]
        live = live_prices.get(sym)
        entry = r.get("fill_price") or r.get("entry_price") or 0.0
        shares = r.get("shares") or 0
        is_long = (r.get("direction") or "LONG").upper() == "LONG"
        fee = float(r.get("execution_fee") or 0.0)

        unrealized_pnl: float | None = None
        unrealized_pct: float | None = None
        if live and entry and shares:
            diff = (live - entry) if is_long else (entry - live)
            unrealized_pnl = round(diff * shares - fee, 2)
            unrealized_pct = round(diff / entry * 100, 3) if entry else None
            total_pnl += unrealized_pnl
            if unrealized_pnl > 0:
                winners += 1
            elif unrealized_pnl < 0:
                losers += 1

        positions.append({
            "id": r["id"],
            "symbol": sym,
            "direction": r.get("direction"),
            "shares": shares,
            "entry_price": entry,
            "fill_price": r.get("fill_price"),
            "filled_at": r.get("filled_at"),
            "execution_fee": fee,
            "stop_loss": r.get("stop_loss"),
            "take_profit": r.get("take_profit"),
            "live_price": live,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pct": unrealized_pct,
            "tier": r.get("tier"),
            "tags": r.get("tags"),
            "executed_at": r.get("executed_at"),
            "account": r.get("account"),
        })

    return {
        "positions": positions,
        "summary": {
            "total_unrealized_pnl": round(total_pnl, 2),
            "count": len(positions),
            "winners": winners,
            "losers": losers,
        },
        "as_of": now_et().isoformat(),
    }


# ── Account Profiles ───────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    id: str
    name: str
    prop_firm: str
    account_type: str
    account_size: int
    daily_loss_limit: float
    max_drawdown: float
    max_positions: int
    must_close_eod: int = 0
    is_active: int = 1
    max_single_position_pct: float = 0.10
    max_batch_exposure_pct: float = 0.50
    dll_headroom_pct: float = 0.70
    dd_headroom_pct: float = 0.80
    max_per_sector: int = 3
    block_duplicate_symbols: int = 1
    environment: str = "demo"
    instrument_scope: str = "us_equities"
    holding_style: str = "swing"
    webhook_url: Optional[str] = None
    webhook_api_key: Optional[str] = None
    execution_bridge: str = "signalstack"


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    prop_firm: Optional[str] = None
    account_type: Optional[str] = None
    account_size: Optional[int] = None
    daily_loss_limit: Optional[float] = None
    max_drawdown: Optional[float] = None
    max_positions: Optional[int] = None
    must_close_eod: Optional[int] = None
    is_active: Optional[int] = None
    max_single_position_pct: Optional[float] = None
    max_batch_exposure_pct: Optional[float] = None
    dll_headroom_pct: Optional[float] = None
    dd_headroom_pct: Optional[float] = None
    max_per_sector: Optional[int] = None
    block_duplicate_symbols: Optional[int] = None
    environment: Optional[str] = None
    instrument_scope: Optional[str] = None
    holding_style: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_api_key: Optional[str] = None
    execution_bridge: Optional[str] = None


@app.get("/api/v1/accounts")
async def list_accounts() -> dict[str, Any]:
    profiles = acc_module.list_profiles()
    return {"profiles": profiles, "count": len(profiles)}


@app.get("/api/v1/accounts/{profile_id}")
async def get_account(profile_id: str) -> dict[str, Any]:
    profile = acc_module.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Account not found: {profile_id}")
    return profile


@app.post("/api/v1/accounts", status_code=201)
async def create_account(body: AccountCreate) -> dict[str, Any]:
    return acc_module.create_profile(body.model_dump())


@app.put("/api/v1/accounts/{profile_id}")
async def update_account(profile_id: str, body: AccountUpdate) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = acc_module.update_profile(profile_id, data)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Account not found: {profile_id}")
    return updated


@app.delete("/api/v1/accounts/{profile_id}", status_code=204)
async def delete_account(profile_id: str) -> Response:
    deleted = acc_module.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Account not found: {profile_id}")
    return Response(status_code=204)
