"""
context_health.py — V6 operational health gate for live broker context.

The context daemon writes facts. This module is the first place that turns
those facts into READY / HOLD / BLOCK / ERROR for a specific candidate.
Risky remains the policy/risk gate; this is only the operational readiness
gate that runs after policy approval and before routing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from vanguard.config.runtime_config import resolve_market_data_source_label
from vanguard.helpers.clock import iso_utc, now_utc


V6_HEALTH_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_v6_context_health (
    cycle_ts_utc             TEXT NOT NULL,
    profile_id               TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    direction                TEXT NOT NULL,
    health_ts_utc            TEXT NOT NULL,
    context_source_id        TEXT,
    source                   TEXT,
    broker                   TEXT,
    mode                     TEXT,
    v6_state                 TEXT NOT NULL,
    ready_to_route           INTEGER NOT NULL DEFAULT 0,
    reasons_json             TEXT NOT NULL,
    source_snapshot_json     TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, profile_id, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_v6_context_health_state
    ON vanguard_v6_context_health(cycle_ts_utc, profile_id, v6_state);
"""

_MISSING_TABLE = object()


def ensure_v6_context_health_schema(con: sqlite3.Connection) -> None:
    """Create the V6 health audit table if it does not exist."""
    for stmt in V6_HEALTH_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def persist_context_health(con: sqlite3.Connection, health: dict[str, Any]) -> None:
    """Persist one candidate health verdict for audit and downstream debugging."""
    ensure_v6_context_health_schema(con)
    con.execute(
        """
        INSERT OR REPLACE INTO vanguard_v6_context_health
            (cycle_ts_utc, profile_id, symbol, direction, health_ts_utc,
             context_source_id, source, broker, mode, v6_state, ready_to_route,
             reasons_json, source_snapshot_json)
        VALUES
            (:cycle_ts_utc, :profile_id, :symbol, :direction, :health_ts_utc,
             :context_source_id, :source, :broker, :mode, :v6_state, :ready_to_route,
             :reasons_json, :source_snapshot_json)
        """,
        {
            **health,
            "reasons_json": json.dumps(health.get("reasons") or [], sort_keys=True),
            "source_snapshot_json": json.dumps(health.get("source_snapshot") or {}, sort_keys=True, default=str),
        },
    )
    con.commit()


