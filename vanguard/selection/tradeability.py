"""
tradeability.py — V5.1 economics and feasibility.

V5.1 reads V4B predictions plus DB-backed asset-state truth and writes
PASS/WEAK/FAIL economics. It does not compute streaks, route tiers, display
labels, final V5 actions, Risky policy, or executor routing.
"""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vanguard.helpers.clock import iso_utc, now_utc
from vanguard.config.runtime_config import get_runtime_config
from vanguard.risk.risky_policy_adapter import load_risk_rules, resolve_risk_profile_id


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "vanguard_v5_tradeability.json"


V5_TRADEABILITY_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_v5_tradeability (
    cycle_ts_utc              TEXT NOT NULL,
    profile_id                TEXT NOT NULL,
    symbol                    TEXT NOT NULL,
    asset_class               TEXT NOT NULL,
    direction                 TEXT NOT NULL,
    evaluated_ts_utc          TEXT NOT NULL,
    model_id                  TEXT,
    predicted_return          REAL,
    predicted_move_pips       REAL,
    predicted_move_bps        REAL,
    spread_pips               REAL,
    spread_bps                REAL,
    cost_pips                 REAL,
    cost_bps                  REAL,
    after_cost_pips           REAL,
    after_cost_bps            REAL,
    economics_state           TEXT NOT NULL,
    v5_action_prelim          TEXT NOT NULL,
    reasons_json              TEXT NOT NULL,
    metrics_json              TEXT NOT NULL,
    pair_state_ref_json       TEXT NOT NULL,
    context_snapshot_ref_json TEXT NOT NULL,
    config_version            TEXT,

    -- Compatibility columns retained until V5.2 owns final selection output.
    rank_value                REAL,
    direction_streak          INTEGER NOT NULL DEFAULT 0,
    strong_prediction_streak  INTEGER NOT NULL DEFAULT 0,
    top_rank_streak           INTEGER NOT NULL DEFAULT 0,
    same_bucket_streak        INTEGER NOT NULL DEFAULT 0,
    flip_count_window         INTEGER NOT NULL DEFAULT 0,
    live_followthrough_pips   REAL,
    live_followthrough_state  TEXT NOT NULL DEFAULT 'NOT_EVALUATED',
    v5_action                 TEXT NOT NULL DEFAULT 'UNASSIGNED',
    route_tier                TEXT NOT NULL DEFAULT 'UNASSIGNED',
    display_label             TEXT NOT NULL DEFAULT 'UNASSIGNED',
    context_snapshot_json     TEXT NOT NULL DEFAULT '{}',

    PRIMARY KEY (cycle_ts_utc, profile_id, symbol, direction)
);
"""

V5_TRADEABILITY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_v5_tradeability_economics
    ON vanguard_v5_tradeability(cycle_ts_utc, profile_id, economics_state);
"""


_MIGRATION_COLUMNS: dict[str, str] = {
    "economics_state": "TEXT",
    "v5_action_prelim": "TEXT",
    "pair_state_ref_json": "TEXT",
    "context_snapshot_ref_json": "TEXT",
    "predicted_move_bps": "REAL",
    "spread_bps": "REAL",
    "cost_bps": "REAL",
    "after_cost_bps": "REAL",
}


def ensure_v5_tradeability_schema(con: sqlite3.Connection) -> None:
    """Create or migrate the V5.1 tradeability audit table."""
    for stmt in V5_TRADEABILITY_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    existing = _table_columns(con, "vanguard_v5_tradeability")
    for column, ddl_type in _MIGRATION_COLUMNS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE vanguard_v5_tradeability ADD COLUMN {column} {ddl_type}")
    for stmt in V5_TRADEABILITY_INDEX_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def load_tradeability_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        return {"schema_version": "v5_tradeability_v1", "defaults": {}}
    return json.loads(cfg_path.read_text())


@lru_cache(maxsize=1)
def _runtime_profiles_by_id() -> dict[str, dict[str, Any]]:
    runtime = get_runtime_config()
    return {
        str(profile.get("id") or "").strip(): dict(profile)
        for profile in (runtime.get("profiles") or [])
        if str(profile.get("id") or "").strip()
    }


