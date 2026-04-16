"""
shortlisting.py — V5.2 selection memory and shortlist mirroring.

V5.2 consumes V5.1 tradeability rows, computes selection memory, assigns route
tiers/display labels/final V5 action, and persists an audit row. It does not
compute pair economics, call Risky, check daemon health, or submit orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from vanguard.helpers.clock import iso_utc, now_utc


V5_SELECTION_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_v5_selection (
    cycle_ts_utc                 TEXT NOT NULL,
    profile_id                   TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    asset_class                  TEXT NOT NULL,
    direction                    TEXT NOT NULL,
    selected                     INTEGER NOT NULL DEFAULT 0,
    selection_state              TEXT NOT NULL,
    selection_rank               INTEGER,
    selection_reason             TEXT,
    route_tier                   TEXT NOT NULL,
    display_label                TEXT NOT NULL,
    v5_action                    TEXT NOT NULL,
    direction_streak             INTEGER NOT NULL DEFAULT 0,
    top_rank_streak              INTEGER NOT NULL DEFAULT 0,
    strong_prediction_streak     INTEGER NOT NULL DEFAULT 0,
    same_bucket_streak           INTEGER NOT NULL DEFAULT 0,
    flip_count_window            INTEGER NOT NULL DEFAULT 0,
    live_followthrough_pips      REAL,
    live_followthrough_bps       REAL,
    live_followthrough_state     TEXT NOT NULL DEFAULT 'UNKNOWN',
    selection_flags_json         TEXT NOT NULL DEFAULT '[]',
    selected_ts_utc              TEXT NOT NULL,
    tradeability_ref_json        TEXT NOT NULL,
    metrics_json                 TEXT NOT NULL DEFAULT '{}',
    config_version               TEXT,
    PRIMARY KEY (cycle_ts_utc, profile_id, symbol, direction)
);

CREATE INDEX IF NOT EXISTS idx_v5_selection_selected
    ON vanguard_v5_selection(cycle_ts_utc, profile_id, selected, selection_rank);
"""


_MIGRATION_COLUMNS: dict[str, str] = {
    "direction_streak": "INTEGER NOT NULL DEFAULT 0",
    "top_rank_streak": "INTEGER NOT NULL DEFAULT 0",
    "strong_prediction_streak": "INTEGER NOT NULL DEFAULT 0",
    "same_bucket_streak": "INTEGER NOT NULL DEFAULT 0",
    "flip_count_window": "INTEGER NOT NULL DEFAULT 0",
    "live_followthrough_pips": "REAL",
    "live_followthrough_bps": "REAL",
    "live_followthrough_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "selection_flags_json": "TEXT NOT NULL DEFAULT '[]'",
    "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
}


