from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_DEFAULT_MANIFEST_PATH = Path("/Users/sjani008/SS/Vanguard/config/ftmo_live_stack_services.json")

_CYCLE_FUNNEL_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_diagnostics_cycle_funnel (
    cycle_ts_utc               TEXT NOT NULL,
    profile_id                 TEXT NOT NULL,
    asset_class                TEXT NOT NULL,
    decision_stream_id         TEXT,
    model_id                   TEXT,
    execution_policy_id        TEXT,
    execution_mode             TEXT,
    predictions_count          INTEGER NOT NULL DEFAULT 0,
    tradeability_rows          INTEGER NOT NULL DEFAULT 0,
    tradeability_pass_rows     INTEGER NOT NULL DEFAULT 0,
    economics_pass_rows        INTEGER NOT NULL DEFAULT 0,
    economics_fail_rows        INTEGER NOT NULL DEFAULT 0,
    state_top_rank_rows        INTEGER NOT NULL DEFAULT 0,
    state_direction_rows       INTEGER NOT NULL DEFAULT 0,
    state_strong_rows          INTEGER NOT NULL DEFAULT 0,
    live_policy_filtered_rows  INTEGER NOT NULL DEFAULT 0,
    route_candidate_rows       INTEGER NOT NULL DEFAULT 0,
    selected_rows              INTEGER NOT NULL DEFAULT 0,
    portfolio_approved_rows    INTEGER NOT NULL DEFAULT 0,
    portfolio_rejected_rows    INTEGER NOT NULL DEFAULT 0,
    decision_rows              INTEGER NOT NULL DEFAULT 0,
    executed_rows              INTEGER NOT NULL DEFAULT 0,
    skipped_policy_rows        INTEGER NOT NULL DEFAULT 0,
    skipped_meta_gate_rows     INTEGER NOT NULL DEFAULT 0,
    open_positions_count       INTEGER NOT NULL DEFAULT 0,
    open_positions_upl         REAL NOT NULL DEFAULT 0.0,
    live_policy_failed_checks_json TEXT NOT NULL DEFAULT '{}',
    portfolio_reasons_json     TEXT NOT NULL DEFAULT '{}',
    decision_counts_json       TEXT NOT NULL DEFAULT '{}',
    created_at_utc             TEXT NOT NULL,
    updated_at_utc             TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_vg_diag_funnel_profile_cycle
    ON vanguard_diagnostics_cycle_funnel(profile_id, cycle_ts_utc DESC);
"""

_ACCOUNT_TRUTH_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_diagnostics_account_truth (
    observed_ts_utc            TEXT NOT NULL,
    profile_id                 TEXT NOT NULL,
    account_number             TEXT,
    received_ts_utc            TEXT,
    source                     TEXT,
    source_status              TEXT,
    source_error               TEXT,
    cached_fallback            INTEGER NOT NULL DEFAULT 0,
    balance                    REAL,
    equity_snapshot            REAL,
    live_upl                   REAL NOT NULL DEFAULT 0.0,
    equity_estimate            REAL,
    open_positions_count       INTEGER NOT NULL DEFAULT 0,
    truth_state                TEXT NOT NULL,
    details_json               TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (observed_ts_utc, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_vg_diag_account_truth_profile_ts
    ON vanguard_diagnostics_account_truth(profile_id, observed_ts_utc DESC);
"""

_SERVICE_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_diagnostics_service_verdicts (
    observed_ts_utc            TEXT NOT NULL,
    service_name               TEXT NOT NULL,
    required_for_ready         INTEGER NOT NULL DEFAULT 0,
    process_alive              INTEGER NOT NULL DEFAULT 0,
    pid                        INTEGER,
    freshness_state            TEXT NOT NULL,
    freshness_seconds          INTEGER NOT NULL DEFAULT 0,
    last_success_ts_utc        TEXT,
    last_error_ts_utc          TEXT,
    log_age_seconds            INTEGER,
    expected_write_surface     TEXT,
    details_json               TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (observed_ts_utc, service_name)
);

CREATE INDEX IF NOT EXISTS idx_vg_diag_service_verdicts_name_ts
    ON vanguard_diagnostics_service_verdicts(service_name, observed_ts_utc DESC);
"""

_ANOMALY_LATEST_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_diagnostics_latest (
    anomaly_key                TEXT PRIMARY KEY,
    family                     TEXT NOT NULL,
    scope_type                 TEXT NOT NULL,
    scope_id                   TEXT NOT NULL,
    severity                   TEXT NOT NULL,
    status                     TEXT NOT NULL,
    active_flag                INTEGER NOT NULL DEFAULT 1,
    first_seen_ts_utc          TEXT NOT NULL,
    last_seen_ts_utc           TEXT NOT NULL,
    last_status_change_ts_utc  TEXT NOT NULL,
    cycle_ts_utc               TEXT,
    summary                    TEXT NOT NULL,
    recommended_action         TEXT,
    details_json               TEXT NOT NULL DEFAULT '{}'
);
"""

_ANOMALY_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_diagnostics_events (
    event_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_ts_utc            TEXT NOT NULL,
    cycle_ts_utc               TEXT,
    anomaly_key                TEXT NOT NULL,
    family                     TEXT NOT NULL,
    scope_type                 TEXT NOT NULL,
    scope_id                   TEXT NOT NULL,
    severity                   TEXT NOT NULL,
    status                     TEXT NOT NULL,
    summary                    TEXT NOT NULL,
    recommended_action         TEXT,
    details_json               TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_vg_diag_events_observed
    ON vanguard_diagnostics_events(observed_ts_utc DESC);

CREATE INDEX IF NOT EXISTS idx_vg_diag_events_key
    ON vanguard_diagnostics_events(anomaly_key, observed_ts_utc DESC);
"""

_STAGE_TABLES = [
    "vanguard_predictions_history",
    "vanguard_v5_tradeability",
    "vanguard_v5_selection",
    "vanguard_tradeable_portfolio",
    "vanguard_signal_decision_log",
]

_SERVICE_FRESHNESS_DEFAULTS = {
    "datasette": 600,
    "lifecycle": 600,
    "v1_precompute": 900,
    "live_ic_tracker": 86400,
    "model_truth_snapshot": 900,
    "metaapi_live_context": 300,
    "metaapi_candle_cache": 300,
    "orchestrator": 600,
    "monitor": 600,
}

_CRITICAL_SERVICE_NAMES = {
    "orchestrator",
    "lifecycle",
    "v1_precompute",
    "metaapi_live_context",
    "metaapi_candle_cache",
    "monitor",
}

_CONTRACT_STAGE_OUTPUTS = {
    "selection": "vanguard_v5_selection",
    "risk_filters": "vanguard_tradeable_portfolio",
}

_SELECTION_HANDOFF_REQUIRED_FIELDS = (
    "decision_stream_id",
    "binding_id",
    "risk_policy_id",
    "execution_policy_id",
    "lifecycle_policy_id",
)

_RECENT_SERVICE_ERROR_WINDOW_SECONDS = 3600


def ensure_schema(con: sqlite3.Connection) -> None:
    for block in (
        _CYCLE_FUNNEL_DDL,
        _ACCOUNT_TRUTH_DDL,
        _SERVICE_VERDICTS_DDL,
        _ANOMALY_LATEST_DDL,
        _ANOMALY_EVENTS_DDL,
    ):
        for stmt in block.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
    con.commit()


def resolve_active_profile_ids(runtime_cfg: dict[str, Any], asset_class: str = "forex") -> list[str]:
    active_ids: list[str] = []
    asset = str(asset_class or "forex").strip().lower() or "forex"
    for profile in (runtime_cfg.get("profiles") or []):
        profile_id = str((profile or {}).get("id") or "").strip()
        lane = (((profile or {}).get("asset_lanes") or {}).get(asset) or {})
        if (
            not profile_id
            or not bool((profile or {}).get("is_active", False))
            or not bool(lane.get("enabled", False))
        ):
            continue
        if str(lane.get("execution_mode") or "").strip().lower() != "auto":
            continue
        active_ids.append(profile_id)
    return active_ids


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso_age_seconds(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)


def _parse_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed
    except Exception:
        return default


def _row_value(row: sqlite3.Row | None, key: str) -> Any:
    if row is None:
        return None
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        return None
    return None


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _latest_stage_status_rows(con: sqlite3.Connection, stage_names: list[str]) -> dict[str, sqlite3.Row]:
    if not stage_names or not _table_exists(con, "vanguard_stage_runs"):
        return {}
    placeholders = ",".join("?" for _ in stage_names)
    rows = con.execute(
        f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY stage_name
                       ORDER BY cycle_ts_utc DESC, run_id DESC
                   ) AS rn
            FROM vanguard_stage_runs
            WHERE stage_name IN ({placeholders})
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        stage_names,
    ).fetchall()
    return {str(row["stage_name"] or ""): row for row in rows}


def _load_manifest(manifest_path: Path | str | None) -> dict[str, Any]:
    path = Path(manifest_path or _DEFAULT_MANIFEST_PATH)
    return json.loads(path.read_text())


def _read_pid(pid_file: str) -> int | None:
    path = Path(str(pid_file))
    if not path.exists():
        return None
    raw = path.read_text().strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _log_age_seconds(path_str: str | None) -> int | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    return max(int(time.time() - path.stat().st_mtime), 0)


def _cycle_interval_seconds(runtime_cfg: dict[str, Any]) -> int:
    return int(((runtime_cfg.get("runtime") or {}).get("cycle_interval_seconds")) or 300)


def _latest_cycle_ts(con: sqlite3.Connection, profile_ids: list[str]) -> str | None:
    if not profile_ids or not _table_exists(con, "vanguard_cycle_audit"):
        return None
    placeholders = ",".join("?" for _ in profile_ids)
    row = con.execute(
        f"""
        SELECT MAX(cycle_ts_utc)
        FROM vanguard_cycle_audit
        WHERE profile_id IN ({placeholders})
        """,
        profile_ids,
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def collect_cycle_funnels(
    con: sqlite3.Connection,
    profile_ids: list[str],
    *,
    cycle_ts_utc: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(con)
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids or not _table_exists(con, "vanguard_cycle_audit"):
        return []
    cycle_ts = cycle_ts_utc or _latest_cycle_ts(con, clean_profile_ids)
    if not cycle_ts:
        return []
    placeholders = ",".join("?" for _ in clean_profile_ids)
    cycle_rows = con.execute(
        f"""
        SELECT *
        FROM vanguard_cycle_audit
        WHERE cycle_ts_utc = ?
          AND profile_id IN ({placeholders})
        ORDER BY profile_id
        """,
        [cycle_ts, *clean_profile_ids],
    ).fetchall()
    if not cycle_rows:
        return []

    state_counts: dict[str, dict[str, int]] = {}
    if _table_exists(con, "vanguard_v5_selection"):
        for row in con.execute(
            f"""
            SELECT profile_id,
                   COUNT(*) AS n,
                   SUM(CASE WHEN COALESCE(top_rank_streak, 0) >= 1 THEN 1 ELSE 0 END) AS top_rank_rows,
                   SUM(CASE WHEN COALESCE(direction_streak, 0) >= 1 THEN 1 ELSE 0 END) AS direction_rows,
                   SUM(CASE WHEN COALESCE(strong_prediction_streak, 0) >= 1 THEN 1 ELSE 0 END) AS strong_rows
            FROM vanguard_v5_selection
            WHERE cycle_ts_utc = ?
              AND profile_id IN ({placeholders})
            GROUP BY profile_id
            """,
            [cycle_ts, *clean_profile_ids],
        ):
            state_counts[str(row["profile_id"])] = {
                "selection_rows": int(row["n"] or 0),
                "state_top_rank_rows": int(row["top_rank_rows"] or 0),
                "state_direction_rows": int(row["direction_rows"] or 0),
                "state_strong_rows": int(row["strong_rows"] or 0),
            }

    decision_counts: dict[str, dict[str, int]] = {}
    if _table_exists(con, "vanguard_signal_decision_log"):
        for row in con.execute(
            f"""
            SELECT profile_id,
                   SUM(CASE WHEN execution_decision = 'EXECUTED' THEN 1 ELSE 0 END) AS executed_rows,
                   SUM(CASE WHEN execution_decision = 'SKIPPED_POLICY' THEN 1 ELSE 0 END) AS skipped_policy_rows,
                   SUM(CASE WHEN execution_decision = 'SKIPPED_META_GATE' THEN 1 ELSE 0 END) AS skipped_meta_gate_rows
            FROM vanguard_signal_decision_log
            WHERE cycle_ts_utc = ?
              AND profile_id IN ({placeholders})
            GROUP BY profile_id
            """,
            [cycle_ts, *clean_profile_ids],
        ):
            decision_counts[str(row["profile_id"])] = {
                "executed_rows": int(row["executed_rows"] or 0),
                "skipped_policy_rows": int(row["skipped_policy_rows"] or 0),
                "skipped_meta_gate_rows": int(row["skipped_meta_gate_rows"] or 0),
            }

    prediction_counts: dict[tuple[str, str], dict[str, Any]] = {}
    if _table_exists(con, "vanguard_predictions_history"):
        stream_ids = sorted(
            {
                str(row["decision_stream_id"] or "").strip()
                for row in cycle_rows
                if str(row["decision_stream_id"] or "").strip()
            }
        )
        if stream_ids:
            stream_placeholders = ",".join("?" for _ in stream_ids)
            for row in con.execute(
                f"""
                SELECT decision_stream_id,
                       COUNT(*) AS n,
                       MIN(model_id) AS model_id
                FROM vanguard_predictions_history
                WHERE cycle_ts_utc = ?
                  AND decision_stream_id IN ({stream_placeholders})
                GROUP BY decision_stream_id
                """,
                [cycle_ts, *stream_ids],
            ):
                prediction_counts[(cycle_ts, str(row["decision_stream_id"] or ""))] = {
                    "predictions_count": int(row["n"] or 0),
                    "model_id": str(row["model_id"] or ""),
                }

    open_positions: dict[str, tuple[int, float]] = {}
    if _table_exists(con, "vanguard_open_positions"):
        for row in con.execute(
            f"""
            SELECT profile_id, COUNT(*) AS n, COALESCE(SUM(unrealized_pnl), 0.0) AS upl
            FROM vanguard_open_positions
            WHERE profile_id IN ({placeholders})
            GROUP BY profile_id
            """,
            clean_profile_ids,
        ):
            open_positions[str(row["profile_id"])] = (int(row["n"] or 0), float(row["upl"] or 0.0))

    now_iso = _iso_now()
    funnels: list[dict[str, Any]] = []
    for row in cycle_rows:
        profile_id = str(row["profile_id"] or "")
        decision_stream_id = str(row["decision_stream_id"] or "")
        pred_info = prediction_counts.get((cycle_ts, decision_stream_id), {})
        state = state_counts.get(profile_id, {})
        decision = decision_counts.get(profile_id, {})
        open_count, open_upl = open_positions.get(profile_id, (0, 0.0))
        funnels.append(
            {
                "cycle_ts_utc": cycle_ts,
                "profile_id": profile_id,
                "asset_class": str(row["asset_class"] or ""),
                "decision_stream_id": decision_stream_id,
                "model_id": pred_info.get("model_id") or "",
                "execution_policy_id": str(row["execution_policy_id"] or ""),
                "execution_mode": str(row["execution_mode"] or ""),
                "predictions_count": int(pred_info.get("predictions_count") or 0),
                "tradeability_rows": int(row["tradeability_rows"] or 0),
                "tradeability_pass_rows": int(row["tradeability_pass_rows"] or 0),
                "economics_pass_rows": int(row["economics_pass_rows"] or 0),
                "economics_fail_rows": int(row["economics_fail_rows"] or 0),
                "state_top_rank_rows": int(state.get("state_top_rank_rows") or 0),
                "state_direction_rows": int(state.get("state_direction_rows") or 0),
                "state_strong_rows": int(state.get("state_strong_rows") or 0),
                "live_policy_filtered_rows": int(row["live_policy_filtered_rows"] or 0),
                "route_candidate_rows": int(row["route_candidate_rows"] or 0),
                "selected_rows": int(row["selected_rows"] or 0),
                "portfolio_approved_rows": int(row["portfolio_approved_rows"] or 0),
                "portfolio_rejected_rows": int(row["portfolio_rejected_rows"] or 0),
                "decision_rows": int(row["decision_rows"] or 0),
                "executed_rows": int(decision.get("executed_rows") or 0),
                "skipped_policy_rows": int(decision.get("skipped_policy_rows") or 0),
                "skipped_meta_gate_rows": int(decision.get("skipped_meta_gate_rows") or 0),
                "open_positions_count": open_count,
                "open_positions_upl": open_upl,
                "live_policy_failed_checks_json": str(row["live_policy_failed_checks_json"] or "{}"),
                "portfolio_reasons_json": str(row["portfolio_reasons_json"] or "{}"),
                "decision_counts_json": str(row["decision_counts_json"] or "{}"),
                "created_at_utc": now_iso,
                "updated_at_utc": now_iso,
            }
        )
    return funnels


def persist_cycle_funnels(con: sqlite3.Connection, funnels: list[dict[str, Any]]) -> None:
    if not funnels:
        return
    ensure_schema(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_diagnostics_cycle_funnel (
            cycle_ts_utc, profile_id, asset_class, decision_stream_id, model_id,
            execution_policy_id, execution_mode, predictions_count, tradeability_rows,
            tradeability_pass_rows, economics_pass_rows, economics_fail_rows,
            state_top_rank_rows, state_direction_rows, state_strong_rows,
            live_policy_filtered_rows, route_candidate_rows, selected_rows,
            portfolio_approved_rows, portfolio_rejected_rows, decision_rows,
            executed_rows, skipped_policy_rows, skipped_meta_gate_rows,
            open_positions_count, open_positions_upl, live_policy_failed_checks_json,
            portfolio_reasons_json, decision_counts_json, created_at_utc, updated_at_utc
        ) VALUES (
            :cycle_ts_utc, :profile_id, :asset_class, :decision_stream_id, :model_id,
            :execution_policy_id, :execution_mode, :predictions_count, :tradeability_rows,
            :tradeability_pass_rows, :economics_pass_rows, :economics_fail_rows,
            :state_top_rank_rows, :state_direction_rows, :state_strong_rows,
            :live_policy_filtered_rows, :route_candidate_rows, :selected_rows,
            :portfolio_approved_rows, :portfolio_rejected_rows, :decision_rows,
            :executed_rows, :skipped_policy_rows, :skipped_meta_gate_rows,
            :open_positions_count, :open_positions_upl, :live_policy_failed_checks_json,
            :portfolio_reasons_json, :decision_counts_json, :created_at_utc, :updated_at_utc
        )
        """,
        funnels,
    )
    con.commit()


def collect_account_truth(
    con: sqlite3.Connection,
    profile_ids: list[str],
) -> list[dict[str, Any]]:
    ensure_schema(con)
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids or not _table_exists(con, "vanguard_context_account_latest"):
        return []
    placeholders = ",".join("?" for _ in clean_profile_ids)
    latest_rows = con.execute(
        f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY profile_id
                       ORDER BY received_ts_utc DESC
                   ) AS rn
            FROM vanguard_context_account_latest
            WHERE profile_id IN ({placeholders})
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        clean_profile_ids,
    ).fetchall()
    open_positions: dict[str, tuple[int, float]] = {}
    if _table_exists(con, "vanguard_open_positions"):
        for row in con.execute(
            f"""
            SELECT profile_id, COUNT(*) AS n, COALESCE(SUM(unrealized_pnl), 0.0) AS upl
            FROM vanguard_open_positions
            WHERE profile_id IN ({placeholders})
            GROUP BY profile_id
            """,
            clean_profile_ids,
        ):
            open_positions[str(row["profile_id"])] = (int(row["n"] or 0), float(row["upl"] or 0.0))

    observed_ts = _iso_now()
    out: list[dict[str, Any]] = []
    for row in latest_rows:
        profile_id = str(row["profile_id"] or "")
        payload = _parse_json(_row_value(row, "raw_payload_json"), {})
        cached_fallback = bool(payload.get("cached_fallback"))
        open_count, live_upl = open_positions.get(profile_id, (0, 0.0))
        balance = float(_row_value(row, "balance")) if _row_value(row, "balance") is not None else None
        equity_snapshot = float(_row_value(row, "equity")) if _row_value(row, "equity") is not None else None
        equity_est = (balance + live_upl) if balance is not None else None
        age_s = _iso_age_seconds(str(_row_value(row, "received_ts_utc") or ""))
        truth_state = "OK"
        if age_s is None or age_s > 300:
            truth_state = "STALE"
        elif cached_fallback and open_count > 0:
            truth_state = "CACHED_FALLBACK"
        out.append(
            {
                "observed_ts_utc": observed_ts,
                "profile_id": profile_id,
                "account_number": str(_row_value(row, "account_number") or ""),
                "received_ts_utc": str(_row_value(row, "received_ts_utc") or ""),
                "source": str(_row_value(row, "source") or ""),
                "source_status": str(_row_value(row, "source_status") or ""),
                "source_error": str(_row_value(row, "source_error") or ""),
                "cached_fallback": 1 if cached_fallback else 0,
                "balance": balance,
                "equity_snapshot": equity_snapshot,
                "live_upl": live_upl,
                "equity_estimate": equity_est,
                "open_positions_count": open_count,
                "truth_state": truth_state,
                "details_json": json.dumps(
                    {
                        "account_age_seconds": age_s,
                        "equity_snapshot": equity_snapshot,
                        "equity_estimate": equity_est,
                        "open_positions_count": open_count,
                    },
                    sort_keys=True,
                ),
            }
        )
    return out