@lru_cache(maxsize=1)
def _risk_rules_payload() -> dict[str, Any]:
    return load_risk_rules()


def _effective_thresholds(
    *,
    profile_id: str,
    asset_class: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(thresholds or {})
    if asset_class != "forex":
        return merged

    profile = _runtime_profiles_by_id().get(profile_id) or {"id": profile_id}
    risk_rules = _risk_rules_payload()
    risk_profile_id = resolve_risk_profile_id(profile)
    account_rules = dict((risk_rules.get("per_account_profiles") or {}).get(risk_profile_id) or {})
    universal_rules = dict(risk_rules.get("universal_rules") or {})
    spread_sanity = dict(universal_rules.get("spread_sanity") or {})

    if not bool(spread_sanity.get("enabled", False)):
        return merged

    block_ratio = spread_sanity.get("block_ratio")
    if block_ratio is not None:
        merged["max_spread_ratio_vs_baseline"] = float(block_ratio)

    absolute_cap_pips = spread_sanity.get("absolute_cap_pips")
    if absolute_cap_pips is not None:
        merged["max_spread_pips"] = float(absolute_cap_pips)

    cost_model = dict(account_rules.get("cost_model") or {})
    if "commission_buffer_pips" not in (merged or {}) and cost_model.get("forex_commission_per_lot_per_side_usd") is not None:
        merged["forex_commission_per_lot_per_side_usd"] = float(cost_model["forex_commission_per_lot_per_side_usd"])

    return merged


def persist_tradeability(con: sqlite3.Connection, verdict: dict[str, Any]) -> None:
    """Persist one V5.1 tradeability verdict."""
    ensure_v5_tradeability_schema(con)
    metrics = verdict.get("metrics") or {}
    pair_state_ref = verdict.get("pair_state_ref") or {}
    context_snapshot_ref = verdict.get("context_snapshot_ref") or {}
    economics_state = str(verdict["economics_state"])
    con.execute(
        """
        INSERT OR REPLACE INTO vanguard_v5_tradeability
            (cycle_ts_utc, profile_id, symbol, asset_class, direction, evaluated_ts_utc,
             model_id, predicted_return, predicted_move_pips, predicted_move_bps,
             spread_pips, spread_bps, cost_pips, cost_bps, after_cost_pips,
             after_cost_bps, economics_state, v5_action_prelim, reasons_json,
             metrics_json, pair_state_ref_json, context_snapshot_ref_json,
             config_version, rank_value, direction_streak, strong_prediction_streak,
             top_rank_streak, same_bucket_streak, flip_count_window,
             live_followthrough_pips, live_followthrough_state, v5_action,
             route_tier, display_label, context_snapshot_json)
        VALUES
            (:cycle_ts_utc, :profile_id, :symbol, :asset_class, :direction, :evaluated_ts_utc,
             :model_id, :predicted_return, :predicted_move_pips, :predicted_move_bps,
             :spread_pips, :spread_bps, :cost_pips, :cost_bps, :after_cost_pips,
             :after_cost_bps, :economics_state, :v5_action_prelim, :reasons_json,
             :metrics_json, :pair_state_ref_json, :context_snapshot_ref_json,
             :config_version, :rank_value, 0, 0, 0, 0, 0, NULL,
             'NOT_EVALUATED', :compat_v5_action, 'UNASSIGNED',
             :compat_display_label, :compat_context_snapshot_json)
        """,
        {
            "cycle_ts_utc": verdict["cycle_ts_utc"],
            "profile_id": verdict["profile_id"],
            "symbol": verdict["symbol"],
            "asset_class": verdict["asset_class"],
            "direction": verdict["direction"],
            "evaluated_ts_utc": verdict["evaluated_ts_utc"],
            "model_id": verdict.get("model_id"),
            "predicted_return": verdict.get("predicted_return"),
            "predicted_move_pips": metrics.get("predicted_move_pips"),
            "predicted_move_bps": metrics.get("predicted_move_bps"),
            "spread_pips": metrics.get("spread_pips"),
            "spread_bps": metrics.get("spread_bps"),
            "cost_pips": metrics.get("cost_pips"),
            "cost_bps": metrics.get("cost_bps"),
            "after_cost_pips": metrics.get("after_cost_pips"),
            "after_cost_bps": metrics.get("after_cost_bps"),
            "economics_state": economics_state,
            "v5_action_prelim": verdict["v5_action_prelim"],
            "reasons_json": json.dumps(verdict.get("reasons") or [], sort_keys=True),
            "metrics_json": json.dumps(metrics, sort_keys=True),
            "pair_state_ref_json": json.dumps(pair_state_ref, sort_keys=True, default=str),
            "context_snapshot_ref_json": json.dumps(context_snapshot_ref, sort_keys=True, default=str),
            "config_version": verdict.get("config_version"),
            "rank_value": metrics.get("rank_value"),
            "compat_v5_action": economics_state,
            "compat_display_label": economics_state,
            "compat_context_snapshot_json": json.dumps(context_snapshot_ref, sort_keys=True, default=str),
        },
    )
    con.commit()


def evaluate_tradeability(
    candidate: dict[str, Any],
    pair_state: dict[str, Any] | None,
    config: dict[str, Any],
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """
    Evaluate V5.1 economics only.

    Candidate is V4B prediction truth. Pair state is DB/context truth. The output
    answers whether the predicted move survives spread/cost feasibility.
    """
    evaluated_at = _ensure_utc(evaluated_at or now_utc())
    asset_class = str(candidate.get("asset_class") or (pair_state or {}).get("asset_class") or "forex").lower()
    cfg = _asset_config(config, asset_class)
    profile_id = str(candidate.get("profile_id") or (pair_state or {}).get("profile_id") or "")
    thresholds = _effective_thresholds(
        profile_id=profile_id,
        asset_class=asset_class,
        thresholds=cfg.get("thresholds") or {},
    )
    buffers = cfg.get("buffers") or {}

    symbol = _normalise_symbol(candidate.get("symbol") or (pair_state or {}).get("symbol"))
    direction = _direction_for(candidate)
    predicted_return = _as_float(candidate.get("predicted_return"), 0.0) or 0.0
    model_id = str(candidate.get("model_id") or "")
    cycle_ts = str(candidate.get("cycle_ts_utc") or (pair_state or {}).get("cycle_ts_utc") or iso_utc(evaluated_at))

    live_mid = _as_float((pair_state or {}).get("live_mid"), None)
    economics = _economics_metrics(
        asset_class=asset_class,
        symbol=symbol,
        predicted_return=predicted_return,
        live_mid=live_mid,
        state=pair_state or {},
        buffers=buffers,
    )

    reasons = _reasons(
        candidate=candidate,
        pair_state=pair_state or {},
        asset_class=asset_class,
        symbol=symbol,
        direction=direction,
        predicted_return=predicted_return,
        live_mid=live_mid,
        spread_pips=economics.get("spread_pips"),
        spread_bps=economics.get("spread_bps"),
        after_cost_pips=economics.get("after_cost_pips"),
        after_cost_bps=economics.get("after_cost_bps"),
        thresholds=thresholds,
    )
    economics_state = _economics_state(reasons, economics, thresholds, asset_class)

    metrics = {
        **economics,
        "live_mid": live_mid,
        "spread_ratio_vs_baseline": _as_float((pair_state or {}).get("spread_ratio_vs_baseline"), None),
        "rank_value": _as_float(candidate.get("rank_value", candidate.get("rank")), None),
        "feature_count": int(candidate.get("feature_count") or 0) if candidate.get("feature_count") is not None else None,
    }
    pair_state_ref = _pair_state_ref(pair_state or {})
    context_snapshot_ref = {
        "pair_state_cycle_ts_utc": pair_state_ref.get("cycle_ts_utc"),
        "pair_state_source": pair_state_ref.get("source"),
        "pair_state_source_status": pair_state_ref.get("source_status"),
        "quote_ts_utc": pair_state_ref.get("quote_ts_utc"),
        "context_ts_utc": pair_state_ref.get("context_ts_utc"),
    }

    return {
        "schema_version": "v5_tradeability_v1",
        "config_version": str(config.get("schema_version") or config.get("version") or "unknown"),
        "cycle_ts_utc": cycle_ts,
        "profile_id": profile_id,
        "symbol": symbol,
        "asset_class": asset_class,
        "direction": direction,
        "evaluated_ts_utc": iso_utc(evaluated_at),
        "model_id": model_id,
        "predicted_return": predicted_return,
        "economics_state": economics_state,
        "v5_action_prelim": economics_state,
        "reasons": reasons,
        "metrics": metrics,
        "pair_state_ref": pair_state_ref,
        "context_snapshot_ref": context_snapshot_ref,
    }


def evaluate_cycle_tradeability(
    con: sqlite3.Connection,
    config: dict[str, Any],
    *,
    cycle_ts_utc: str | None = None,
    profile_ids: list[str] | None = None,
    asset_classes: list[str] | None = None,
    persist: bool = True,
    evaluated_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluate latest predictions against latest asset-state rows.

    This is the DB bridge for V5.1. It does not call MT5 and does not alter
    shortlist tables.
    """
    con.row_factory = sqlite3.Row
    ensure_v5_tradeability_schema(con)
    cycle_ts = cycle_ts_utc or _latest_prediction_cycle(con, None)
    if not cycle_ts:
        return []
    out: list[dict[str, Any]] = []
    requested_assets = [a.lower() for a in (asset_classes or _configured_asset_classes(config))]
    for asset_class in requested_assets:
        predictions = _prediction_rows(con, cycle_ts, asset_class)
        if not predictions:
            continue
        profiles = profile_ids or _state_profiles(con, asset_class)
        for profile_id in profiles:
            for pred in predictions:
                pair_state = _latest_state(con, asset_class, profile_id, pred["symbol"])
                candidate = dict(pred)
                candidate["profile_id"] = profile_id
                verdict = evaluate_tradeability(candidate, pair_state, config, evaluated_at=evaluated_at)
                out.append(verdict)
                if persist:
                    persist_tradeability(con, verdict)
    return out


def _reasons(
    *,
    candidate: dict[str, Any],
    pair_state: dict[str, Any],
    asset_class: str,
    symbol: str,
    direction: str,
    predicted_return: float,
    live_mid: float | None,
    spread_pips: float | None,
    spread_bps: float | None,
    after_cost_pips: float | None,
    after_cost_bps: float | None,
    thresholds: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if not pair_state:
        reasons.append("CONTEXT_MISSING" if asset_class == "crypto" else "PAIR_STATE_MISSING")
    elif str(pair_state.get("source_status") or "MISSING").upper() != "OK":
        reasons.append("QUOTE_MISSING" if asset_class == "crypto" else "PAIR_STATE_NOT_OK")
    if not symbol:
        reasons.append("SYMBOL_MISSING")
    if direction not in {"LONG", "SHORT"}:
        reasons.append("DIRECTION_INVALID")
    if predicted_return == 0:
        reasons.append("PREDICTION_ZERO")
    elif direction == "LONG" and predicted_return < 0:
        reasons.append("PREDICTION_DIRECTION_MISMATCH")
    elif direction == "SHORT" and predicted_return > 0:
        reasons.append("PREDICTION_DIRECTION_MISMATCH")
    if live_mid is None or live_mid <= 0:
        reasons.append("MID_MISSING")
    if asset_class == "crypto":
        if not bool(pair_state.get("trade_allowed", 0)):
            reasons.append("TRADE_NOT_ALLOWED")
        if spread_bps is None:
            reasons.append("SPREAD_MISSING")
        elif spread_bps <= 0:
            reasons.append("SPREAD_INVALID")
        max_spread_bps = _as_float(thresholds.get("max_spread_bps"), None)
        if max_spread_bps is not None and spread_bps is not None and spread_bps > max_spread_bps:
            reasons.append("SPREAD_TOO_WIDE")
    else:
        if spread_pips is None:
            reasons.append("SPREAD_MISSING")
        elif spread_pips <= 0:
            reasons.append("SPREAD_INVALID")
        max_spread = _as_float(thresholds.get("max_spread_pips"), None)
        if max_spread is not None and spread_pips is not None and spread_pips > max_spread:
            reasons.append("SPREAD_TOO_WIDE")
    max_spread_ratio = _as_float(thresholds.get("max_spread_ratio_vs_baseline"), None)
    spread_ratio = _as_float(pair_state.get("spread_ratio_vs_baseline"), None)
    if max_spread_ratio is not None and spread_ratio is not None and spread_ratio > max_spread_ratio:
        reasons.append("SPREAD_RATIO_TOO_WIDE")
    after_cost = after_cost_bps if asset_class == "crypto" else after_cost_pips
    if after_cost is None:
        reasons.append("ECONOMICS_INCOMPLETE")
    elif after_cost < 0:
        reasons.append("AFTER_COST_NEGATIVE")
    return reasons


def _economics_state(
    reasons: list[str],
    metrics: dict[str, Any],
    thresholds: dict[str, Any],
    asset_class: str,
) -> str:
    hard = {
        "PAIR_STATE_MISSING",
        "PAIR_STATE_NOT_OK",
        "CONTEXT_MISSING",
        "QUOTE_MISSING",
        "TRADE_NOT_ALLOWED",
        "SYMBOL_MISSING",
        "DIRECTION_INVALID",
        "PREDICTION_ZERO",
        "PREDICTION_DIRECTION_MISMATCH",
        "MID_MISSING",
        "SPREAD_MISSING",
        "SPREAD_INVALID",
        "SPREAD_TOO_WIDE",
        "SPREAD_RATIO_TOO_WIDE",
        "ECONOMICS_INCOMPLETE",
        "AFTER_COST_NEGATIVE",
    }
    if any(reason in hard for reason in reasons):
        return "FAIL"
    if asset_class == "crypto":
        after_cost = metrics.get("after_cost_bps")
        min_after_cost = _as_float(thresholds.get("min_after_cost_bps"), 0.0) or 0.0
    else:
        after_cost = metrics.get("after_cost_pips")
        min_after_cost = _as_float(thresholds.get("min_after_cost_pips"), 0.0) or 0.0
    if after_cost is None or float(after_cost) < min_after_cost:
        return "WEAK"
    return "PASS"


def _pair_state_ref(pair_state: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "cycle_ts_utc",
        "profile_id",
        "symbol",
        "asset_class",
        "session_bucket",
        "liquidity_bucket",
        "spread_bps",
        "spread_price",
        "spread_ratio_vs_baseline",
        "pair_correlation_bucket",
        "same_currency_open_count",
        "correlated_open_count",
        "quote_ts_utc",
        "context_ts_utc",
        "source",
        "source_status",
        "source_error",
        "config_version",
    ]
    return {key: pair_state.get(key) for key in keys if key in pair_state}


def _economics_metrics(
    *,
    asset_class: str,
    symbol: str,
    predicted_return: float,
    live_mid: float | None,
    state: dict[str, Any],
    buffers: dict[str, Any],
) -> dict[str, Any]:
    if asset_class == "crypto":
        predicted_move_bps = abs(predicted_return) * 10_000.0
        spread_bps = _as_float(state.get("spread_bps"), None)
        cost_bps = (
            spread_bps
            + (_as_float(buffers.get("slippage_buffer_bps"), 0.0) or 0.0)
            + (_as_float(buffers.get("commission_buffer_bps"), 0.0) or 0.0)
            if spread_bps is not None
            else None
        )
        after_cost_bps = predicted_move_bps - cost_bps if cost_bps is not None else None
        return {
            "predicted_move_pips": None,
            "predicted_move_bps": predicted_move_bps,
            "predicted_move_score": predicted_move_bps,
            "spread_pips": None,
            "spread_bps": spread_bps,
            "cost_pips": None,
            "cost_bps": cost_bps,
            "after_cost_pips": None,
            "after_cost_bps": after_cost_bps,
            "after_cost_score": after_cost_bps,
            "score_unit": "bps",
            "pip_size": None,
        }

    pip_size = _pip_size(symbol)
    predicted_move_pips = (
        abs(predicted_return) * live_mid / pip_size
        if live_mid is not None and live_mid > 0 and pip_size > 0
        else None
    )
    spread_pips = _as_float(state.get("spread_pips"), None)
    cost_pips = (
        spread_pips
        + (_as_float(buffers.get("slippage_buffer_pips"), 0.0) or 0.0)
        + (_as_float(buffers.get("commission_buffer_pips"), 0.0) or 0.0)
        if spread_pips is not None
        else None
    )
    after_cost_pips = (
        predicted_move_pips - cost_pips
        if predicted_move_pips is not None and cost_pips is not None
        else None
    )
    return {
        "predicted_move_pips": predicted_move_pips,
        "predicted_move_bps": None,
        "predicted_move_score": predicted_move_pips,
        "spread_pips": spread_pips,
        "spread_bps": None,
        "cost_pips": cost_pips,
        "cost_bps": None,
        "after_cost_pips": after_cost_pips,
        "after_cost_bps": None,
        "after_cost_score": after_cost_pips,
        "score_unit": "pips",
        "pip_size": pip_size,
    }


def _asset_config(config: dict[str, Any], asset_class: str) -> dict[str, Any]:
    defaults = dict(config.get("defaults") or {})
    assets = config.get("asset_classes") or {}
    specific = dict(assets.get(asset_class.lower()) or assets.get("default") or {})
    return _deep_merge(defaults, specific)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _latest_prediction_cycle(con: sqlite3.Connection, asset_class: str | None) -> str | None:
    if asset_class:
        row = con.execute(
            "SELECT MAX(cycle_ts_utc) FROM vanguard_predictions WHERE asset_class = ?",
            (asset_class,),
        ).fetchone()
    else:
        row = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_predictions").fetchone()
    return row[0] if row and row[0] else None


def _prediction_rows(con: sqlite3.Connection, cycle_ts_utc: str, asset_class: str) -> list[dict[str, Any]]:
    columns = _table_columns(con, "vanguard_predictions")
    feature_count_expr = "feature_count" if "feature_count" in columns else "NULL AS feature_count"
    rows = con.execute(
        f"""
        SELECT cycle_ts_utc, symbol, asset_class, predicted_return, direction, model_id, {feature_count_expr}
        FROM vanguard_predictions
        WHERE cycle_ts_utc = ? AND asset_class = ?
        ORDER BY symbol
        """,
        (cycle_ts_utc, asset_class),
    ).fetchall()
    return [dict(row) for row in rows]


def _configured_asset_classes(config: dict[str, Any]) -> list[str]:
    assets = list((config.get("asset_classes") or {}).keys())
    return [a.lower() for a in assets if a.lower() != "default"] or ["forex"]


def _state_profiles(con: sqlite3.Connection, asset_class: str) -> list[str]:
    table = _state_table(asset_class)
    if table is None:
        return []
    try:
        rows = con.execute(f"SELECT DISTINCT profile_id FROM {table} ORDER BY profile_id").fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row[0]) for row in rows]


def _latest_state(con: sqlite3.Connection, asset_class: str, profile_id: str, symbol: str) -> dict[str, Any] | None:
    table = _state_table(asset_class)
    if table is None:
        return None
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE profile_id = ? AND symbol = ?
            ORDER BY cycle_ts_utc DESC
            LIMIT 1
            """,
            (profile_id, _normalise_symbol(symbol)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _state_table(asset_class: str) -> str | None:
    if asset_class == "forex":
        return "vanguard_forex_pair_state"
    if asset_class == "crypto":
        return "vanguard_crypto_symbol_state"
    return None


def _latest_pair_state(con: sqlite3.Connection, profile_id: str, symbol: str) -> dict[str, Any] | None:
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT *
        FROM vanguard_forex_pair_state
        WHERE profile_id = ? AND symbol = ?
        ORDER BY cycle_ts_utc DESC
        LIMIT 1
        """,
        (profile_id, _normalise_symbol(symbol)),
    ).fetchone()
    return dict(row) if row else None


def _direction_for(candidate: dict[str, Any]) -> str:
    explicit = str(candidate.get("direction") or candidate.get("side") or "").upper()
    if explicit in {"LONG", "SHORT"}:
        return explicit
    predicted_return = _as_float(candidate.get("predicted_return"), 0.0) or 0.0
    if predicted_return > 0:
        return "LONG"
    if predicted_return < 0:
        return "SHORT"
    return "FLAT"


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


def _normalise_symbol(value: Any) -> str:
    return str(value or "").replace("/", "").replace(".x", "").replace(".X", "").upper()


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}
