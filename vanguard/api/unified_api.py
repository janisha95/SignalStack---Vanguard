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
from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .field_registry import FIELD_REGISTRY
from . import userviews as uv_module
from . import trade_desk as td_module
from . import reports as rpt_module
from . import accounts as acc_module
from .adapters import meridian_adapter, s1_adapter, vanguard_adapter
from vanguard.config.runtime_config import (
    get_runtime_config,
    get_shadow_db_path,
    save_runtime_config,
)
from vanguard.helpers.clock import now_et
from vanguard.helpers.db import sqlite_conn
from vanguard.helpers.universe_builder import classify_asset_class

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("unified_api")
_DB_PATH = Path(get_shadow_db_path())
_RISKY_BASE_URL = "http://127.0.0.1:8092"
_RISKY_TIMEOUT_SECONDS = 2.0
_RISKY_STARTUP_TIMEOUT_SECONDS = 1.0
_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3007",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3007",
    "https://signalstack-app.vercel.app",
]


class RiskyUnavailableError(RuntimeError):
    """Raised when the local Risky service cannot be reached."""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize local tables plus the shared Risky client."""
    app.state.risky_client = httpx.AsyncClient(
        base_url=_RISKY_BASE_URL,
        timeout=httpx.Timeout(_RISKY_TIMEOUT_SECONDS),
    )

    uv_module.init_table()
    uv_module.seed_system_views()
    td_module.init_table()
    rpt_module.init_table()
    acc_module.init_table()
    acc_module.seed_profiles()
    logger.info("Unified API started — DB tables ready")

    try:
        risky_health = await _fetch_risky_health(app, timeout=_RISKY_STARTUP_TIMEOUT_SECONDS)
        logger.info(
            "Risky reachable, config_version=%s, checks_enabled=%s",
            risky_health.get("config_version"),
            risky_health.get("checks_enabled"),
        )
    except RiskyUnavailableError as exc:
        logger.warning("Risky health check failed at startup: %s", exc)
    except Exception as exc:
        logger.warning("Risky startup probe returned an unexpected error: %s", exc)

    try:
        yield
    finally:
        await app.state.risky_client.aclose()


app = FastAPI(
    title="SignalStack Unified API",
    version="1.0.0",
    description="Unified candidate data from Meridian, S1, and Vanguard.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def _risky_required() -> bool:
    raw = (_get_config_value("RISKY_REQUIRED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _format_httpx_error(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


async def _request_risky(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    client: httpx.AsyncClient = app.state.risky_client
    try:
        return await client.request(
            method,
            path,
            json=json_body,
            params=params,
            timeout=timeout,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise RiskyUnavailableError(_format_httpx_error(exc)) from exc


async def _fetch_risky_health(
    app: FastAPI,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    response = await _request_risky(app, "GET", "/risk/health", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    checks_enabled = payload.get("checks_enabled") or {}
    enabled_count = sum(1 for enabled in checks_enabled.values() if enabled)
    return {
        "reachable": True,
        "config_version": payload.get("config_version"),
        "checks_enabled": enabled_count,
        "mode": payload.get("mode"),
        "raw": payload,
    }


async def _get_risky_status_snapshot(request: Request) -> dict[str, Any]:
    try:
        return await _fetch_risky_health(request.app)
    except (RiskyUnavailableError, httpx.HTTPError) as exc:
        logger.warning("system status Risky probe failed: %s", exc)
        return {
            "reachable": False,
            "config_version": None,
            "checks_enabled": None,
            "mode": None,
        }


def _risky_unreachable_response(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "risky_unreachable", "message": message},
    )


def _proxy_response_from_httpx(response: httpx.Response) -> Response:
    media_type = response.headers.get("content-type", "application/json").split(";", 1)[0]
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=media_type,
    )


def _build_risky_order(trade: TradeItem, profile_id: str | None) -> dict[str, Any]:
    qty = trade.quantity if trade.quantity is not None else trade.shares
    return {
        "symbol": trade.symbol.upper(),
        "side": trade.direction.upper(),
        "qty": float(qty),
        "asset_class": classify_asset_class(trade.symbol),
        "intended_entry": float(trade.entry_price or 0.0),
        "intended_stop": float(trade.stop_loss or 0.0),
        "account_id": profile_id or "default",
    }


def _collapse_risky_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not verdicts:
        return None
    if len(verdicts) == 1:
        return verdicts[0]
    return {"batch": verdicts}


async def _check_trades_with_risky(
    request: Request,
    trades: list[TradeItem],
    profile_id: str | None,
) -> list[dict[str, Any]] | JSONResponse:
    verdicts: list[dict[str, Any]] = []
    for trade in trades:
        order_payload = _build_risky_order(trade, profile_id)
        try:
            response = await _request_risky(
                request.app,
                "POST",
                "/risk/check",
                json_body=order_payload,
            )
        except RiskyUnavailableError as exc:
            if _risky_required():
                return _risky_unreachable_response(str(exc))
            logger.warning(
                "Risky unreachable with RISKY_REQUIRED=false — proceeding without gating: %s",
                exc,
            )
            return []

        if response.status_code >= 400:
            return JSONResponse(
                status_code=response.status_code,
                content=response.json(),
            )

        verdict = response.json()
        if not verdict.get("approved", False):
            return JSONResponse(
                status_code=422,
                content={"status": "rejected_by_risky", "verdict": verdict},
            )

        logger.info(
            "Risky approved %s %s severity=%s config_version=%s",
            order_payload["side"],
            order_payload["symbol"],
            verdict.get("severity"),
            verdict.get("config_version"),
        )
        verdicts.append(verdict)

    return verdicts


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
async def system_status(request: Request) -> dict[str, Any]:
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
    risky_status = await _get_risky_status_snapshot(request)

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
        "risky": risky_status,
    }


@app.get("/api/v1/runtime/resolved-universe")
async def get_resolved_universe() -> dict[str, Any]:
    """Return the most recent resolved universe snapshot (Phase 2a debug endpoint)."""
    import json as _json
    try:
        with sqlite3.connect(_DB_PATH) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT * FROM vanguard_resolved_universe_log
                ORDER BY cycle_ts_utc DESC LIMIT 1
                """
            ).fetchone()
        if not row:
            return {"status": "no_data", "message": "No resolved universe log entries yet"}
        d = dict(row)
        # Parse JSON arrays stored as text
        for col in ("active_profile_ids", "expected_asset_classes", "in_scope_symbols"):
            try:
                d[col] = _json.loads(d[col])
            except Exception:
                pass
        return d
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/config/runtime")
async def get_runtime_config_endpoint() -> dict[str, Any]:
    """Return the active QA/shadow runtime config snapshot."""
    return {
        "status": "ok",
        "config": get_runtime_config(refresh=True),
    }