def persist_account_truth(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_diagnostics_account_truth (
            observed_ts_utc, profile_id, account_number, received_ts_utc, source,
            source_status, source_error, cached_fallback, balance, equity_snapshot,
            live_upl, equity_estimate, open_positions_count, truth_state, details_json
        ) VALUES (
            :observed_ts_utc, :profile_id, :account_number, :received_ts_utc, :source,
            :source_status, :source_error, :cached_fallback, :balance, :equity_snapshot,
            :live_upl, :equity_estimate, :open_positions_count, :truth_state, :details_json
        )
        """,
        rows,
    )
    con.commit()


def _service_freshness_seconds(
    service_name: str,
    runtime_cfg: dict[str, Any],
    cycle_interval_seconds: int,
    service_cfg: dict[str, Any] | None = None,
) -> int:
    service_name = str(service_name or "")
    service_cfg = service_cfg or {}
    explicit = service_cfg.get("freshness_seconds")
    if explicit is not None:
        try:
            return max(int(explicit), 1)
        except Exception:
            pass
    if service_name == "v1_precompute":
        return int(((runtime_cfg.get("runtime") or {}).get("v1_precompute") or {}).get("freshness_seconds") or 900)
    if service_name == "model_truth_snapshot":
        return max(900, cycle_interval_seconds * 3)
    if service_name == "metaapi_live_context":
        return max(60, cycle_interval_seconds)
    if service_name == "metaapi_candle_cache":
        interval_seconds = int((((runtime_cfg.get("market_data") or {}).get("metaapi_candle_cache")) or {}).get("interval_seconds") or 60)
        return max(180, interval_seconds * 3)
    return int(_SERVICE_FRESHNESS_DEFAULTS.get(service_name, 600))


def collect_service_verdicts(
    con: sqlite3.Connection,
    runtime_cfg: dict[str, Any],
    manifest_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(con)
    manifest = _load_manifest(manifest_path)
    services = manifest.get("services") or {}
    cycle_interval_seconds = _cycle_interval_seconds(runtime_cfg)
    observed_ts = _iso_now()
    service_state_rows: dict[str, sqlite3.Row] = {}
    if _table_exists(con, "vanguard_service_state"):
        for row in con.execute("SELECT * FROM vanguard_service_state").fetchall():
            service_state_rows[str(row["service_name"] or "")] = row

    out: list[dict[str, Any]] = []
    for service_name, service_cfg in services.items():
        pid_file = str(service_cfg.get("pid_file") or "")
        log_file = str(service_cfg.get("log_file") or "")
        pid = _read_pid(pid_file)
        alive = _pid_alive(pid)
        log_age_seconds = _log_age_seconds(log_file)
        freshness_seconds = _service_freshness_seconds(
            service_name,
            runtime_cfg,
            cycle_interval_seconds,
            service_cfg=service_cfg,
        )
        service_row = service_state_rows.get(service_name)
        last_success_ts = str(service_row["last_success_ts_utc"]) if service_row and service_row["last_success_ts_utc"] else None
        success_age = _iso_age_seconds(last_success_ts)
        if not alive:
            freshness_state = "DOWN"
        elif success_age is not None and success_age <= freshness_seconds:
            freshness_state = "OK"
        elif log_age_seconds is not None and log_age_seconds <= freshness_seconds:
            freshness_state = "LOG_ONLY"
        elif service_row is None:
            freshness_state = "PROCESS_ONLY"
        else:
            freshness_state = "STALE"
        out.append(
            {
                "observed_ts_utc": observed_ts,
                "service_name": service_name,
                "required_for_ready": 1 if bool(service_cfg.get("required_for_ready")) else 0,
                "process_alive": 1 if alive else 0,
                "pid": pid,
                "freshness_state": freshness_state,
                "freshness_seconds": freshness_seconds,
                "last_success_ts_utc": last_success_ts,
                "last_error_ts_utc": str(service_row["last_error_ts_utc"]) if service_row and service_row["last_error_ts_utc"] else None,
                "log_age_seconds": log_age_seconds,
                "expected_write_surface": str(service_cfg.get("expected_write_surface") or "") or (
                    f"service_state:{service_name}" if service_row else f"log:{Path(log_file).name}"
                ),
                "details_json": json.dumps(
                    {
                        "pid_file": pid_file,
                        "log_file": log_file,
                        "required_for_ready": bool(service_cfg.get("required_for_ready")),
                        "service_state_status": str(service_row["status"] or "") if service_row else "",
                    },
                    sort_keys=True,
                ),
            }
        )
    return out


def persist_service_verdicts(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_schema(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_diagnostics_service_verdicts (
            observed_ts_utc, service_name, required_for_ready, process_alive, pid,
            freshness_state, freshness_seconds, last_success_ts_utc, last_error_ts_utc,
            log_age_seconds, expected_write_surface, details_json
        ) VALUES (
            :observed_ts_utc, :service_name, :required_for_ready, :process_alive, :pid,
            :freshness_state, :freshness_seconds, :last_success_ts_utc, :last_error_ts_utc,
            :log_age_seconds, :expected_write_surface, :details_json
        )
        """,
        rows,
    )
    con.commit()