def evaluate_context_health(
    con: sqlite3.Connection,
    *,
    config: dict[str, Any],
    profile: dict[str, Any],
    candidate: dict[str, Any],
    cycle_ts_utc: str,
) -> dict[str, Any]:
    """
    Evaluate live-context readiness for one policy-approved candidate.

    Returns a serialisable dict containing v6_state, ready_to_route, reasons,
    and the exact source snapshot used. It does not mutate portfolio/risk state.
    """
    health_cfg = config.get("v6_health") or {}
    profile_id = str(profile.get("id") or "")
    symbol = str(candidate.get("symbol") or "").replace("/", "").upper()
    asset_class = str(candidate.get("asset_class") or "").lower()
    direction = str(candidate.get("direction") or candidate.get("side") or "").upper()
    source_id = str(profile.get("context_source_id") or health_cfg.get("default_context_source_id") or "")
    source_cfg = (config.get("context_sources") or {}).get(source_id) or {}
    source = str(source_cfg.get("source") or source_id or "unknown")
    broker = str(source_cfg.get("broker") or "unknown")
    mode = str(profile.get("context_health_mode") or profile.get("execution_mode") or config.get("execution_mode") or "manual")
    mode_cfg = (health_cfg.get("mode_requirements") or {}).get(mode) or {}
    required = set(mode_cfg.get("required_context") or [])
    thresholds = health_cfg.get("thresholds") or {}

    base = {
        "cycle_ts_utc": cycle_ts_utc,
        "profile_id": profile_id,
        "symbol": symbol,
        "direction": direction,
        "health_ts_utc": iso_utc(now_utc()),
        "context_source_id": source_id,
        "source": source,
        "broker": broker,
        "mode": mode,
        "v6_state": "READY",
        "ready_to_route": 1,
        "reasons": [],
        "source_snapshot": {
            "required_context": sorted(required),
            "thresholds": thresholds,
        },
    }

    crypto_validation_cfg = health_cfg.get("crypto_validation") or {}
    if asset_class == "crypto" and crypto_validation_cfg.get("enabled", False):
        return _evaluate_crypto_validation_health(
            con,
            base,
            config=config,
            profile_id=profile_id,
            symbol=symbol,
            cycle_ts_utc=cycle_ts_utc,
            now=now_utc(),
            thresholds=thresholds,
            validation_cfg=crypto_validation_cfg,
        )

    if not health_cfg.get("enabled", False):
        base["v6_state"] = "SKIPPED"
        base["ready_to_route"] = 1
        base["source_snapshot"]["health_enabled"] = False
        return base

    if not source_cfg.get("enabled", False):
        _add_reason(base, "BLOCK", "CONTEXT_SOURCE_DISABLED", "daemon", "context_sources", source_id)
        return _finalise(base, mode_cfg)

    account_number = _profile_account_number(source_cfg, profile_id)
    now = now_utc()

    try:
        if "daemon" in required:
            _check_daemon(con, base, source, profile_id, now, thresholds)
        if "quote" in required:
            _check_quote(con, base, source, profile_id, symbol, now, thresholds)
        if "account" in required:
            _check_account(con, base, source, profile_id, account_number, now, thresholds)
        if "positions" in required:
            _check_ingest_component(con, base, source, profile_id, "positions", now, thresholds)
        if "orders" in required:
            _check_ingest_component(con, base, source, profile_id, "orders", now, thresholds)
        if "symbol_specs" in required:
            _check_symbol_specs(con, base, broker, profile_id, symbol, now, thresholds)
    except Exception as exc:  # noqa: BLE001
        _add_reason(base, "ERROR", "CONTEXT_HEALTH_EXCEPTION", "v6_health", "exception", str(exc))

    return _finalise(base, mode_cfg)