@app.put("/api/v1/config/runtime")
async def put_runtime_config_endpoint(body: RuntimeConfigUpdate) -> dict[str, Any]:
    """Validate and persist the QA/shadow runtime config document."""
    try:
        saved = save_runtime_config(body.config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "config": saved,
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


# ── Daemon Heartbeat ──────────────────────────────────────────────────────────

_LIFECYCLE_LOG   = Path("/tmp/vanguard_lifecycle_qa.log")
_LIFECYCLE_PID   = Path("/tmp/vanguard_lifecycle_qa.pid")
_LOG_WARN_SECS   = 300   # 5 min without log update → warn
_LOG_DEAD_SECS   = 600   # 10 min without log update → not_alive
_DB_WARN_SECS    = 300   # 5 min since last DB write → warn


@app.get("/api/v1/daemon_health")
async def daemon_health() -> dict[str, Any]:
    """
    Lifecycle daemon liveness check.

    Returns:
      alive        – process detected by PID file or pgrep
      log_age_s    – seconds since lifecycle log was last written
      db_age_s     – seconds since vanguard_account_state was last updated
      status       – "ok" | "warn" | "dead"
      detail       – human-readable summary
    """
    import subprocess
    import time

    now = time.time()

    # ── 1. Process liveness ───────────────────────────────────────────────────
    alive = False
    live_pid: int | None = None

    pid_str = _LIFECYCLE_PID.read_text().strip() if _LIFECYCLE_PID.exists() else ""
    if pid_str.isdigit():
        try:
            os.kill(int(pid_str), 0)   # signal 0 = existence check only
            alive = True
            live_pid = int(pid_str)
        except (ProcessLookupError, PermissionError):
            pass

    if not alive:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "lifecycle_daemon"],
                capture_output=True, text=True, timeout=3,
            )
            pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
            if pids:
                alive = True
                live_pid = int(pids[0])
        except Exception:
            pass

    # ── 2. Log freshness ──────────────────────────────────────────────────────
    log_age_s: int | None = None
    if _LIFECYCLE_LOG.exists():
        log_age_s = int(now - _LIFECYCLE_LOG.stat().st_mtime)

    # ── 3. DB freshness ───────────────────────────────────────────────────────
    db_age_s: int | None = None
    try:
        with sqlite3.connect(_DB_PATH) as con:
            row = con.execute(
                "SELECT MAX(updated_at_utc) FROM vanguard_account_state"
            ).fetchone()
        if row and row[0]:
            last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            db_age_s = int(now - last_ts.timestamp())
    except Exception:
        pass

    # ── 4. Derive status ──────────────────────────────────────────────────────
    problems: list[str] = []
    if not alive:
        problems.append("process not found")
    if log_age_s is not None and log_age_s > _LOG_DEAD_SECS:
        problems.append(f"log silent for {log_age_s}s")
    elif log_age_s is not None and log_age_s > _LOG_WARN_SECS:
        problems.append(f"log stale for {log_age_s}s")
    if db_age_s is not None and db_age_s > _DB_WARN_SECS:
        problems.append(f"DB not updated for {db_age_s}s")

    if not alive or (log_age_s is not None and log_age_s > _LOG_DEAD_SECS):
        status = "dead"
    elif problems:
        status = "warn"
    else:
        status = "ok"

    detail = "; ".join(problems) if problems else "all checks pass"

    return {
        "status":     status,
        "alive":      alive,
        "pid":        live_pid,
        "log_age_s":  log_age_s,
        "db_age_s":   db_age_s,
        "detail":     detail,
    }


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
    quantity: Optional[float] = None   # exact fractional qty (V6-authoritative for crypto/forex)
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