def _recent_funnels(con: sqlite3.Connection, profile_id: str, limit: int = 3) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT *
        FROM vanguard_diagnostics_cycle_funnel
        WHERE profile_id = ?
        ORDER BY cycle_ts_utc DESC
        LIMIT ?
        """,
        (profile_id, int(limit)),
    ).fetchall()


def classify_funnel_seam(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, str]:
    predictions = int(_row_value(row, "predictions_count") or 0)
    tradeability_rows = int(_row_value(row, "tradeability_rows") or 0)
    economics_pass = int(_row_value(row, "economics_pass_rows") or 0)
    live_policy_filtered = int(_row_value(row, "live_policy_filtered_rows") or 0)
    route_candidates = int(_row_value(row, "route_candidate_rows") or 0)
    approved = int(_row_value(row, "portfolio_approved_rows") or 0)
    executed = int(_row_value(row, "executed_rows") or 0)
    skipped_meta_gate = int(_row_value(row, "skipped_meta_gate_rows") or 0)
    state_rows = max(
        int(_row_value(row, "state_top_rank_rows") or 0),
        int(_row_value(row, "state_direction_rows") or 0),
        int(_row_value(row, "state_strong_rows") or 0),
    )
    failed_checks = _parse_json(_row_value(row, "live_policy_failed_checks_json"), {})
    execution_attempt_reason_codes = _parse_json(_row_value(row, "execution_attempt_reason_codes_json"), [])
    execution_attempt_statuses = _parse_json(_row_value(row, "execution_attempt_statuses_json"), [])

    if predictions <= 0:
        return {"status": "idle", "seam": "idle", "reason": "no predictions written"}
    if (
        skipped_meta_gate >= max(5, int(predictions * 0.4))
        and route_candidates == 0
        and approved == 0
        and executed == 0
    ):
        return {"status": "blocked", "seam": "meta_gate", "reason": "MetaGate rejected most viable candidates"}
    if economics_pass <= 0:
        return {"status": "blocked", "seam": "economics", "reason": "economics layer rejected everything"}
    if state_rows <= 0:
        return {"status": "broken", "seam": "state_layer", "reason": "state layer has no buildup despite economics pass"}
    if route_candidates <= 0 and live_policy_filtered >= max(tradeability_rows - 1, 1):
        return {"status": "blocked", "seam": "policy", "reason": "live policy rejected healthy upstream candidates"}
    if approved <= 0 and route_candidates > 0:
        return {"status": "blocked", "seam": "portfolio", "reason": "portfolio/risk rejected route candidates"}
    if executed <= 0 and approved > 0:
        if execution_attempt_reason_codes:
            first_reason = str(execution_attempt_reason_codes[0] or "").strip()
            first_status = str(execution_attempt_statuses[0] or "").strip()
            if first_reason:
                return {
                    "status": "blocked",
                    "seam": "execution",
                    "reason": f"execution stopped after risk approval: {first_reason.lower()}",
                }
            if first_status:
                return {
                    "status": "blocked",
                    "seam": "execution",
                    "reason": f"execution stopped after risk approval: {first_status.lower()}",
                }
        return {"status": "blocked", "seam": "execution", "reason": "execution path stopped after risk approval"}
    return {"status": "healthy", "seam": "healthy", "reason": "pipeline is flowing"}


def _latest_account_truth_rows(con: sqlite3.Connection, profile_ids: list[str]) -> list[sqlite3.Row]:
    if not profile_ids:
        return []
    placeholders = ",".join("?" for _ in profile_ids)
    return con.execute(
        f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY profile_id
                       ORDER BY observed_ts_utc DESC
                   ) AS rn
            FROM vanguard_diagnostics_account_truth
            WHERE profile_id IN ({placeholders})
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        profile_ids,
    ).fetchall()


def _latest_service_verdict_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY service_name
                       ORDER BY observed_ts_utc DESC
                   ) AS rn
            FROM vanguard_diagnostics_service_verdicts
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """
    ).fetchall()