SHORTLIST_V2_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_shortlist_v2 (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    direction TEXT NOT NULL,
    predicted_return REAL,
    edge_score REAL,
    rank INTEGER,
    model_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_vg_shortlist_v2_cycle
    ON vanguard_shortlist_v2(cycle_ts_utc, asset_class, direction);
"""


SHORTLIST_LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_shortlist (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_rank INTEGER NOT NULL,
    strategy_score REAL NOT NULL,
    ml_prob REAL NOT NULL,
    edge_score REAL,
    consensus_count INTEGER DEFAULT 0,
    strategies_matched TEXT,
    regime TEXT NOT NULL,
    model_family TEXT,
    model_source TEXT,
    model_readiness TEXT,
    feature_profile TEXT,
    tbm_profile TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
);
CREATE INDEX IF NOT EXISTS idx_vg_sl_cycle
    ON vanguard_shortlist(cycle_ts_utc);
"""


def ensure_v5_selection_schema(con: sqlite3.Connection) -> None:
    for stmt in V5_SELECTION_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    existing = _table_columns(con, "vanguard_v5_selection")
    for column, ddl_type in _MIGRATION_COLUMNS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE vanguard_v5_selection ADD COLUMN {column} {ddl_type}")
    con.commit()


def ensure_shortlist_schema(con: sqlite3.Connection) -> None:
    for block in (SHORTLIST_V2_DDL, SHORTLIST_LEGACY_DDL):
        for stmt in block.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
    con.commit()


def persist_selection(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    ensure_v5_selection_schema(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_v5_selection
            (cycle_ts_utc, profile_id, symbol, asset_class, direction, selected,
             selection_state, selection_rank, selection_reason, route_tier,
             display_label, v5_action, direction_streak, top_rank_streak,
             strong_prediction_streak, same_bucket_streak, flip_count_window,
             live_followthrough_pips, live_followthrough_bps, live_followthrough_state,
             selection_flags_json, selected_ts_utc, tradeability_ref_json,
             metrics_json, config_version)
        VALUES
            (:cycle_ts_utc, :profile_id, :symbol, :asset_class, :direction, :selected,
             :selection_state, :selection_rank, :selection_reason, :route_tier,
             :display_label, :v5_action, :direction_streak, :top_rank_streak,
             :strong_prediction_streak, :same_bucket_streak, :flip_count_window,
             :live_followthrough_pips, :live_followthrough_bps, :live_followthrough_state,
             :selection_flags_json, :selected_ts_utc, :tradeability_ref_json,
             :metrics_json, :config_version)
        """,
        [_serialise_row(row) for row in rows],
    )
    con.commit()


def mirror_selected_shortlist(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Mirror selected V5.2 rows into V6-compatible shortlist tables."""
    ensure_shortlist_schema(con)
    selected = [row for row in rows if int(row.get("selected") or 0) == 1]
    if not rows:
        return 0
    cycle_ts = str(rows[0]["cycle_ts_utc"])
    created_at = rows[0].get("selected_ts_utc") or iso_utc(now_utc())
    con.execute("DELETE FROM vanguard_shortlist_v2 WHERE cycle_ts_utc = ?", (cycle_ts,))
    con.execute("DELETE FROM vanguard_shortlist WHERE cycle_ts_utc = ?", (cycle_ts,))
    if not selected:
        con.commit()
        return 0

    v2_rows = []
    legacy_rows = []
    total = max(len(selected), 1)
    for row in selected:
        ref = row.get("tradeability_ref") or {}
        metrics = row.get("metrics") or {}
        rank = int(row.get("selection_rank") or 999999)
        predicted_return = _as_float(ref.get("predicted_return"), 0.0) or 0.0
        score = _metric_value(metrics, "after_cost") or abs(predicted_return)
        v2_rows.append(
            {
                "cycle_ts_utc": row["cycle_ts_utc"],
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "direction": row["direction"],
                "predicted_return": predicted_return,
                "edge_score": score,
                "rank": rank,
                "model_id": ref.get("model_id"),
                "created_at": created_at,
            }
        )
        legacy_rows.append(
            {
                "cycle_ts_utc": row["cycle_ts_utc"],
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "direction": row["direction"],
                "strategy": "v5_2_selection",
                "strategy_rank": rank,
                "strategy_score": abs(predicted_return),
                "ml_prob": max(0.0, 1.0 - ((rank - 1) / total)),
                "edge_score": score,
                "consensus_count": int(row.get("direction_streak") or 1),
                "strategies_matched": row.get("display_label"),
                "regime": "ACTIVE",
                "model_family": "regressor",
                "model_source": ref.get("model_id"),
                "model_readiness": "v5_2_selected",
                "feature_profile": "",
                "tbm_profile": "",
            }
        )
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_shortlist_v2
        (cycle_ts_utc, symbol, asset_class, direction, predicted_return, edge_score, rank, model_id, created_at)
        VALUES (:cycle_ts_utc, :symbol, :asset_class, :direction, :predicted_return, :edge_score, :rank, :model_id, :created_at)
        """,
        v2_rows,
    )
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_shortlist
        (cycle_ts_utc, symbol, asset_class, direction, strategy, strategy_rank,
         strategy_score, ml_prob, edge_score, consensus_count, strategies_matched,
         regime, model_family, model_source, model_readiness, feature_profile, tbm_profile)
        VALUES (:cycle_ts_utc, :symbol, :asset_class, :direction, :strategy, :strategy_rank,
                :strategy_score, :ml_prob, :edge_score, :consensus_count, :strategies_matched,
                :regime, :model_family, :model_source, :model_readiness, :feature_profile, :tbm_profile)
        """,
        legacy_rows,
    )
    con.commit()
    return len(selected)


def select_shortlist(
    tradeability_rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    history_rows: list[dict[str, Any]] | None = None,
    selected_at: str | None = None,
) -> list[dict[str, Any]]:
    """
    Compute V5.2 memory, route tags, and selected rows from V5.1 output.

    Returns one row for every tradeability input. `selected=1` means eligible
    to mirror into `vanguard_shortlist_v2` and legacy `vanguard_shortlist`.
    """
    selected_at = selected_at or iso_utc(now_utc())
    cfg = config.get("defaults") or config
    rows = [_normalise_tradeability_row(row) for row in tradeability_rows]
    _assign_current_ranks(rows, cfg)
    history = history_rows or []

    classified: list[dict[str, Any]] = []
    for row in rows:
        metrics = row["metrics"]
        memory = _compute_memory(row, history, cfg)
        route_tier, display_label = _classify_row(row, memory, cfg)
        v5_action, state, reason = _action_state_reason(row, route_tier, display_label)
        flags = _selection_flags(row, memory, route_tier, display_label, cfg)
        classified.append(
            {
                "cycle_ts_utc": row["cycle_ts_utc"],
                "profile_id": row["profile_id"],
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "direction": row["direction"],
                "selected": 0,
                "selection_state": state,
                "selection_rank": None,
                "selection_reason": reason,
                "route_tier": route_tier,
                "display_label": display_label,
                "v5_action": v5_action,
                "direction_streak": memory["direction_streak"],
                "top_rank_streak": memory["top_rank_streak"],
                "strong_prediction_streak": memory["strong_prediction_streak"],
                "same_bucket_streak": memory["same_bucket_streak"],
                "flip_count_window": memory["flip_count_window"],
                "live_followthrough_pips": memory["live_followthrough_pips"],
                "live_followthrough_bps": memory["live_followthrough_bps"],
                "live_followthrough_state": memory["live_followthrough_state"],
                "selection_flags": flags,
                "selected_ts_utc": selected_at,
                "tradeability_ref": row,
                "metrics": {**metrics, **memory},
                "config_version": str(config.get("schema_version") or config.get("version") or "unknown"),
            }
        )

    _apply_selection_caps(classified, cfg)
    for row in classified:
        if int(row.get("selected") or 0) != 1:
            row["direction_streak"] = 0
        flag_row = {**(row.get("tradeability_ref") or {}), "selected": row.get("selected"), "selection_state": row.get("selection_state")}
        row["selection_flags"] = _selection_flags(
            flag_row,
            row,
            str(row.get("route_tier") or ""),
            str(row.get("display_label") or ""),
            cfg,
        )
        row["metrics"] = {**(row.get("metrics") or {}), "direction_streak": int(row.get("direction_streak") or 0)}
    return sorted(
        classified,
        key=lambda row: (
            str(row.get("profile_id") or ""),
            0 if int(row.get("selected") or 0) else 1,
            int(row.get("selection_rank") or 999999),
            _sort_key(row, cfg),
        ),
    )


def evaluate_cycle_selection(
    con: sqlite3.Connection,
    config: dict[str, Any],
    *,
    cycle_ts_utc: str | None = None,
    profile_ids: list[str] | None = None,
    persist: bool = True,
    mirror_shortlist: bool = False,
) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    ensure_v5_selection_schema(con)
    cycle_ts = cycle_ts_utc or _latest_tradeability_cycle(con)
    if not cycle_ts:
        return []
    current = _tradeability_rows(con, cycle_ts, profile_ids=profile_ids)
    history = _selection_history(con, current, config)
    rows = select_shortlist(current, config, history_rows=history)
    if persist:
        persist_selection(con, rows)
        if mirror_shortlist:
            mirror_selected_shortlist(con, rows)
    return rows


def _assign_current_ranks(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("profile_id") or ""),
            str(row.get("asset_class") or ""),
            str(row.get("direction") or ""),
        )
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        model_ranked = sorted(
            group,
            key=lambda row: -abs(float(row.get("predicted_return") or (row.get("tradeability_ref") or {}).get("predicted_return") or 0.0)),
        )
        for idx, row in enumerate(model_ranked, start=1):
            metrics = row["metrics"]
            if metrics.get("model_rank") is None and metrics.get("rank_value") is None:
                metrics["model_rank"] = idx
            elif metrics.get("model_rank") is None:
                metrics["model_rank"] = metrics.get("rank_value")
        economics_ranked = sorted(group, key=lambda row: -_metric_value(row.get("metrics") or {}, "after_cost"))
        for idx, row in enumerate(economics_ranked, start=1):
            metrics = row["metrics"]
            metrics["economics_rank"] = idx


def _compute_memory(row: dict[str, Any], history_rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    memory_cfg = cfg.get("memory") or {}
    streak_minutes = int(memory_cfg.get("streak_window_minutes") or memory_cfg.get("window_minutes") or 30)
    flip_minutes = int(memory_cfg.get("flip_window_minutes") or streak_minutes)
    rows = _history_for_window(row, history_rows, streak_minutes)
    flip_rows = _history_for_window(row, history_rows, flip_minutes)
    direction = str(row.get("direction") or "")
    current_bucket = _bucket_candidate(row, cfg)
    current_strong = _is_strong(row, cfg)
    current_top_rank = _is_top_rank(row, cfg)
    current_streak_eligible = _is_streak_eligible_current(row)
    direction_streak = 1 if current_streak_eligible else 0
    strong_prediction_streak = 1 if current_streak_eligible and current_strong else 0
    top_rank_streak = 1 if current_streak_eligible and current_top_rank else 0
    same_bucket_streak = 1 if current_streak_eligible and current_bucket != "NO_TRADE" else 0
    raw_direction_streak = 1 if current_streak_eligible else 0
    flip_count = 0
    prev_direction = direction
    broke_direction = not current_streak_eligible
    broke_strong = not (current_streak_eligible and current_strong)
    broke_rank = not (current_streak_eligible and current_top_rank)
    broke_bucket = not (current_streak_eligible and current_bucket != "NO_TRADE")
    prev_hist_ts = _parse_ts(row.get("cycle_ts_utc"))
    max_gap_seconds = _streak_max_gap_seconds(
        str(row.get("asset_class") or ""),
        memory_cfg,
    )
    min_gap_seconds = _streak_min_gap_seconds(
        str(row.get("asset_class") or ""),
        memory_cfg,
    )

    for hist in flip_rows:
        hist_ts = _parse_ts(hist.get("cycle_ts_utc"))
        if prev_hist_ts is not None and hist_ts is not None:
            delta = (prev_hist_ts - hist_ts).total_seconds()
            if delta < min_gap_seconds:
                continue
        hist_direction = str(hist.get("direction") or "")
        if _is_streak_eligible_history(hist, memory_cfg) and hist_direction in {"LONG", "SHORT"} and prev_direction in {"LONG", "SHORT"} and hist_direction != prev_direction:
            flip_count += 1
        if _is_streak_eligible_history(hist, memory_cfg) and hist_direction in {"LONG", "SHORT"}:
            prev_direction = hist_direction

    for hist in rows:
        hist_direction = str(hist.get("direction") or "")
        hist_eligible = _is_streak_eligible_history(hist, memory_cfg)
        if hist_eligible and hist_direction == direction:
            raw_direction_streak += 1

        gap_ok = True
        hist_ts = _parse_ts(hist.get("cycle_ts_utc"))
        if prev_hist_ts is None or hist_ts is None:
            gap_ok = False
        else:
            delta = (prev_hist_ts - hist_ts).total_seconds()
            if delta < min_gap_seconds:
                continue
            if delta > max_gap_seconds:
                gap_ok = False

        if not broke_direction and hist_eligible and hist_direction == direction and gap_ok:
            direction_streak += 1
        else:
            broke_direction = True
        if hist_ts is not None:
            prev_hist_ts = hist_ts

        if not broke_strong and hist_eligible and _is_strong(hist, cfg):
            strong_prediction_streak += 1
        else:
            broke_strong = True

        if not broke_rank and hist_eligible and _is_top_rank(hist, cfg):
            top_rank_streak += 1
        else:
            broke_rank = True

        if not broke_bucket and hist_eligible and _bucket_candidate(hist, cfg) == current_bucket:
            same_bucket_streak += 1
        else:
            broke_bucket = True

    follow_pips, follow_bps, follow_state = _live_followthrough(row, rows, cfg)
    return {
        "direction_streak": direction_streak,
        "raw_direction_streak": raw_direction_streak,
        "top_rank_streak": top_rank_streak,
        "strong_prediction_streak": strong_prediction_streak,
        "same_bucket_streak": same_bucket_streak,
        "flip_count_window": flip_count,
        "live_followthrough_pips": follow_pips,
        "live_followthrough_bps": follow_bps,
        "live_followthrough_state": follow_state,
    }


def _streak_max_gap_seconds(asset_class: str, memory_cfg: dict[str, Any]) -> int:
    by_asset = memory_cfg.get("max_gap_seconds_by_asset_class") or {}
    asset_key = str(asset_class or "").lower()
    configured = by_asset.get(asset_key)
    if configured is None:
        defaults = {
            "crypto": 180,
            "forex": 600,
            "equity": 1800,
            "index": 1800,
        }
        configured = defaults.get(asset_key, 600)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return 600


def _streak_min_gap_seconds(asset_class: str, memory_cfg: dict[str, Any]) -> int:
    by_asset = memory_cfg.get("min_gap_seconds_by_asset_class") or {}
    asset_key = str(asset_class or "").lower()
    configured = by_asset.get(asset_key)
    if configured is None:
        defaults = {
            "crypto": 240,
            "forex": 240,
            "equity": 900,
            "index": 900,
        }
        configured = defaults.get(asset_key, 240)
    try:
        return max(0, int(configured))
    except (TypeError, ValueError):
        return 240


def _is_directional_signal(direction: str) -> bool:
    return str(direction or "").upper() in {"LONG", "SHORT"}


def _is_streak_eligible_current(row: dict[str, Any]) -> bool:
    direction = str(row.get("direction") or "")
    economics = str(row.get("economics_state") or "").upper()
    return _is_directional_signal(direction) and economics == "PASS"


def _is_streak_eligible_history(row: dict[str, Any], memory_cfg: dict[str, Any]) -> bool:
    direction = str(row.get("direction") or "")
    if not _is_directional_signal(direction):
        return False
    if "selection_state" in row or "selected" in row:
        allowed_states = {
            str(state).upper()
            for state in (memory_cfg.get("streak_eligible_selection_states") or ["SELECTED"])
        }
        selection_state = str(row.get("selection_state") or "").upper()
        if selection_state:
            return selection_state in allowed_states
        return int(row.get("selected") or 0) == 1
    return str(row.get("economics_state") or "").upper() == "PASS"


def _classify_row(row: dict[str, Any], memory: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    if str(row.get("economics_state") or "") == "FAIL":
        return "NONE", "NO_TRADE"
    if str(row.get("economics_state") or "") == "WEAK":
        return "NONE", "WATCHLIST"
    tiers = cfg.get("route_tiers") or {}
    for tier, spec in sorted(tiers.items(), key=lambda item: int(item[1].get("priority", 100))):
        if _tier_matches(row, memory, spec, cfg):
            return tier, tier
    return "NONE", "WATCHLIST"


def _tier_matches(row: dict[str, Any], memory: dict[str, Any], spec: dict[str, Any], cfg: dict[str, Any]) -> bool:
    metrics = row.get("metrics") or {}
    selection_behavior = cfg.get("selection_behavior") or {}
    if _metric_value(metrics, "predicted_move") < _tier_threshold(spec, metrics, "min_predicted_move"):
        return False
    if _metric_value(metrics, "after_cost") < _tier_threshold(spec, metrics, "min_after_cost"):
        return False
    max_rank = spec.get("max_rank")
    if max_rank is not None and int(metrics.get("model_rank") or metrics.get("rank_value") or 999999) > int(max_rank):
        return False
    # Controlled by vanguard_v5_selection.json: selection_behavior.use_direction_streak_in_tier_matching
    if bool(selection_behavior.get("use_direction_streak_in_tier_matching", True)) and int(memory.get("direction_streak") or 0) < int(spec.get("min_direction_streak") or 1):
        return False
    if int(memory.get("strong_prediction_streak") or 0) < int(spec.get("min_strong_prediction_streak") or 0):
        return False
    if int(memory.get("top_rank_streak") or 0) < int(spec.get("min_top_rank_streak") or 0):
        return False
    if int(memory.get("flip_count_window") or 0) > int(spec.get("max_flip_count_window") or 999999):
        return False
    allowed_follow = set(spec.get("allowed_followthrough_states") or ["ALIGNED", "FLAT", "UNKNOWN"])
    if str(memory.get("live_followthrough_state") or "UNKNOWN") not in allowed_follow:
        return False
    return True


def _action_state_reason(row: dict[str, Any], route_tier: str, display_label: str) -> tuple[str, str, str]:
    economics = str(row.get("economics_state") or "")
    if economics == "FAIL":
        return "REJECT", "REJECTED", "ECONOMICS_FAIL"
    if route_tier != "NONE":
        return "ROUTE", "HELD", "ROUTE_CANDIDATE"
    if display_label == "WATCHLIST":
        return "HOLD", "WATCHLIST", "WATCHLIST"
    return "REJECT", "REJECTED", "NO_ROUTE_TIER"


def _selection_flags(
    row: dict[str, Any],
    memory: dict[str, Any],
    route_tier: str,
    display_label: str,
    cfg: dict[str, Any],
) -> list[str]:
    flags = [f"ECONOMICS_{str(row.get('economics_state') or 'UNKNOWN')}"]
    direction_streak = int(memory.get("direction_streak") or 0)
    flip_count = int(memory.get("flip_count_window") or 0)

    if int(row.get("selected") or 0) == 1 and direction_streak >= 2:
        flags.append(f"STREAK_{direction_streak}")
    elif int(row.get("selected") or 0) == 1:
        flags.append("NEW_ENTRY")
    if flip_count >= int((cfg.get("thresholds") or {}).get("flip_risk_min_count") or 2):
        flags.append("FLIP_RISK")
    return list(dict.fromkeys(flags))


def _apply_selection_caps(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    candidates = [row for row in rows if row.get("v5_action") == "ROUTE"]
    candidates.sort(key=lambda row: _sort_key(row, cfg))
    max_per_profile = int(cfg.get("max_selected_per_profile") or 999999)
    max_per_profile_direction = int(cfg.get("max_selected_per_profile_direction") or 999999)
    max_per_asset_class = int(cfg.get("max_selected_per_asset_class") or 999999)
    counts_profile: dict[str, int] = {}
    counts_profile_direction: dict[tuple[str, str], int] = {}
    counts_asset: dict[tuple[str, str], int] = {}
    rank_by_profile: dict[str, int] = {}
    for row in candidates:
        profile = str(row.get("profile_id") or "")
        direction = str(row.get("direction") or "")
        asset = str(row.get("asset_class") or "")
        if counts_profile.get(profile, 0) >= max_per_profile:
            row["selection_state"] = "HOLD"
            row["selection_reason"] = "PROFILE_SELECTION_CAP"
            continue
        if counts_profile_direction.get((profile, direction), 0) >= max_per_profile_direction:
            row["selection_state"] = "HOLD"
            row["selection_reason"] = "PROFILE_DIRECTION_SELECTION_CAP"
            continue
        if counts_asset.get((profile, asset), 0) >= max_per_asset_class:
            row["selection_state"] = "HOLD"
            row["selection_reason"] = "ASSET_CLASS_SELECTION_CAP"
            continue
        counts_profile[profile] = counts_profile.get(profile, 0) + 1
        counts_profile_direction[(profile, direction)] = counts_profile_direction.get((profile, direction), 0) + 1
        counts_asset[(profile, asset)] = counts_asset.get((profile, asset), 0) + 1
        rank_by_profile[profile] = rank_by_profile.get(profile, 0) + 1
        row["selected"] = 1
        row["selection_state"] = "SELECTED"
        row["selection_rank"] = rank_by_profile[profile]
        row["selection_reason"] = "ROUTE_TIER_SELECTED"


def _sort_key(row: dict[str, Any], cfg: dict[str, Any]) -> tuple:
    priority = (cfg.get("route_tier_priority") or {}).get(str(row.get("route_tier") or ""), 999)
    metrics = row.get("metrics") or {}
    selection_behavior = cfg.get("selection_behavior") or {}
    # Controlled by vanguard_v5_selection.json: selection_behavior.use_direction_streak_in_sorting
    direction_streak_key = -int(row.get("direction_streak") or 0) if bool(selection_behavior.get("use_direction_streak_in_sorting", True)) else 0
    return (
        str(row.get("profile_id") or ""),
        int(metrics.get("model_rank") or metrics.get("rank_value") or 999999),
        -_metric_value(metrics, "after_cost"),
        direction_streak_key,
        int(priority),
        str(row.get("symbol") or ""),
    )


def _normalise_tradeability_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["symbol"] = str(out.get("symbol") or "").upper()
    out["direction"] = str(out.get("direction") or "").upper()
    out["asset_class"] = str(out.get("asset_class") or "forex").lower()
    metrics = out.get("metrics")
    if metrics is None:
        metrics = _json_dict(out.get("metrics_json"))
    out["metrics"] = metrics
    return out


def _history_for(row: dict[str, Any], history_rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    memory_cfg = cfg.get("memory") or {}
    window_minutes = int(memory_cfg.get("streak_window_minutes") or memory_cfg.get("window_minutes") or 30)
    return _history_for_window(row, history_rows, window_minutes)


def _history_for_window(row: dict[str, Any], history_rows: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    current_ts = _parse_ts(row.get("cycle_ts_utc")) or now_utc()
    cutoff = current_ts - timedelta(minutes=minutes)
    out = []
    for hist in history_rows:
        if str(hist.get("profile_id") or "") != str(row.get("profile_id") or ""):
            continue
        if str(hist.get("symbol") or "").upper() != str(row.get("symbol") or "").upper():
            continue
        hist_ts = _parse_ts(hist.get("cycle_ts_utc"))
        if hist_ts is None or hist_ts >= current_ts or hist_ts < cutoff:
            continue
        out.append(_normalise_tradeability_row(hist))
    out.sort(key=lambda item: str(item.get("cycle_ts_utc") or ""), reverse=True)
    return out


def _bucket_candidate(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    economics = str(row.get("economics_state") or "")
    if economics == "FAIL":
        return "NO_TRADE"
    if economics == "WEAK":
        return "WATCHLIST"
    return "SELECTED_CANDIDATE"


def _is_strong(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    metrics = row.get("metrics") or {}
    thresholds = cfg.get("thresholds") or {}
    return (
        _metric_value(metrics, "predicted_move") >= _threshold_for_unit(thresholds, metrics, "strong_prediction_min")
        and _metric_value(metrics, "after_cost") >= _threshold_for_unit(thresholds, metrics, "strong_after_cost_min")
    )


def _is_top_rank(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    max_rank = int((cfg.get("thresholds") or {}).get("top_rank_max") or 999999)
    metrics = row.get("metrics") or {}
    return int(metrics.get("model_rank") or metrics.get("rank_value") or 999999) <= max_rank


def _live_followthrough(row: dict[str, Any], history: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[float | None, float | None, str]:
    metrics = row.get("metrics") or {}
    current_mid = _as_float(metrics.get("live_mid"), None)
    asset_class = str(row.get("asset_class") or "").lower()
    pip_size = _as_float(metrics.get("pip_size"), None)
    if current_mid is None or current_mid <= 0:
        return None, None, "UNKNOWN"
    direction = str(row.get("direction") or "")
    anchor = next((hist for hist in history if str(hist.get("direction") or "") == direction), None)
    if anchor is None:
        return None, None, "UNKNOWN"
    anchor_mid = _as_float((anchor.get("metrics") or {}).get("live_mid"), None)
    if anchor_mid is None or anchor_mid <= 0:
        return None, None, "UNKNOWN"
    raw_move = current_mid - anchor_mid
    pips = raw_move / pip_size if pip_size is not None and pip_size > 0 else None
    bps = (raw_move / anchor_mid) * 10_000.0 if anchor_mid > 0 else None
    if direction == "SHORT":
        if pips is not None:
            pips *= -1.0
        if bps is not None:
            bps *= -1.0
    thresholds = cfg.get("thresholds") or {}
    if asset_class == "crypto":
        score = bps
        flat_band = float(thresholds.get("followthrough_flat_band_bps") or 1.0)
    else:
        score = pips
        flat_band = float(thresholds.get("followthrough_flat_band_pips") or 0.2)
    if score is None:
        return pips, bps, "UNKNOWN"
    if score > flat_band:
        return pips, bps, "ALIGNED"
    if score < -flat_band:
        return pips, bps, "AGAINST"
    return pips, bps, "FLAT"


def _metric_value(metrics: dict[str, Any], name: str) -> float:
    score_key = f"{name}_score"
    if metrics.get(score_key) is not None:
        return float(metrics.get(score_key) or 0.0)
    unit = str(metrics.get("score_unit") or "pips")
    if unit == "bps":
        return float(metrics.get(f"{name}_bps") or 0.0)
    return float(metrics.get(f"{name}_pips") or 0.0)


def _threshold_for_unit(thresholds: dict[str, Any], metrics: dict[str, Any], base_name: str) -> float:
    unit = str(metrics.get("score_unit") or "pips")
    if unit == "bps":
        return float(thresholds.get(f"{base_name}_bps") or thresholds.get(base_name) or 0.0)
    return float(thresholds.get(f"{base_name}_pips") or thresholds.get(base_name) or 0.0)


def _tier_threshold(spec: dict[str, Any], metrics: dict[str, Any], base_name: str) -> float:
    unit = str(metrics.get("score_unit") or "pips")
    if unit == "bps":
        return float(spec.get(f"{base_name}_bps") or spec.get(base_name) or 0.0)
    return float(spec.get(f"{base_name}_pips") or spec.get(base_name) or 0.0)


def _latest_tradeability_cycle(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        """
        SELECT MAX(cycle_ts_utc)
        FROM vanguard_v5_tradeability
        WHERE economics_state IS NOT NULL AND economics_state != ''
        """
    ).fetchone()
    return row[0] if row and row[0] else None


def _tradeability_rows(
    con: sqlite3.Connection,
    cycle_ts_utc: str,
    *,
    profile_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [cycle_ts_utc]
    where = "WHERE cycle_ts_utc = ?"
    if profile_ids:
        placeholders = ",".join("?" for _ in profile_ids)
        where += f" AND profile_id IN ({placeholders})"
        params.extend(profile_ids)
    rows = con.execute(
        f"""
        SELECT *
        FROM vanguard_v5_tradeability
        {where}
        ORDER BY profile_id, symbol
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _selection_history(con: sqlite3.Connection, current_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not current_rows:
        return []
    cfg = config.get("defaults") or config
    memory_cfg = cfg.get("memory") or {}
    window_minutes = max(
        int(memory_cfg.get("streak_window_minutes") or memory_cfg.get("window_minutes") or 30),
        int(memory_cfg.get("flip_window_minutes") or memory_cfg.get("window_minutes") or 30),
    )
    latest_cycle = max(str(row.get("cycle_ts_utc") or "") for row in current_rows)
    cutoff = iso_utc((_parse_ts(latest_cycle) or now_utc()) - timedelta(minutes=window_minutes))
    profile_ids = sorted({str(row.get("profile_id") or "") for row in current_rows})
    symbols = sorted({str(row.get("symbol") or "") for row in current_rows})
    if not profile_ids or not symbols:
        return []
    profile_placeholders = ",".join("?" for _ in profile_ids)
    symbol_placeholders = ",".join("?" for _ in symbols)
    params = [latest_cycle, cutoff, *profile_ids, *symbols]
    query = f"""
        SELECT *
        FROM vanguard_v5_selection
        WHERE cycle_ts_utc < ?
          AND cycle_ts_utc >= ?
          AND profile_id IN ({profile_placeholders})
          AND symbol IN ({symbol_placeholders})
        ORDER BY cycle_ts_utc DESC
    """
    try:
        rows = con.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        rows = con.execute(
            f"""
            SELECT *
            FROM vanguard_v5_tradeability
            WHERE cycle_ts_utc < ?
              AND cycle_ts_utc >= ?
              AND profile_id IN ({profile_placeholders})
              AND symbol IN ({symbol_placeholders})
            ORDER BY cycle_ts_utc DESC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def _serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "tradeability_ref_json": json.dumps(row.get("tradeability_ref") or {}, sort_keys=True, default=str),
        "selection_flags_json": json.dumps(row.get("selection_flags") or [], sort_keys=True),
        "metrics_json": json.dumps(row.get("metrics") or {}, sort_keys=True, default=str),
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


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


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}