def _evaluate_crypto_validation_health(
    con: sqlite3.Connection,
    health: dict[str, Any],
    *,
    config: dict[str, Any],
    profile_id: str,
    symbol: str,
    cycle_ts_utc: str,
    now: datetime,
    thresholds: dict[str, Any],
    validation_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Crypto weekend validation health: prove pipeline, never allow execution."""
    snap = health["source_snapshot"]
    snap["crypto_validation"] = validation_cfg
    active_source = resolve_market_data_source_label("crypto", runtime_config=config)
    snap["crypto_validation_active_source"] = active_source
    row = _fetchone(
        con,
        health,
        "crypto_state",
        """
        SELECT * FROM vanguard_crypto_symbol_state
        WHERE profile_id = ? AND symbol = ?
        ORDER BY cycle_ts_utc DESC
        LIMIT 1
        """,
        (profile_id, symbol),
    )
    if row is _MISSING_TABLE:
        snap["crypto_state"] = None
        _add_reason(health, "ERROR", "CRYPTO_STATE_TABLE_MISSING", "crypto_state", "vanguard_crypto_symbol_state", None)
        return _finalise(health, {"ready_state_allows_execution": False})
    if row is None:
        _add_reason(health, "HOLD", "CRYPTO_STATE_MISSING", "crypto_state", "vanguard_crypto_symbol_state", symbol)
        return _finalise(health, {"ready_state_allows_execution": False})

    state = _rowdict(row)
    snap["crypto_state"] = state
    snap["crypto_state_source_matches_active"] = str(state.get("source") or "") == str(active_source or "")
    if str(state.get("source_status") or "").upper() != "OK":
        _add_reason(health, "HOLD", "CRYPTO_STATE_NOT_OK", "crypto_state", "source_status", state.get("source_status"))
    if state.get("cycle_ts_utc") != cycle_ts_utc:
        snap["crypto_state_cycle_mismatch"] = {"candidate": cycle_ts_utc, "state": state.get("cycle_ts_utc")}

    quote_age_ms = _age_ms(state.get("quote_ts_utc"), now)
    snap["crypto_quote_age_ms"] = quote_age_ms
    max_age = int(validation_cfg.get("quote_max_age_ms") or thresholds.get("quote_max_age_ms") or 900000)
    if quote_age_ms is None or quote_age_ms > max_age:
        _add_reason(health, "HOLD", "CRYPTO_QUOTE_STALE", "crypto_state", "quote_ts_utc", quote_age_ms, max_age)

    source_row = _fetchone(
        con,
        health,
        "source_health",
        """
        SELECT * FROM vanguard_source_health
        WHERE data_source = ? AND asset_class = 'crypto'
        ORDER BY last_updated DESC
        LIMIT 1
        """,
        (str(active_source or state.get("source") or "unknown"),),
    )
    if source_row not in (_MISSING_TABLE, None):
        snap["crypto_source_health"] = _rowdict(source_row)
        if str(source_row["status"] or "").lower() not in {"live", "ok"}:
            _add_reason(health, "HOLD", "CRYPTO_SOURCE_NOT_LIVE", "source_health", "status", source_row["status"])

    if bool(validation_cfg.get("block_execution", True)):
        _add_reason(
            health,
            "HOLD",
            "CRYPTO_VALIDATION_MODE_EXECUTION_DISABLED",
            "v6_health",
            "crypto_validation.block_execution",
            True,
        )
    return _finalise(health, {"ready_state_allows_execution": False})


def apply_health_to_tradeable_row(row: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """Attach health columns and downgrade status when operationally unsafe."""
    out = dict(row)
    state = str(health.get("v6_state") or "ERROR")
    reasons = health.get("reasons") or []
    first_code = str(reasons[0].get("code")) if reasons else None
    out["v6_state"] = state
    out["v6_ready_to_route"] = int(health.get("ready_to_route") or 0)
    out["v6_reasons_json"] = json.dumps(reasons, sort_keys=True)
    out["v6_source_snapshot_json"] = json.dumps(health.get("source_snapshot") or {}, sort_keys=True, default=str)

    if state == "READY":
        return out
    if state == "HOLD":
        out["status"] = "HOLD"
    elif state == "BLOCK":
        out["status"] = "BLOCKED"
    elif state == "SKIPPED":
        return out
    else:
        out["status"] = "ERROR"
    out["rejection_reason"] = first_code or state
    return out


def _profile_account_number(source_cfg: dict[str, Any], profile_id: str) -> str:
    profiles = source_cfg.get("profiles") or {}
    profile_cfg = profiles.get(profile_id) or {}
    return str(profile_cfg.get("account_number") or "")


def _check_daemon(
    con: sqlite3.Connection,
    health: dict[str, Any],
    source: str,
    profile_id: str,
    now: datetime,
    thresholds: dict[str, Any],
) -> None:
    row = _fetchone(
        con,
        health,
        "daemon",
        """
        SELECT * FROM vanguard_context_ingest_runs
        WHERE profile_id = ? AND source = ?
        ORDER BY finished_ts_utc DESC
        LIMIT 1
        """,
        (profile_id, source),
    )
    snap = health["source_snapshot"]
    if row is _MISSING_TABLE:
        snap["daemon"] = None
        return
    snap["daemon"] = _rowdict(row)
    if row is None:
        _add_reason(health, "HOLD", "CONTEXT_DAEMON_MISSING", "daemon", "vanguard_context_ingest_runs", None)
        return
    age_ms = _age_ms(row["finished_ts_utc"], now)
    snap["daemon_age_ms"] = age_ms
    max_age = int(thresholds.get("ingest_run_max_age_ms") or 10000)
    if age_ms is None or age_ms > max_age:
        _add_reason(health, "HOLD", "CONTEXT_DAEMON_STALE", "daemon", "finished_ts_utc", age_ms, max_age)
    if str(row["source_status"] or "").upper() == "ERROR":
        _add_reason(health, "ERROR", "CONTEXT_DAEMON_ERROR", "daemon", "source_status", row["source_error"])


def _check_quote(
    con: sqlite3.Connection,
    health: dict[str, Any],
    source: str,
    profile_id: str,
    symbol: str,
    now: datetime,
    thresholds: dict[str, Any],
) -> None:
    row = _fetchone(
        con,
        health,
        "quote",
        """
        SELECT * FROM vanguard_context_quote_latest
        WHERE profile_id = ? AND source = ? AND symbol = ?
        """,
        (profile_id, source, symbol),
    )
    snap = health["source_snapshot"]
    if row is _MISSING_TABLE:
        snap["quote"] = None
        return
    snap["quote"] = _rowdict(row)
    if row is None:
        _add_reason(health, "HOLD", "QUOTE_MISSING", "quote", "vanguard_context_quote_latest", symbol)
        return
    age_ms = _age_ms(row["received_ts_utc"], now)
    snap["quote_age_ms"] = age_ms
    max_age = int(thresholds.get("quote_max_age_ms") or 5000)
    if age_ms is None or age_ms > max_age:
        _add_reason(health, "HOLD", "QUOTE_STALE", "quote", "received_ts_utc", age_ms, max_age)
    bid = float(row["bid"] or 0.0)
    ask = float(row["ask"] or 0.0)
    if bid <= 0 or ask <= 0:
        _add_reason(health, "HOLD", "QUOTE_ZERO_BID_ASK", "quote", "bid_ask", {"bid": bid, "ask": ask})
    elif ask < bid:
        _add_reason(health, "HOLD", "QUOTE_ASK_BELOW_BID", "quote", "bid_ask", {"bid": bid, "ask": ask})
    if str(row["source_status"] or "").upper() == "ERROR":
        _add_reason(health, "ERROR", "QUOTE_SOURCE_ERROR", "quote", "source_error", row["source_error"])


def _check_account(
    con: sqlite3.Connection,
    health: dict[str, Any],
    source: str,
    profile_id: str,
    account_number: str,
    now: datetime,
    thresholds: dict[str, Any],
) -> None:
    params: tuple[Any, ...]
    sql = """
        SELECT * FROM vanguard_context_account_latest
        WHERE profile_id = ? AND source = ?
    """
    params = (profile_id, source)
    if account_number:
        sql += " AND account_number = ?"
        params = (profile_id, source, account_number)
    sql += " ORDER BY received_ts_utc DESC LIMIT 1"
    row = _fetchone(con, health, "account", sql, params)
    snap = health["source_snapshot"]
    if row is _MISSING_TABLE:
        snap["account"] = None
        return
    snap["account"] = _rowdict(row)
    if row is None:
        _add_reason(health, "HOLD", "ACCOUNT_MISSING", "account", "vanguard_context_account_latest", account_number)
        return
    age_ms = _age_ms(row["received_ts_utc"], now)
    snap["account_age_ms"] = age_ms
    max_age = int(thresholds.get("account_max_age_ms") or 15000)
    if age_ms is None or age_ms > max_age:
        _add_reason(health, "HOLD", "ACCOUNT_STALE", "account", "received_ts_utc", age_ms, max_age)
    for field in ("balance", "equity", "free_margin"):
        if float(row[field] or 0.0) <= 0:
            _add_reason(health, "HOLD", f"ACCOUNT_{field.upper()}_ZERO", "account", field, row[field])
    if str(row["source_status"] or "").upper() == "ERROR":
        _add_reason(health, "ERROR", "ACCOUNT_SOURCE_ERROR", "account", "source_error", row["source_error"])


def _check_ingest_component(
    con: sqlite3.Connection,
    health: dict[str, Any],
    source: str,
    profile_id: str,
    component: str,
    now: datetime,
    thresholds: dict[str, Any],
) -> None:
    row = _fetchone(
        con,
        health,
        component,
        """
        SELECT * FROM vanguard_context_ingest_runs
        WHERE profile_id = ? AND source = ?
        ORDER BY finished_ts_utc DESC
        LIMIT 1
        """,
        (profile_id, source),
    )
    if row is _MISSING_TABLE:
        return
    if row is None:
        _add_reason(health, "HOLD", f"{component.upper()}_SNAPSHOT_MISSING", component, "vanguard_context_ingest_runs", None)
        return
    age_ms = _age_ms(row["finished_ts_utc"], now)
    max_age = int(thresholds.get(f"{component}_max_age_ms") or thresholds.get("ingest_run_max_age_ms") or 15000)
    health["source_snapshot"][f"{component}_age_ms"] = age_ms
    health["source_snapshot"][f"{component}_count"] = row[f"{component}_written"] if f"{component}_written" in row.keys() else None
    if age_ms is None or age_ms > max_age:
        _add_reason(health, "HOLD", f"{component.upper()}_SNAPSHOT_STALE", component, "finished_ts_utc", age_ms, max_age)


def _check_symbol_specs(
    con: sqlite3.Connection,
    health: dict[str, Any],
    broker: str,
    profile_id: str,
    symbol: str,
    now: datetime,
    thresholds: dict[str, Any],
) -> None:
    row = _fetchone(
        con,
        health,
        "symbol_specs",
        """
        SELECT * FROM vanguard_context_symbol_specs
        WHERE broker = ? AND profile_id = ? AND symbol = ?
        """,
        (broker, profile_id, symbol),
    )
    if row is _MISSING_TABLE:
        health["source_snapshot"]["symbol_specs"] = None
        return
    health["source_snapshot"]["symbol_specs"] = _rowdict(row)
    if row is None:
        _add_reason(health, "HOLD", "SYMBOL_SPECS_MISSING", "symbol_specs", "vanguard_context_symbol_specs", symbol)
        return
    age_ms = _age_ms(row["updated_ts_utc"], now)
    max_age = int(thresholds.get("symbol_specs_max_age_minutes") or 1440) * 60 * 1000
    health["source_snapshot"]["symbol_specs_age_ms"] = age_ms
    if age_ms is None or age_ms > max_age:
        _add_reason(health, "HOLD", "SYMBOL_SPECS_STALE", "symbol_specs", "updated_ts_utc", age_ms, max_age)
    trade_allowed = row["trade_allowed"]
    if trade_allowed is not None and int(trade_allowed) == 0:
        _add_reason(health, "BLOCK", "SYMBOL_NOT_TRADEABLE", "symbol_specs", "trade_allowed", trade_allowed)


def _add_reason(
    health: dict[str, Any],
    severity: str,
    code: str,
    component: str,
    field: str,
    observed: Any,
    threshold: Any | None = None,
) -> None:
    reason = {
        "severity": severity,
        "code": code,
        "component": component,
        "field": field,
        "observed": observed,
    }
    if threshold is not None:
        reason["threshold"] = threshold
    health.setdefault("reasons", []).append(reason)


def _finalise(health: dict[str, Any], mode_cfg: dict[str, Any]) -> dict[str, Any]:
    severities = {str(r.get("severity") or "") for r in health.get("reasons") or []}
    if "ERROR" in severities:
        state = "ERROR"
    elif "BLOCK" in severities:
        state = "BLOCK"
    elif "HOLD" in severities:
        state = "HOLD"
    else:
        state = "READY"
    health["v6_state"] = state
    allow_route = bool(mode_cfg.get("ready_state_allows_execution", state == "READY"))
    health["ready_to_route"] = 1 if state == "READY" and allow_route else 0
    return health


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _fetchone(
    con: sqlite3.Connection,
    health: dict[str, Any],
    component: str,
    sql: str,
    params: tuple[Any, ...],
) -> sqlite3.Row | object | None:
    try:
        return con.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "no such table" in message.lower():
            _add_reason(
                health,
                "HOLD",
                f"{component.upper()}_CONTEXT_TABLE_MISSING",
                component,
                "sqlite_table",
                message,
            )
            return _MISSING_TABLE
        raise


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_ms(value: Any, now: datetime) -> int | None:
    dt = _parse_ts(value)
    if dt is None:
        return None
    return int((now - dt).total_seconds() * 1000)
