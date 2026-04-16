"""
model_v2_outcomes.py — Model V2 LGBM result tracking.

Dedicated audit/outcome table for the new V4B → V5.1 → V5.2 forex path.
This intentionally does not write to the legacy execution_log forward tracker.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from vanguard.helpers.clock import iso_utc, now_utc


MODEL_V2_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_model_v2_lgbm_results (
    cycle_ts_utc               TEXT NOT NULL,
    profile_id                 TEXT NOT NULL,
    symbol                     TEXT NOT NULL,
    asset_class                TEXT NOT NULL,
    direction                  TEXT NOT NULL,
    model_id                   TEXT,
    predicted_return           REAL,
    model_rank                 INTEGER,
    feature_count              INTEGER,

    live_mid                   REAL,
    spread_pips                REAL,
    spread_ratio_vs_baseline   REAL,
    quote_ts_utc               TEXT,
    context_ts_utc             TEXT,
    source                     TEXT,

    predicted_move_pips        REAL,
    cost_pips                  REAL,
    after_cost_pips            REAL,
    economics_state            TEXT,
    reasons_json               TEXT NOT NULL DEFAULT '[]',

    selected                   INTEGER NOT NULL DEFAULT 0,
    selection_rank             INTEGER,
    selection_state            TEXT,
    route_tier                 TEXT,
    display_label              TEXT,
    direction_streak           INTEGER NOT NULL DEFAULT 0,
    top_rank_streak            INTEGER NOT NULL DEFAULT 0,
    strong_prediction_streak   INTEGER NOT NULL DEFAULT 0,
    same_bucket_streak         INTEGER NOT NULL DEFAULT 0,
    flip_count_window          INTEGER NOT NULL DEFAULT 0,
    live_followthrough_pips    REAL,
    live_followthrough_state   TEXT,
    selection_flags_json       TEXT NOT NULL DEFAULT '[]',

    entry_mid                  REAL,
    target_ts_utc              TEXT,
    future_mid                 REAL,
    future_pips                REAL,
    direction_hit              INTEGER,
    after_cost_outcome_pips    REAL,
    resolved_ts_utc            TEXT,
    outcome_state              TEXT NOT NULL DEFAULT 'UNRESOLVED',

    tradeability_ref_json      TEXT NOT NULL DEFAULT '{}',
    selection_ref_json         TEXT NOT NULL DEFAULT '{}',
    config_version             TEXT,
    created_at_utc             TEXT NOT NULL,
    updated_at_utc             TEXT NOT NULL,

    PRIMARY KEY (cycle_ts_utc, profile_id, symbol, direction)
);

CREATE INDEX IF NOT EXISTS idx_model_v2_results_outcome
    ON vanguard_model_v2_lgbm_results(outcome_state, target_ts_utc);

CREATE INDEX IF NOT EXISTS idx_model_v2_results_profile_cycle
    ON vanguard_model_v2_lgbm_results(profile_id, cycle_ts_utc, selected, selection_rank);
"""