def _latest_selection_handoff_violations(
    con: sqlite3.Connection,
    profile_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not profile_ids or not _table_exists(con, "vanguard_v5_selection"):
        return {}
    placeholders = ",".join("?" for _ in profile_ids)
    rows = con.execute(
        f"""
        WITH latest_cycle AS (
            SELECT MAX(cycle_ts_utc) AS cycle_ts_utc
            FROM vanguard_v5_selection
            WHERE profile_id IN ({placeholders})
              AND COALESCE(selected, 0) = 1
        )
        SELECT *
        FROM vanguard_v5_selection
        WHERE cycle_ts_utc = (SELECT cycle_ts_utc FROM latest_cycle)
          AND profile_id IN ({placeholders})
          AND COALESCE(selected, 0) = 1
        ORDER BY profile_id, selection_rank, symbol
        """,
        [*profile_ids, *profile_ids],
    ).fetchall()
    violations: dict[str, dict[str, Any]] = {}
    for row in rows:
        profile_id = str(row["profile_id"] or "")
        trade_ref = _parse_json(_row_value(row, "tradeability_ref_json"), {})
        missing = [
            field for field in _SELECTION_HANDOFF_REQUIRED_FIELDS
            if not str(trade_ref.get(field) or "").strip()
        ]
        if missing and profile_id not in violations:
            violations[profile_id] = {
                "cycle_ts_utc": str(_row_value(row, "cycle_ts_utc") or ""),
                "symbol": str(_row_value(row, "symbol") or ""),
                "direction": str(_row_value(row, "direction") or ""),
                "missing_fields": missing,
            }
    return violations


def _latest_exit_policy_drifts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(con, "vanguard_trade_journal") or not _table_exists(con, "vanguard_position_lifecycle"):
        return []
    rows = con.execute(
        """
        WITH latest_review AS (
            SELECT trade_id, MAX(review_id) AS max_review_id
            FROM vanguard_position_lifecycle
            GROUP BY trade_id
        )
        SELECT
            tj.trade_id,
            tj.profile_id,
            tj.symbol,
            tj.side,
            tj.status,
            tj.policy_id AS journal_policy_id,
            vpl.policy_id AS lifecycle_policy_id,
            vpl.reviewed_at_utc
        FROM vanguard_trade_journal tj
        JOIN latest_review lr
          ON lr.trade_id = tj.trade_id
        JOIN vanguard_position_lifecycle vpl
          ON vpl.review_id = lr.max_review_id
        WHERE tj.status IN ('OPEN', 'PENDING_FILL', 'SUBMITTED', 'FILLED', 'FORWARD_TRACKED')
        """
    ).fetchall()
    drifts: list[dict[str, Any]] = []
    for row in rows:
        journal_policy_id = str(row["journal_policy_id"] or "").strip()
        lifecycle_policy_id = str(row["lifecycle_policy_id"] or "").strip()
        if journal_policy_id and lifecycle_policy_id and journal_policy_id != lifecycle_policy_id:
            drifts.append(
                {
                    "trade_id": str(row["trade_id"] or ""),
                    "profile_id": str(row["profile_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "side": str(row["side"] or ""),
                    "status": str(row["status"] or ""),
                    "journal_policy_id": journal_policy_id,
                    "lifecycle_policy_id": lifecycle_policy_id,
                    "reviewed_at_utc": str(row["reviewed_at_utc"] or ""),
                }
            )
    return drifts


def _latest_execution_attempt_drifts(
    con: sqlite3.Connection,
    profile_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if (
        not profile_ids
        or not _table_exists(con, "vanguard_tradeable_portfolio")
        or not _table_exists(con, "vanguard_execution_attempts")
    ):
        return {}
    placeholders = ",".join("?" for _ in profile_ids)
    approved_rows = con.execute(
        f"""
        WITH latest_cycle AS (
            SELECT MAX(cycle_ts_utc) AS cycle_ts_utc
            FROM vanguard_tradeable_portfolio
            WHERE account_id IN ({placeholders})
              AND status = 'APPROVED'
        ),
        latest_attempts AS (
            SELECT
                ea.*,
                ROW_NUMBER() OVER (
                    PARTITION BY ea.cycle_ts_utc, ea.profile_id, ea.symbol, ea.direction
                    ORDER BY ea.attempt_id DESC
                ) AS rn
            FROM vanguard_execution_attempts ea
            WHERE ea.profile_id IN ({placeholders})
        )
        SELECT
            tp.cycle_ts_utc,
            tp.account_id,
            tp.symbol,
            tp.direction,
            tp.decision_stream_id,
            ea.attempt_status,
            ea.reason_code
        FROM vanguard_tradeable_portfolio tp
        LEFT JOIN latest_attempts ea
          ON ea.cycle_ts_utc = tp.cycle_ts_utc
         AND ea.profile_id = tp.account_id
         AND ea.symbol = tp.symbol
         AND ea.direction = tp.direction
         AND ea.rn = 1
        WHERE tp.cycle_ts_utc = (SELECT cycle_ts_utc FROM latest_cycle)
          AND tp.account_id IN ({placeholders})
          AND tp.status = 'APPROVED'
        ORDER BY tp.account_id, tp.symbol, tp.direction
        """,
        [*profile_ids, *profile_ids, *profile_ids],
    ).fetchall()
    drifts: dict[str, dict[str, Any]] = {}
    for row in approved_rows:
        attempt_status = str(row["attempt_status"] or "").strip()
        if attempt_status:
            continue
        profile_id = str(row["account_id"] or "")
        drift = drifts.setdefault(
            profile_id,
            {
                "cycle_ts_utc": str(row["cycle_ts_utc"] or ""),
                "approved_without_attempt": [],
            },
        )
        drift["approved_without_attempt"].append(
            {
                "symbol": str(row["symbol"] or ""),
                "direction": str(row["direction"] or ""),
                "decision_stream_id": str(row["decision_stream_id"] or ""),
            }
        )
    return drifts


def _latest_execution_attempt_summary(
    con: sqlite3.Connection,
    profile_ids: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not profile_ids or not _table_exists(con, "vanguard_execution_attempts"):
        return {}
    placeholders = ",".join("?" for _ in profile_ids)
    rows = con.execute(
        f"""
        SELECT
            cycle_ts_utc,
            profile_id,
            symbol,
            direction,
            attempt_status,
            reason_code,
            execution_bridge
        FROM vanguard_execution_attempts
        WHERE profile_id IN ({placeholders})
        ORDER BY cycle_ts_utc DESC, attempt_id DESC
        """,
        profile_ids,
    ).fetchall()
    by_cycle_profile: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["cycle_ts_utc"] or ""), str(row["profile_id"] or ""))
        summary = by_cycle_profile.setdefault(
            key,
            {
                "attempt_statuses": [],
                "reason_codes": [],
                "execution_bridges": [],
                "symbols": [],
            },
        )
        for src, dst in (
            ("attempt_status", "attempt_statuses"),
            ("reason_code", "reason_codes"),
            ("execution_bridge", "execution_bridges"),
            ("symbol", "symbols"),
        ):
            value = str(row[src] or "").strip()
            if value and value not in summary[dst]:
                summary[dst].append(value)
    return by_cycle_profile


def _recent_service_errors(
    con: sqlite3.Connection,
    runtime_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not _table_exists(con, "vanguard_service_state"):
        return []
    cycle_interval_seconds = _cycle_interval_seconds(runtime_cfg)
    recent_rows = con.execute(
        """
        SELECT service_name, status, last_success_ts_utc, last_error_ts_utc, updated_ts_utc, details_json
        FROM vanguard_service_state
        WHERE last_error_ts_utc IS NOT NULL
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    error_window = max(_RECENT_SERVICE_ERROR_WINDOW_SECONDS, cycle_interval_seconds * 3)
    for row in recent_rows:
        last_error_ts = str(row["last_error_ts_utc"] or "")
        last_success_ts = str(row["last_success_ts_utc"] or "")
        error_age = _iso_age_seconds(last_error_ts)
        if error_age is None or error_age > error_window:
            continue
        if last_success_ts:
            try:
                if datetime.fromisoformat(last_success_ts.replace("Z", "+00:00")) > datetime.fromisoformat(last_error_ts.replace("Z", "+00:00")):
                    continue
            except Exception:
                pass
        details = _parse_json(row["details_json"], {})
        out.append(
            {
                "service_name": str(row["service_name"] or ""),
                "status": str(row["status"] or ""),
                "last_error_ts_utc": last_error_ts,
                "last_success_ts_utc": last_success_ts or None,
                "error_age_seconds": error_age,
                "error_message": str(details.get("last_error") or "").strip(),
            }
        )
    return out


def _anomaly(
    *,
    family: str,
    scope_type: str,
    scope_id: str,
    severity: str,
    cycle_ts_utc: str | None,
    summary: str,
    recommended_action: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    key = f"{family}:{scope_type}:{scope_id}"
    return {
        "anomaly_key": key,
        "family": family,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "severity": severity,
        "cycle_ts_utc": cycle_ts_utc,
        "summary": summary,
        "recommended_action": recommended_action,
        "details_json": json.dumps(details, sort_keys=True, default=str),
    }


def detect_anomalies(
    con: sqlite3.Connection,
    runtime_cfg: dict[str, Any],
    profile_ids: list[str],
) -> list[dict[str, Any]]:
    ensure_schema(con)
    anomalies: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def push(anomaly: dict[str, Any]) -> None:
        key = str(anomaly.get("anomaly_key") or "")
        if key and key not in seen_keys:
            seen_keys.add(key)
            anomalies.append(anomaly)

    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    latest_funnels = {
        str(row["profile_id"]): row
        for row in con.execute(
            """
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY profile_id
                           ORDER BY cycle_ts_utc DESC
                       ) AS rn
                FROM vanguard_diagnostics_cycle_funnel
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            """
        ).fetchall()
        if str(row["profile_id"] or "") in clean_profile_ids
    }

    for profile_id in clean_profile_ids:
        recent = _recent_funnels(con, profile_id, limit=3)
        if len(recent) >= 2 and all(int(r["predictions_count"] or 0) > 0 for r in recent[:2]):
            seams = [classify_funnel_seam(r) for r in recent[:2]]
            seam_names = {item["seam"] for item in seams}
            latest = recent[0]
            latest_seam = seams[0]
            if len(seam_names) == 1:
                seam = latest_seam["seam"]
                if seam == "meta_gate":
                    push(
                        _anomaly(
                            family="meta_gate_choke",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="high",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} is being choked by MetaGate across recent cycles.",
                            recommended_action="Inspect live feature warmup, meta scores, and threshold calibration before changing downstream policy.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "recent_skipped_meta_gate_rows": [int(r["skipped_meta_gate_rows"] or 0) for r in recent[:2]],
                                "recent_predictions_count": [int(r["predictions_count"] or 0) for r in recent[:2]],
                            },
                        )
                    )
                elif seam == "economics":
                    push(
                        _anomaly(
                            family="economics_blocked",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="medium",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} has predictions but no economically viable candidates across recent cycles.",
                            recommended_action="Inspect conviction, predicted-return, session/liquidity, and cost inputs before treating this as a slot wiring fault.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "recent_economics_fail_rows": [int(r["economics_fail_rows"] or 0) for r in recent[:2]],
                                "recent_tradeability_rows": [int(r["tradeability_rows"] or 0) for r in recent[:2]],
                            },
                        )
                    )
                elif seam == "policy":
                    push(
                        _anomaly(
                            family="policy_choke",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="high",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} is policy-choked across recent cycles.",
                            recommended_action="Inspect live policy failed checks and compare them against the last healthy control/fleet cycles.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "failed_checks": _parse_json(_row_value(latest, "live_policy_failed_checks_json"), {}),
                                "recent_economics_pass_rows": [int(r["economics_pass_rows"] or 0) for r in recent[:2]],
                            },
                        )
                    )
                elif seam == "portfolio":
                    push(
                        _anomaly(
                            family="portfolio_blocked",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="high",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} is producing route candidates, but portfolio/risk is blocking them across recent cycles.",
                            recommended_action="Inspect portfolio rejection reasons and open-risk/position-cap state.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "recent_route_candidate_rows": [int(r["route_candidate_rows"] or 0) for r in recent[:2]],
                                "recent_portfolio_rejected_rows": [int(r["portfolio_rejected_rows"] or 0) for r in recent[:2]],
                            },
                        )
                    )
                elif seam == "execution":
                    push(
                        _anomaly(
                            family="execution_blocked",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="critical",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} is approving trades, but nothing is reaching execution across recent cycles.",
                            recommended_action="Inspect execution bridge, order journal, and broker submit path.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "recent_portfolio_approved_rows": [int(r["portfolio_approved_rows"] or 0) for r in recent[:2]],
                                "recent_executed_rows": [int(r["executed_rows"] or 0) for r in recent[:2]],
                            },
                        )
                    )
                elif seam == "state_layer":
                    push(
                        _anomaly(
                            family="dead_slot",
                            scope_type="profile",
                            scope_id=profile_id,
                            severity="high",
                            cycle_ts_utc=str(latest["cycle_ts_utc"] or ""),
                            summary=f"{profile_id} is dead because the state layer is not building despite viable upstream candidates.",
                            recommended_action="Inspect streak persistence inputs and V5 state-memory updates.",
                            details={
                                "recent_cycles": [str(r["cycle_ts_utc"] or "") for r in recent[:2]],
                                "recent_economics_pass_rows": [int(r["economics_pass_rows"] or 0) for r in recent[:2]],
                                "recent_state_top_rank_rows": [int(r["state_top_rank_rows"] or 0) for r in recent[:2]],
                                "recent_state_direction_rows": [int(r["state_direction_rows"] or 0) for r in recent[:2]],
                            },
                        )
                    )

    if len(clean_profile_ids) >= 2:
        ratios: dict[str, dict[str, float]] = {}
        for profile_id in clean_profile_ids:
            row = latest_funnels.get(profile_id)
            if not row or int(row["tradeability_rows"] or 0) <= 0:
                continue
            denom = max(int(row["tradeability_rows"] or 0), 1)
            ratios[profile_id] = {
                "top_rank_ratio": float(row["state_top_rank_rows"] or 0) / denom,
                "direction_ratio": float(row["state_direction_rows"] or 0) / denom,
                "strong_ratio": float(row["state_strong_rows"] or 0) / denom,
            }
        for profile_id, ratio in ratios.items():
            peers = [other for pid, other in ratios.items() if pid != profile_id]
            if not peers:
                continue
            peer_top = statistics.median([item["top_rank_ratio"] for item in peers])
            peer_dir = statistics.median([item["direction_ratio"] for item in peers])
            if (
                ratio["top_rank_ratio"] <= 0.05
                and peer_top >= 0.20
                and ratio["direction_ratio"] <= 0.05
                and peer_dir >= 0.15
            ):
                row = latest_funnels[profile_id]
                push(
                    _anomaly(
                        family="state_layer_broken",
                        scope_type="profile",
                        scope_id=profile_id,
                        severity="critical",
                        cycle_ts_utc=str(row["cycle_ts_utc"] or ""),
                        summary=f"{profile_id} state-layer buildup is materially lower than peers.",
                        recommended_action="Compare streak columns across slots and inspect V5 selection memory inputs.",
                        details={
                            "profile_ratios": ratio,
                            "peer_median_top_rank_ratio": peer_top,
                            "peer_median_direction_ratio": peer_dir,
                            "cycle_ts_utc": str(row["cycle_ts_utc"] or ""),
                        },
                    )
                )

    for profile_id, row in latest_funnels.items():
        if (
            classify_funnel_seam(row)["seam"] == "policy"
            and int(row["route_candidate_rows"] or 0) == 0
        ):
            push(
                _anomaly(
                    family="policy_choke",
                    scope_type="profile",
                    scope_id=profile_id,
                    severity="high",
                    cycle_ts_utc=str(row["cycle_ts_utc"] or ""),
                    summary=f"{profile_id} is policy-choked despite healthy upstream economics.",
                    recommended_action="Inspect live policy failed checks and compare against peer slots.",
                    details={
                        "economics_pass_rows": int(row["economics_pass_rows"] or 0),
                        "live_policy_filtered_rows": int(row["live_policy_filtered_rows"] or 0),
                        "tradeability_rows": int(row["tradeability_rows"] or 0),
                        "failed_checks": _parse_json(row["live_policy_failed_checks_json"], {}),
                    },
                )
            )

    account_truth_rows = _latest_account_truth_rows(con, clean_profile_ids)
    for row in account_truth_rows:
        truth_state = str(row["truth_state"] or "")
        if truth_state == "OK":
            continue
        profile_id = str(row["profile_id"] or "")
        push(
            _anomaly(
                family="account_truth_degraded",
                scope_type="profile",
                scope_id=profile_id,
                severity="medium" if truth_state == "CACHED_FALLBACK" else "high",
                cycle_ts_utc=None,
                summary=f"{profile_id} account truth is {truth_state.lower().replace('_', ' ')}.",
                recommended_action="Inspect context account freshness and use estimated equity in operator messaging.",
                details={
                    "truth_state": truth_state,
                    "cached_fallback": bool(row["cached_fallback"]),
                    "open_positions_count": int(row["open_positions_count"] or 0),
                    "received_ts_utc": str(row["received_ts_utc"] or ""),
                },
            )
        )

    service_rows = _latest_service_verdict_rows(con)
    for row in service_rows:
        service_name = str(row["service_name"] or "")
        freshness_state = str(row["freshness_state"] or "")
        required = bool(row["required_for_ready"])
        if freshness_state in {"OK", "LOG_ONLY", "PROCESS_ONLY"}:
            continue
        if not required and service_name not in _CRITICAL_SERVICE_NAMES:
            continue
        push(
            _anomaly(
                family="service_drift",
                scope_type="service",
                scope_id=service_name,
                severity="critical" if required else "medium",
                cycle_ts_utc=None,
                summary=f"{service_name} is {freshness_state.lower()} while the stack expects it to be writing.",
                recommended_action="Inspect supervisor status and service log freshness.",
                details={
                    "freshness_state": freshness_state,
                    "process_alive": bool(row["process_alive"]),
                    "required_for_ready": required,
                    "last_success_ts_utc": str(row["last_success_ts_utc"] or ""),
                },
            )
        )

    latest_by_table: dict[str, str | None] = {}
    for table_name in _STAGE_TABLES:
        if not _table_exists(con, table_name):
            latest_by_table[table_name] = None
            continue
        row = con.execute(f"SELECT MAX(cycle_ts_utc) FROM {table_name}").fetchone()
        latest_by_table[table_name] = str(row[0]) if row and row[0] else None
    for idx in range(len(_STAGE_TABLES) - 1):
        upstream = _STAGE_TABLES[idx]
        downstream = _STAGE_TABLES[idx + 1]
        upstream_ts = latest_by_table.get(upstream)
        downstream_ts = latest_by_table.get(downstream)
        should_expect_downstream = True
        if upstream == "vanguard_v5_selection" and downstream == "vanguard_tradeable_portfolio":
            should_expect_downstream = any(
                int(row["route_candidate_rows"] or 0) > 0 or int(row["selected_rows"] or 0) > 0
                for row in latest_funnels.values()
            )
        elif upstream == "vanguard_tradeable_portfolio" and downstream == "vanguard_signal_decision_log":
            should_expect_downstream = any(
                int(row["portfolio_approved_rows"] or 0) > 0
                for row in latest_funnels.values()
            )
        if should_expect_downstream and upstream_ts and (not downstream_ts or downstream_ts < upstream_ts):
            push(
                _anomaly(
                    family="table_pipeline_gap",
                    scope_type="table",
                    scope_id=f"{upstream}->{downstream}",
                    severity="high",
                    cycle_ts_utc=upstream_ts,
                    summary=f"Downstream table {downstream} is lagging {upstream}.",
                    recommended_action="Inspect the broken seam between the two pipeline stages.",
                    details={
                        "upstream": upstream,
                        "upstream_cycle_ts_utc": upstream_ts,
                        "downstream": downstream,
                        "downstream_cycle_ts_utc": downstream_ts,
                    },
                )
            )
            break

    latest_stage_rows = _latest_stage_status_rows(con, list(_CONTRACT_STAGE_OUTPUTS))
    for stage_name, output_table in _CONTRACT_STAGE_OUTPUTS.items():
        stage_row = latest_stage_rows.get(stage_name)
        if not stage_row:
            continue
        stage_status = str(stage_row["status"] or "")
        stage_cycle = str(stage_row["cycle_ts_utc"] or "")
        table_cycle = latest_by_table.get(output_table)
        if stage_status not in {"OK", "SKIPPED"}:
            push(
                _anomaly(
                    family="stage_contract_drift",
                    scope_type="stage",
                    scope_id=stage_name,
                    severity="high",
                    cycle_ts_utc=stage_cycle or None,
                    summary=f"{stage_name} finished with status={stage_status} on the latest audited cycle.",
                    recommended_action=f"Inspect {stage_name} stage output and logs before trusting downstream tables.",
                    details={
                        "stage_name": stage_name,
                        "stage_status": stage_status,
                        "stage_cycle_ts_utc": stage_cycle,
                        "expected_output_table": output_table,
                        "output_table_cycle_ts_utc": table_cycle,
                    },
                )
            )
        elif stage_cycle and table_cycle and table_cycle < stage_cycle:
            push(
                _anomaly(
                    family="stage_contract_drift",
                    scope_type="stage",
                    scope_id=stage_name,
                    severity="critical",
                    cycle_ts_utc=stage_cycle,
                    summary=f"{stage_name} advanced, but {output_table} is stale against the audited stage cycle.",
                    recommended_action=f"Inspect the {stage_name} persistence path and downstream consumers of {output_table}.",
                    details={
                        "stage_name": stage_name,
                        "stage_status": stage_status,
                        "stage_cycle_ts_utc": stage_cycle,
                        "expected_output_table": output_table,
                        "output_table_cycle_ts_utc": table_cycle,
                    },
                )
            )

    handoff_violations = _latest_selection_handoff_violations(con, clean_profile_ids)
    for profile_id, details in handoff_violations.items():
        push(
            _anomaly(
                family="selection_handoff_drift",
                scope_type="profile",
                scope_id=profile_id,
                severity="high",
                cycle_ts_utc=str(details.get("cycle_ts_utc") or ""),
                summary=f"{profile_id} has selected V5.2 rows with malformed canonical handoff payload.",
                recommended_action="Inspect vanguard_v5_selection.tradeability_ref_json and restore required V5.1 binding fields before running V6.",
                details=details,
            )
        )

    for drift in _latest_exit_policy_drifts(con):
        push(
            _anomaly(
                family="exit_policy_drift",
                scope_type="trade",
                scope_id=str(drift.get("trade_id") or ""),
                severity="high",
                cycle_ts_utc=None,
                summary=(
                    f"{drift.get('profile_id')} {drift.get('symbol')} {drift.get('side')} "
                    "has journal/lifecycle exit policy drift."
                ),
                recommended_action="Inspect shared exit-policy resolution and ensure trade journal policy_id matches latest lifecycle review policy_id.",
                details=drift,
            )
        )

    execution_attempt_drifts = _latest_execution_attempt_drifts(con, clean_profile_ids)
    for profile_id, details in execution_attempt_drifts.items():
        push(
            _anomaly(
                family="execution_attempt_drift",
                scope_type="profile",
                scope_id=profile_id,
                severity="high",
                cycle_ts_utc=str(details.get("cycle_ts_utc") or ""),
                summary=f"{profile_id} has approved V6 rows without a canonical execution-attempt outcome.",
                recommended_action="Inspect the V7 execution path and ensure every APPROVED candidate records a final execution attempt outcome.",
                details=details,
            )
        )

    for details in _recent_service_errors(con, runtime_cfg):
        service_name = str(details.get("service_name") or "")
        error_message = str(details.get("error_message") or "").strip()
        summary = f"{service_name} has a recent unresolved cycle error."
        if error_message:
            summary = f"{service_name} has a recent unresolved cycle error: {error_message}"
        push(
            _anomaly(
                family="cycle_error",
                scope_type="service",
                scope_id=service_name,
                severity="critical" if service_name == "orchestrator" else "high",
                cycle_ts_utc=None,
                summary=summary,
                recommended_action="Inspect the service error, confirm the latest cycle completed successfully, and clear the root cause before trusting downstream state.",
                details=details,
            )
        )

    if _table_exists(con, "vanguard_predictions_history") and latest_funnels:
        latest_cycle = max(str(row["cycle_ts_utc"] or "") for row in latest_funnels.values())
        stream_to_profile = {
            str(row["decision_stream_id"] or ""): str(row["profile_id"] or "")
            for row in latest_funnels.values()
            if str(row["decision_stream_id"] or "")
        }
        if len(stream_to_profile) >= 2:
            placeholders = ",".join("?" for _ in stream_to_profile)
            rows = con.execute(
                f"""
                SELECT decision_stream_id, symbol, direction, model_id, predicted_return, conviction
                FROM vanguard_predictions_history
                WHERE cycle_ts_utc = ?
                  AND decision_stream_id IN ({placeholders})
                """,
                [latest_cycle, *stream_to_profile.keys()],
            ).fetchall()
            by_stream: dict[str, dict[tuple[str, str], tuple[str, float, float]]] = {}
            for row in rows:
                by_stream.setdefault(str(row["decision_stream_id"] or ""), {})[
                    (str(row["symbol"] or ""), str(row["direction"] or ""))
                ] = (
                    str(row["model_id"] or ""),
                    float(row["predicted_return"] or 0.0),
                    float(row["conviction"] or 0.0),
                )
            stream_ids = sorted(by_stream)
            for idx, lhs in enumerate(stream_ids):
                for rhs in stream_ids[idx + 1:]:
                    left_rows = by_stream[lhs]
                    right_rows = by_stream[rhs]
                    common = sorted(set(left_rows) & set(right_rows))
                    if len(common) < 20:
                        continue
                    identical = 0
                    left_model = next(iter(left_rows.values()))[0]
                    right_model = next(iter(right_rows.values()))[0]
                    if left_model == right_model:
                        continue
                    for key in common:
                        _, left_pred, left_conv = left_rows[key]
                        _, right_pred, right_conv = right_rows[key]
                        if abs(left_pred - right_pred) <= 1e-9 and abs(left_conv - right_conv) <= 1e-9:
                            identical += 1
                    ratio = identical / len(common)
                    left_profile = str(stream_to_profile.get(lhs) or "")
                    right_profile = str(stream_to_profile.get(rhs) or "")
                    if "metagate" in lhs.lower() or "metagate" in rhs.lower():
                        continue
                    if "metagate" in left_profile.lower() or "metagate" in right_profile.lower():
                        continue
                    if ratio >= 0.95:
                        push(
                            _anomaly(
                                family="model_identity_mismatch",
                                scope_type="stream_pair",
                                scope_id=f"{lhs}::{rhs}",
                                severity="critical",
                                cycle_ts_utc=latest_cycle,
                                summary=f"Streams {lhs} and {rhs} are emitting near-identical predictions despite different model_ids.",
                                recommended_action="Inspect scorer checkpoint selection and per-stream model wiring.",
                                details={
                                    "left_profile_id": left_profile,
                                    "right_profile_id": right_profile,
                                    "left_model_id": left_model,
                                    "right_model_id": right_model,
                                    "identical_rows": identical,
                                    "total_common_rows": len(common),
                                    "identical_ratio": ratio,
                                },
                            )
                        )
    return anomalies


def persist_anomaly_snapshot(
    con: sqlite3.Connection,
    anomalies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ensure_schema(con)
    now_iso = _iso_now()
    active_rows = {
        str(row["anomaly_key"]): row
        for row in con.execute(
            "SELECT * FROM vanguard_diagnostics_latest WHERE active_flag = 1"
        ).fetchall()
    }
    current_keys = {str(item["anomaly_key"]) for item in anomalies}
    transitions: list[dict[str, Any]] = []

    for anomaly in anomalies:
        key = str(anomaly["anomaly_key"])
        existing = active_rows.get(key)
        if existing:
            con.execute(
                """
                UPDATE vanguard_diagnostics_latest
                SET severity = ?,
                    cycle_ts_utc = ?,
                    summary = ?,
                    recommended_action = ?,
                    details_json = ?,
                    last_seen_ts_utc = ?,
                    status = 'ACTIVE',
                    active_flag = 1
                WHERE anomaly_key = ?
                """,
                (
                    anomaly["severity"],
                    anomaly["cycle_ts_utc"],
                    anomaly["summary"],
                    anomaly["recommended_action"],
                    anomaly["details_json"],
                    now_iso,
                    key,
                ),
            )
            continue
        prior = con.execute(
            "SELECT * FROM vanguard_diagnostics_latest WHERE anomaly_key = ?",
            (key,),
        ).fetchone()
        if prior:
            con.execute(
                """
                UPDATE vanguard_diagnostics_latest
                SET family = ?, scope_type = ?, scope_id = ?, severity = ?, status = 'ACTIVE',
                    active_flag = 1, cycle_ts_utc = ?, summary = ?, recommended_action = ?,
                    details_json = ?, last_seen_ts_utc = ?, last_status_change_ts_utc = ?
                WHERE anomaly_key = ?
                """,
                (
                    anomaly["family"],
                    anomaly["scope_type"],
                    anomaly["scope_id"],
                    anomaly["severity"],
                    anomaly["cycle_ts_utc"],
                    anomaly["summary"],
                    anomaly["recommended_action"],
                    anomaly["details_json"],
                    now_iso,
                    now_iso,
                    key,
                ),
            )
        else:
            con.execute(
                """
                INSERT INTO vanguard_diagnostics_latest (
                    anomaly_key, family, scope_type, scope_id, severity, status, active_flag,
                    first_seen_ts_utc, last_seen_ts_utc, last_status_change_ts_utc,
                    cycle_ts_utc, summary, recommended_action, details_json
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    anomaly["family"],
                    anomaly["scope_type"],
                    anomaly["scope_id"],
                    anomaly["severity"],
                    now_iso,
                    now_iso,
                    now_iso,
                    anomaly["cycle_ts_utc"],
                    anomaly["summary"],
                    anomaly["recommended_action"],
                    anomaly["details_json"],
                ),
            )
        con.execute(
            """
            INSERT INTO vanguard_diagnostics_events (
                observed_ts_utc, cycle_ts_utc, anomaly_key, family, scope_type,
                scope_id, severity, status, summary, recommended_action, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                now_iso,
                anomaly["cycle_ts_utc"],
                key,
                anomaly["family"],
                anomaly["scope_type"],
                anomaly["scope_id"],
                anomaly["severity"],
                anomaly["summary"],
                anomaly["recommended_action"],
                anomaly["details_json"],
            ),
        )
        transitions.append({"status": "ACTIVE", **anomaly})

    for key, row in active_rows.items():
        if key in current_keys:
            continue
        con.execute(
            """
            UPDATE vanguard_diagnostics_latest
            SET status = 'RECOVERED',
                active_flag = 0,
                last_seen_ts_utc = ?,
                last_status_change_ts_utc = ?
            WHERE anomaly_key = ?
            """,
            (now_iso, now_iso, key),
        )
        con.execute(
            """
            INSERT INTO vanguard_diagnostics_events (
                observed_ts_utc, cycle_ts_utc, anomaly_key, family, scope_type,
                scope_id, severity, status, summary, recommended_action, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RECOVERED', ?, ?, ?)
            """,
            (
                now_iso,
                row["cycle_ts_utc"],
                key,
                row["family"],
                row["scope_type"],
                row["scope_id"],
                row["severity"],
                row["summary"],
                row["recommended_action"],
                row["details_json"],
            ),
        )
        transitions.append(
            {
                "status": "RECOVERED",
                "anomaly_key": key,
                "family": str(row["family"] or ""),
                "scope_type": str(row["scope_type"] or ""),
                "scope_id": str(row["scope_id"] or ""),
                "severity": str(row["severity"] or ""),
                "cycle_ts_utc": str(row["cycle_ts_utc"] or ""),
                "summary": str(row["summary"] or ""),
                "recommended_action": str(row["recommended_action"] or ""),
                "details_json": str(row["details_json"] or "{}"),
            }
        )

    con.commit()
    return transitions


def refresh_diagnostics(
    con: sqlite3.Connection,
    runtime_cfg: dict[str, Any],
    profile_ids: list[str],
    *,
    manifest_path: Path | str | None = None,
    cycle_ts_utc: str | None = None,
) -> dict[str, Any]:
    ensure_schema(con)
    funnels = collect_cycle_funnels(con, profile_ids, cycle_ts_utc=cycle_ts_utc)
    persist_cycle_funnels(con, funnels)
    account_truth_rows = collect_account_truth(con, profile_ids)
    persist_account_truth(con, account_truth_rows)
    service_verdict_rows = collect_service_verdicts(con, runtime_cfg, manifest_path=manifest_path)
    persist_service_verdicts(con, service_verdict_rows)
    anomalies = detect_anomalies(con, runtime_cfg, profile_ids)
    transitions = persist_anomaly_snapshot(con, anomalies)
    return {
        "cycle_funnels": funnels,
        "account_truth_rows": account_truth_rows,
        "service_verdict_rows": service_verdict_rows,
        "anomalies": anomalies,
        "transitions": transitions,
    }


def fetch_diagnostics_snapshot(
    con: sqlite3.Connection,
    profile_ids: list[str],
    *,
    active_only: bool = True,
    anomaly_limit: int = 50,
) -> dict[str, Any]:
    ensure_schema(con)
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    placeholders = ",".join("?" for _ in clean_profile_ids) if clean_profile_ids else None

    active_sql = "SELECT * FROM vanguard_diagnostics_latest"
    params: list[Any] = []
    if active_only:
        active_sql += " WHERE active_flag = 1"
    if clean_profile_ids:
        clause = (
            "("
            f"(scope_type = 'profile' AND scope_id IN ({placeholders})) "
            f"OR scope_type IN ('service', 'table', 'stream_pair', 'global', 'stage')"
            ")"
        )
        active_sql += (" AND " if "WHERE" in active_sql else " WHERE ") + clause
        params.extend(clean_profile_ids)
    active_sql += " ORDER BY severity DESC, last_seen_ts_utc DESC LIMIT ?"
    params.append(int(anomaly_limit))
    anomalies = [dict(row) for row in con.execute(active_sql, params).fetchall()]

    funnel_rows: list[dict[str, Any]] = []
    if clean_profile_ids:
        funnel_rows = [
            dict(row)
            for row in con.execute(
                f"""
                WITH ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY profile_id
                               ORDER BY cycle_ts_utc DESC
                           ) AS rn
                    FROM vanguard_diagnostics_cycle_funnel
                    WHERE profile_id IN ({placeholders})
                )
                SELECT *
                FROM ranked
                WHERE rn = 1
                ORDER BY profile_id
                """,
                clean_profile_ids,
            ).fetchall()
        ]
        attempt_summary = _latest_execution_attempt_summary(con, clean_profile_ids)
        for row in funnel_rows:
            key = (str(row.get("cycle_ts_utc") or ""), str(row.get("profile_id") or ""))
            summary = attempt_summary.get(key) or {}
            row["execution_attempt_statuses_json"] = json.dumps(summary.get("attempt_statuses") or [])
            row["execution_attempt_reason_codes_json"] = json.dumps(summary.get("reason_codes") or [])
            row["execution_attempt_bridges_json"] = json.dumps(summary.get("execution_bridges") or [])
            row["execution_attempt_symbols_json"] = json.dumps(summary.get("symbols") or [])
    account_truth = [dict(row) for row in _latest_account_truth_rows(con, clean_profile_ids)]
    service_verdicts = [dict(row) for row in _latest_service_verdict_rows(con)]
    recent_events = [
        dict(row)
        for row in con.execute(
            """
            SELECT *
            FROM vanguard_diagnostics_events
            ORDER BY observed_ts_utc DESC, event_id DESC
            LIMIT ?
            """,
            (int(anomaly_limit),),
        ).fetchall()
    ]
    return {
        "active_anomalies": anomalies,
        "slot_funnels": funnel_rows,
        "account_truth": account_truth,
        "service_verdicts": service_verdicts,
        "recent_events": recent_events,
    }


def format_transition_message(transition: dict[str, Any]) -> str:
    status = str(transition.get("status") or "").upper()
    family = str(transition.get("family") or "diagnostics")
    scope_id = str(transition.get("scope_id") or "-")
    severity = str(transition.get("severity") or "").upper()
    summary = str(transition.get("summary") or "").strip()
    recommended_action = str(transition.get("recommended_action") or "").strip()
    cycle_ts = str(transition.get("cycle_ts_utc") or "").strip()
    icon = "🚨" if status == "ACTIVE" else "✅"
    status_label = "NEW" if status == "ACTIVE" else "RECOVERED"
    parts = [
        f"{icon} <b>DIAGNOSTIC {status_label}</b>",
        f"family={family}",
        f"scope={scope_id}",
        f"severity={severity or '-'}",
    ]
    if cycle_ts:
        parts.append(f"cycle={cycle_ts}")
    if summary:
        parts.append(summary)
    if recommended_action:
        parts.append(f"next={recommended_action}")
    return "\n".join(parts)


def render_diagnostics_summary(snapshot: dict[str, Any]) -> str:
    anomalies = snapshot.get("active_anomalies") or []
    slot_funnels = snapshot.get("slot_funnels") or []
    service_verdicts = snapshot.get("service_verdicts") or []
    lines = [f"active_anomalies={len(anomalies)}"]
    if anomalies:
        for row in anomalies[:5]:
            lines.append(
                f"- [{row.get('severity')}] {row.get('family')} {row.get('scope_id')}: {row.get('summary')}"
            )
    if slot_funnels:
        lines.append("slot_funnels:")
        for row in slot_funnels:
            lines.append(
                "  "
                f"{row.get('profile_id')}: pred={row.get('predictions_count')} "
                f"econ_pass={row.get('economics_pass_rows')} "
                f"state(top={row.get('state_top_rank_rows')},dir={row.get('state_direction_rows')},strong={row.get('state_strong_rows')}) "
                f"policy_filtered={row.get('live_policy_filtered_rows')} "
                f"route={row.get('route_candidate_rows')} risk_approved={row.get('portfolio_approved_rows')} exec={row.get('executed_rows')}"
            )
    if service_verdicts:
        lines.append("service_verdicts:")
        for row in service_verdicts:
            if str(row.get("freshness_state") or "") not in {"OK", "LOG_ONLY", "PROCESS_ONLY"}:
                lines.append(
                    f"  {row.get('service_name')}: {row.get('freshness_state')} alive={row.get('process_alive')}"
                )
    return "\n".join(lines)