class ExecuteResponse(BaseModel):
    approved: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    summary: dict[str, Any]
    submitted: int
    failed: int
    results: list[dict[str, Any]]
    risky_verdict: Optional[dict[str, Any]] = None


class RuntimeConfigUpdate(BaseModel):
    config: dict[str, Any]


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


@app.get("/api/v1/risk/health")
async def risky_health_proxy(request: Request) -> Response:
    try:
        response = await _request_risky(request.app, "GET", "/risk/health")
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.get("/api/v1/risk/config")
async def risky_config_proxy(request: Request) -> Response:
    try:
        response = await _request_risky(request.app, "GET", "/risk/config")
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.post("/api/v1/risk/config")
async def risky_config_update_proxy(request: Request) -> Response:
    try:
        payload = await request.json()
        response = await _request_risky(request.app, "POST", "/risk/config", json_body=payload)
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.post("/api/v1/risk/check")
async def risky_check_proxy(request: Request) -> Response:
    try:
        payload = await request.json()
        response = await _request_risky(request.app, "POST", "/risk/check", json_body=payload)
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.post("/api/v1/risk/check_batch")
async def risky_check_batch_proxy(request: Request) -> Response:
    try:
        payload = await request.json()
        response = await _request_risky(request.app, "POST", "/risk/check_batch", json_body=payload)
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.get("/api/v1/risk/history")
async def risky_history_proxy(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> Response:
    try:
        response = await _request_risky(
            request.app,
            "GET",
            "/risk/history",
            params={"limit": limit},
        )
    except RiskyUnavailableError as exc:
        return _risky_unreachable_response(str(exc))
    return _proxy_response_from_httpx(response)


@app.post("/api/v1/execute", response_model=ExecuteResponse)
async def execute_trades(body: ExecuteRequest, request: Request) -> ExecuteResponse | JSONResponse:
    risky_check = await _check_trades_with_risky(request, body.trades, body.profile_id)
    if isinstance(risky_check, JSONResponse):
        return risky_check

    trades = [t.model_dump() for t in body.trades]
    result = td_module.execute_trades(
        trades,
        profile_id=body.profile_id,
        preview_only=body.preview_only,
    )
    result["risky_verdict"] = _collapse_risky_verdicts(risky_check)
    return ExecuteResponse(**result)


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
_VANGUARD_BARS_DB = Path(_DB_PATH)


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


@app.get("/api/v1/vanguard_sizing/{symbol}/{direction}")
async def get_vanguard_sizing(symbol: str, direction: str) -> dict[str, Any]:
    """
    Return intraday ATR(14) and position sizing stops for a symbol.

    Uses vanguard_bars_5m (5-minute bars) from the Vanguard universe DB — the
    same data source and same AVG(high-low) formula V6 uses in _enrich_with_prices().
    Falls back to vanguard_bars_1h if 5m has no data for the symbol.

    Returns atr_14=null with fallback=true if neither table has the symbol, so
    Risky's existing fallback logic can warn-and-proceed rather than crash.
    """
    ticker = symbol.upper()
    is_long = direction.upper() == "LONG"

    if not _VANGUARD_BARS_DB.exists():
        raise HTTPException(status_code=503, detail="Vanguard bars DB unavailable")

    atr: float | None = None
    price: float | None = None

    try:
        with sqlite_conn(str(_VANGUARD_BARS_DB)) as con:
            for table in ("vanguard_bars_5m", "vanguard_bars_1h"):
                if atr is not None:
                    break

                # Latest close
                price_row = con.execute(
                    f"""
                    SELECT b.close
                    FROM {table} b
                    INNER JOIN (
                        SELECT symbol, MAX(bar_ts_utc) AS max_ts
                        FROM {table} WHERE symbol = ?
                    ) latest ON b.symbol = latest.symbol AND b.bar_ts_utc = latest.max_ts
                    """,
                    (ticker,),
                ).fetchone()
                if not price_row or not price_row[0]:
                    continue
                price = float(price_row[0])

                # ATR: AVG(high - low) over last 14 bars — same formula as V6 _enrich_with_prices()
                atr_row = con.execute(
                    f"""
                    SELECT AVG(high - low) AS avg_hl
                    FROM (
                        SELECT high, low,
                               ROW_NUMBER() OVER (ORDER BY bar_ts_utc DESC) rn
                        FROM {table} WHERE symbol = ?
                    ) WHERE rn <= 14
                    """,
                    (ticker,),
                ).fetchone()
                if atr_row and atr_row[0]:
                    atr = float(atr_row[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if atr is None or price is None:
        # Symbol not in Vanguard bars — return null ATR so Risky's fallback path fires
        return {
            "symbol": ticker,
            "direction": direction.upper(),
            "price": None,
            "atr_14": None,
            "atr_pct": None,
            "fallback": True,
            "fallback_reason": f"No 5m or 1h bars found for {ticker} in vanguard_universe.db",
            "stops": None,
        }

    def stop_at(mult: float) -> dict[str, Any]:
        dist = round(atr * mult, 6)
        stop_price = round(price - dist, 6) if is_long else round(price + dist, 6)
        return {"distance": dist, "price": stop_price}

    return {
        "symbol": ticker,
        "direction": direction.upper(),
        "price": round(price, 6),
        "atr_14": round(atr, 6),
        "atr_pct": round(atr / price * 100, 4) if price else 0.0,
        "fallback": False,
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

def _is_stale(updated_at_utc: str | None, threshold_minutes: int = 5) -> bool:
    """Return True if the timestamp is older than threshold_minutes from now."""
    if not updated_at_utc:
        return True
    try:
        from datetime import timezone as _tz
        ts = datetime.fromisoformat(updated_at_utc.replace("Z", "+00:00"))
        age_s = (datetime.now(_tz.utc) - ts).total_seconds()
        return age_s > threshold_minutes * 60
    except Exception:
        return True


def _read_open_positions_for_profile(profile_id: str) -> list[dict[str, Any]]:
    """Read live open positions from the broker-mirrored vanguard_open_positions table."""
    try:
        with sqlite3.connect(str(_DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT broker_position_id, symbol, side, qty,
                       entry_price, current_sl, current_tp,
                       unrealized_pnl, opened_at_utc, last_synced_at_utc
                FROM vanguard_open_positions
                WHERE profile_id = ?
                ORDER BY opened_at_utc DESC
                """,
                (profile_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("_read_open_positions_for_profile %s: %s", profile_id, exc)
        return []


def _read_account_state_for_profile_unified(profile_id: str) -> dict[str, Any] | None:
    """Read live account state from the broker-mirrored vanguard_account_state table.
    Thin wrapper so unified_api doesn't import from trade_desk (avoids circular imports).
    The authoritative implementation is td_module._read_account_state_for_profile().
    """
    return td_module._read_account_state_for_profile(profile_id)


@app.get("/api/v1/portfolio/live")
async def get_live_portfolio(
    profile_id: Optional[str] = Query(None, description="Profile ID to scope positions (e.g. gft_10k)"),
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
) -> dict[str, Any]:
    """
    Return open positions with unrealized P&L.

    For metaapi profiles (GFT): reads from vanguard_open_positions (broker-mirrored
    by lifecycle daemon every 60s). source="broker_truth".

    For signalstack profiles (TTP) or when no profile_id given: reads from
    execution_log with live prices from yfinance. source="journal_derived".

    Adds stale=true + warning if account_state is >5 minutes old.
    """
    date_str = date or str(date_type.today())

    # Determine broker kind for this profile
    broker_kind = ""
    if profile_id:
        profile = acc_module.get_profile(profile_id)
        bridge = str((profile or {}).get("execution_bridge") or "")
        broker_kind = bridge.split("_")[0]  # "metaapi" | "mt5" | "signalstack" | ""

    if broker_kind == "metaapi" and profile_id:
        # ── Broker-truth path for MetaApi (GFT) profiles ──────────────────
        broker_positions = _read_open_positions_for_profile(profile_id)
        acct_state = _read_account_state_for_profile_unified(profile_id)

        positions = []
        total_pnl = 0.0
        winners = 0
        losers = 0
        for bp in broker_positions:
            unr = float(bp.get("unrealized_pnl") or 0.0)
            total_pnl += unr
            if unr > 0:
                winners += 1
            elif unr < 0:
                losers += 1
            side = str(bp.get("side") or "LONG").upper()
            # Map vanguard_open_positions columns to PortfolioPosition shape
            positions.append({
                "id": str(bp.get("broker_position_id") or ""),
                "symbol": str(bp.get("symbol") or ""),
                "direction": side,
                "shares": float(bp.get("qty") or 0.0),
                "entry_price": float(bp.get("entry_price") or 0.0),
                "fill_price": float(bp.get("entry_price") or 0.0),
                "filled_at": bp.get("opened_at_utc"),
                "execution_fee": None,
                "stop_loss": float(bp["current_sl"]) if bp.get("current_sl") is not None else None,
                "take_profit": float(bp["current_tp"]) if bp.get("current_tp") is not None else None,
                "live_price": None,       # broker gives unrealized_pnl directly
                "unrealized_pnl": unr,
                "unrealized_pct": None,
                "tier": "broker",
                "tags": None,
                "executed_at": bp.get("opened_at_utc"),
                "account": profile_id,
                "broker_position_id": str(bp.get("broker_position_id") or ""),
                "last_synced_at_utc": bp.get("last_synced_at_utc"),
            })

        resp: dict[str, Any] = {
            "positions": positions,
            "summary": {
                "total_unrealized_pnl": round(total_pnl, 2),
                "count": len(positions),
                "winners": winners,
                "losers": losers,
            },
            "as_of": now_et().isoformat(),
            "source": "broker_truth",
            "profile_id": profile_id,
        }

        # Account state + stale guard
        if acct_state:
            resp["account_state"] = {
                "equity": float(acct_state.get("equity") or 0.0),
                "starting_equity_today": float(acct_state.get("starting_equity_today") or 0.0),
                "daily_pnl_pct": float(acct_state.get("daily_pnl_pct") or 0.0),
                "trailing_dd_pct": float(acct_state.get("trailing_dd_pct") or 0.0),
                "updated_at_utc": acct_state.get("updated_at_utc"),
            }
            if _is_stale(acct_state.get("updated_at_utc"), threshold_minutes=5):
                resp["stale"] = True
                resp["warning"] = (
                    "Account state is stale by > 5 minutes — lifecycle daemon may not be running. "
                    "Check qa_servers.sh."
                )
            else:
                resp["stale"] = False

        return resp

    # ── Journal-derived path for signalstack (TTP) or no profile_id ───────
    all_rows = td_module.get_execution_log(date=date_str, limit=500)
    open_rows = [r for r in all_rows if not r.get("outcome") or r["outcome"] == "OPEN"]

    # Scope to profile if provided (signalstack profiles have account tag)
    if profile_id:
        open_rows = [r for r in open_rows if str(r.get("account") or "") == profile_id]

    if not open_rows:
        return {
            "positions": [],
            "summary": {"total_unrealized_pnl": 0.0, "count": 0, "winners": 0, "losers": 0},
            "as_of": now_et().isoformat(),
            "source": "journal_derived",
            "profile_id": profile_id,
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
        "source": "journal_derived",
        "profile_id": profile_id,
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