def ensure_model_v2_results_schema(con: sqlite3.Connection) -> None:
    for stmt in MODEL_V2_RESULTS_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def persist_model_v2_results(
    con: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    horizon_minutes: int = 30,
    created_at: datetime | None = None,
) -> int:
    """Persist one Model V2 result row per V5.2 selection/audit row."""
    if not rows:
        return 0
    ensure_model_v2_results_schema(con)
    now = created_at or now_utc()
    payload = [_result_payload(row, horizon_minutes=horizon_minutes, now=now) for row in rows]
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_model_v2_lgbm_results
            (cycle_ts_utc, profile_id, symbol, asset_class, direction, model_id,
             predicted_return, model_rank, feature_count, live_mid, spread_pips,
             spread_ratio_vs_baseline, quote_ts_utc, context_ts_utc, source,
             predicted_move_pips, cost_pips, after_cost_pips, economics_state,
             reasons_json, selected, selection_rank, selection_state, route_tier,
             display_label, direction_streak, top_rank_streak, strong_prediction_streak,
             same_bucket_streak, flip_count_window, live_followthrough_pips,
             live_followthrough_state, selection_flags_json, entry_mid, target_ts_utc,
             future_mid, future_pips, direction_hit, after_cost_outcome_pips,
             resolved_ts_utc, outcome_state, tradeability_ref_json,
             selection_ref_json, config_version, created_at_utc, updated_at_utc)
        VALUES
            (:cycle_ts_utc, :profile_id, :symbol, :asset_class, :direction, :model_id,
             :predicted_return, :model_rank, :feature_count, :live_mid, :spread_pips,
             :spread_ratio_vs_baseline, :quote_ts_utc, :context_ts_utc, :source,
             :predicted_move_pips, :cost_pips, :after_cost_pips, :economics_state,
             :reasons_json, :selected, :selection_rank, :selection_state, :route_tier,
             :display_label, :direction_streak, :top_rank_streak, :strong_prediction_streak,
             :same_bucket_streak, :flip_count_window, :live_followthrough_pips,
             :live_followthrough_state, :selection_flags_json, :entry_mid, :target_ts_utc,
             :future_mid, :future_pips, :direction_hit, :after_cost_outcome_pips,
             :resolved_ts_utc, :outcome_state, :tradeability_ref_json,
             :selection_ref_json, :config_version, :created_at_utc, :updated_at_utc)
        """,
        payload,
    )
    con.commit()
    return len(payload)


def _result_payload(row: dict[str, Any], *, horizon_minutes: int, now: datetime) -> dict[str, Any]:
    tradeability = row.get("tradeability_ref") or {}
    metrics = row.get("metrics") or {}
    pair_state = tradeability.get("pair_state_ref") or {}
    context = tradeability.get("context_snapshot_ref") or {}
    reasons = tradeability.get("reasons") or []
    cycle_ts = str(row.get("cycle_ts_utc") or tradeability.get("cycle_ts_utc") or "")
    target_ts = _target_ts(cycle_ts, horizon_minutes)
    live_mid = _as_float(metrics.get("live_mid"), None)
    return {
        "cycle_ts_utc": cycle_ts,
        "profile_id": row.get("profile_id"),
        "symbol": row.get("symbol"),
        "asset_class": row.get("asset_class"),
        "direction": row.get("direction"),
        "model_id": tradeability.get("model_id"),
        "predicted_return": _as_float(tradeability.get("predicted_return"), None),
        "model_rank": _as_int(metrics.get("model_rank") or metrics.get("rank_value"), None),
        "feature_count": _as_int(metrics.get("feature_count"), None),
        "live_mid": live_mid,
        "spread_pips": _as_float(metrics.get("spread_pips"), None),
        "spread_ratio_vs_baseline": _as_float(metrics.get("spread_ratio_vs_baseline"), None),
        "quote_ts_utc": pair_state.get("quote_ts_utc") or context.get("quote_ts_utc"),
        "context_ts_utc": pair_state.get("context_ts_utc") or context.get("context_ts_utc"),
        "source": pair_state.get("source") or context.get("pair_state_source"),
        "predicted_move_pips": _as_float(metrics.get("predicted_move_pips"), None),
        "cost_pips": _as_float(metrics.get("cost_pips"), None),
        "after_cost_pips": _as_float(metrics.get("after_cost_pips"), None),
        "economics_state": tradeability.get("economics_state"),
        "reasons_json": json.dumps(reasons, sort_keys=True, default=str),
        "selected": int(row.get("selected") or 0),
        "selection_rank": _as_int(row.get("selection_rank"), None),
        "selection_state": row.get("selection_state"),
        "route_tier": row.get("route_tier"),
        "display_label": row.get("display_label"),
        "direction_streak": int(row.get("direction_streak") or 0),
        "top_rank_streak": int(row.get("top_rank_streak") or 0),
        "strong_prediction_streak": int(row.get("strong_prediction_streak") or 0),
        "same_bucket_streak": int(row.get("same_bucket_streak") or 0),
        "flip_count_window": int(row.get("flip_count_window") or 0),
        "live_followthrough_pips": _as_float(row.get("live_followthrough_pips"), None),
        "live_followthrough_state": row.get("live_followthrough_state"),
        "selection_flags_json": json.dumps(row.get("selection_flags") or [], sort_keys=True, default=str),
        "entry_mid": live_mid,
        "target_ts_utc": target_ts,
        "future_mid": None,
        "future_pips": None,
        "direction_hit": None,
        "after_cost_outcome_pips": None,
        "resolved_ts_utc": None,
        "outcome_state": "UNRESOLVED",
        "tradeability_ref_json": json.dumps(tradeability, sort_keys=True, default=str),
        "selection_ref_json": json.dumps(_selection_ref(row), sort_keys=True, default=str),
        "config_version": row.get("config_version"),
        "created_at_utc": iso_utc(now),
        "updated_at_utc": iso_utc(now),
    }


def _selection_ref(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "cycle_ts_utc",
        "profile_id",
        "symbol",
        "asset_class",
        "direction",
        "selected",
        "selection_state",
        "selection_rank",
        "selection_reason",
        "route_tier",
        "display_label",
        "v5_action",
        "direction_streak",
        "top_rank_streak",
        "strong_prediction_streak",
        "same_bucket_streak",
        "flip_count_window",
        "live_followthrough_pips",
        "live_followthrough_state",
        "selection_flags",
    ]
    return {key: row.get(key) for key in keys}


def _target_ts(cycle_ts: str, horizon_minutes: int) -> str | None:
    parsed = _parse_ts(cycle_ts)
    if parsed is None:
        return None
    return iso_utc(parsed + timedelta(minutes=horizon_minutes))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int | None = 0) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
