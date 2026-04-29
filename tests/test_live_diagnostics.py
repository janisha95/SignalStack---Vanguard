from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.analytics.live_diagnostics import (
    fetch_diagnostics_snapshot,
    refresh_diagnostics,
)


def _create_table(con: sqlite3.Connection, sql: str) -> None:
    con.execute(sql)


def _seed_base_tables(con: sqlite3.Connection) -> None:
    _create_table(
        con,
        """
        CREATE TABLE vanguard_cycle_audit (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            asset_class TEXT,
            decision_stream_id TEXT,
            execution_policy_id TEXT,
            execution_mode TEXT,
            tradeability_rows INTEGER,
            tradeability_pass_rows INTEGER,
            economics_pass_rows INTEGER,
            economics_fail_rows INTEGER,
            live_policy_filtered_rows INTEGER,
            route_candidate_rows INTEGER,
            selected_rows INTEGER,
            portfolio_approved_rows INTEGER,
            portfolio_rejected_rows INTEGER,
            decision_rows INTEGER,
            live_policy_failed_checks_json TEXT,
            portfolio_reasons_json TEXT,
            decision_counts_json TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_v5_selection (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            symbol TEXT,
            direction TEXT,
            selected INTEGER,
            selection_rank INTEGER,
            tradeability_ref_json TEXT,
            top_rank_streak INTEGER,
            direction_streak INTEGER,
            strong_prediction_streak INTEGER
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_signal_decision_log (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            execution_decision TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_predictions_history (
            cycle_ts_utc TEXT,
            decision_stream_id TEXT,
            symbol TEXT,
            direction TEXT,
            model_id TEXT,
            predicted_return REAL,
            conviction REAL
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_context_account_latest (
            profile_id TEXT,
            account_number TEXT,
            received_ts_utc TEXT,
            source TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT,
            balance REAL,
            equity REAL
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_open_positions (
            profile_id TEXT,
            unrealized_pnl REAL
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_service_state (
            service_name TEXT,
            status TEXT,
            last_started_ts_utc TEXT,
            last_success_ts_utc TEXT,
            last_error_ts_utc TEXT,
            updated_ts_utc TEXT,
            pid INTEGER,
            details_json TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_stage_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_ts_utc TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            status TEXT NOT NULL,
            rows_written INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            duration_seconds REAL NOT NULL,
            observations_json TEXT,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NOT NULL
        )
        """,
    )
    for table in (
        "vanguard_v5_tradeability",
    ):
        _create_table(con, f"CREATE TABLE {table} (cycle_ts_utc TEXT)")
    _create_table(
        con,
        """
        CREATE TABLE vanguard_tradeable_portfolio (
            cycle_ts_utc TEXT,
            account_id TEXT,
            decision_stream_id TEXT,
            symbol TEXT,
            direction TEXT,
            status TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_execution_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_ts_utc TEXT,
            profile_id TEXT,
            decision_stream_id TEXT,
            trade_id TEXT,
            symbol TEXT,
            direction TEXT,
            execution_mode TEXT,
            execution_bridge TEXT,
            attempt_status TEXT,
            reason_code TEXT,
            broker_order_id TEXT,
            broker_position_id TEXT,
            response_json TEXT,
            created_at_utc TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_trade_journal (
            trade_id TEXT,
            profile_id TEXT,
            symbol TEXT,
            side TEXT,
            status TEXT,
            policy_id TEXT
        )
        """,
    )
    _create_table(
        con,
        """
        CREATE TABLE vanguard_position_lifecycle (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT,
            policy_id TEXT,
            reviewed_at_utc TEXT
        )
        """,
    )


def _runtime_cfg() -> dict:
    return {
        "runtime": {"cycle_interval_seconds": 300},
        "profiles": [
            {
                "id": "slot_1",
                "is_active": True,
                "asset_lanes": {
                    "forex": {"enabled": True, "execution_mode": "auto", "decision_stream_id": "stream_1"}
                },
            },
            {
                "id": "slot_2",
                "is_active": True,
                "asset_lanes": {
                    "forex": {"enabled": True, "execution_mode": "auto", "decision_stream_id": "stream_2"}
                },
            },
        ],
    }


def _write_manifest(tmp_path: Path) -> Path:
    log_file = tmp_path / "orchestrator.log"
    log_file.write_text("ok\n", encoding="utf-8")
    manifest = {
        "services": {
            "orchestrator": {
                "pid_file": str(tmp_path / "orchestrator.pid"),
                "log_file": str(log_file),
                "required_for_ready": True,
                "freshness_seconds": 60,
                "expected_write_surface": "service_state:orchestrator",
            },
            "monitor": {
                "pid_file": str(tmp_path / "monitor.pid"),
                "log_file": str(tmp_path / "monitor.log"),
                "required_for_ready": True,
                "freshness_seconds": 60,
            },
        }
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _insert_cycle(con: sqlite3.Connection, cycle_ts: str, profile_id: str, stream_id: str, *, healthy: bool) -> None:
    if healthy:
        live_policy_checks = {}
        portfolio_reasons = {"APPROVED": 2}
        decision_counts = {"EXECUTED": 1}
        vals = (cycle_ts, profile_id, "forex", stream_id, "policy_a", "auto", 40, 20, 20, 20, 2, 4, 4, 2, 2, 4,
                json.dumps(live_policy_checks), json.dumps(portfolio_reasons), json.dumps(decision_counts))
    else:
        live_policy_checks = {"min_top_rank_streak": 40}
        portfolio_reasons = {}
        decision_counts = {"SKIPPED_POLICY": 40}
        vals = (cycle_ts, profile_id, "forex", stream_id, "policy_b", "auto", 40, 20, 20, 20, 40, 0, 0, 0, 0, 40,
                json.dumps(live_policy_checks), json.dumps(portfolio_reasons), json.dumps(decision_counts))
    con.execute(
        """
        INSERT INTO vanguard_cycle_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        vals,
    )


def _insert_selection_rows(
    con: sqlite3.Connection,
    cycle_ts: str,
    profile_id: str,
    *,
    top_rank_count: int,
    direction_count: int,
    strong_count: int,
    total: int = 40,
) -> None:
    rows = []
    for idx in range(total):
        rows.append(
            (
                cycle_ts,
                profile_id,
                f"SYM{idx:02d}",
                "LONG" if idx % 2 == 0 else "SHORT",
                1,
                idx + 1,
                json.dumps(
                    {
                        "decision_stream_id": f"stream_{profile_id}",
                        "binding_id": f"binding_{profile_id}",
                        "risk_policy_id": f"risk_{profile_id}",
                        "execution_policy_id": f"exec_{profile_id}",
                        "lifecycle_policy_id": f"life_{profile_id}",
                    }
                ),
                1 if idx < top_rank_count else 0,
                1 if idx < direction_count else 0,
                1 if idx < strong_count else 0,
            )
        )
    con.executemany("INSERT INTO vanguard_v5_selection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_prediction_rows(
    con: sqlite3.Connection,
    cycle_ts: str,
    stream_id: str,
    model_id: str,
    *,
    identical_offset: float,
) -> None:
    rows = []
    for idx in range(25):
        symbol = f"SYM{idx:02d}"
        direction = "LONG" if idx % 2 == 0 else "SHORT"
        pred = identical_offset + idx * 0.001
        conv = 0.50 + idx * 0.001
        rows.append((cycle_ts, stream_id, symbol, direction, model_id, pred, conv))
    con.executemany("INSERT INTO vanguard_predictions_history VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_stage_cycle_rows(con: sqlite3.Connection, cycle_ts: str, profile_id: str) -> None:
    con.execute("INSERT INTO vanguard_v5_tradeability VALUES (?)", (cycle_ts,))
    con.execute(
        """
        INSERT INTO vanguard_tradeable_portfolio (
            cycle_ts_utc, account_id, decision_stream_id, symbol, direction, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (cycle_ts, profile_id, f"stream_{profile_id}", "SYM00", "LONG", "APPROVED"),
    )
    con.execute(
        "INSERT INTO vanguard_signal_decision_log VALUES (?, ?, ?)",
        (cycle_ts, profile_id, "SKIPPED_POLICY"),
    )


def _insert_stage_run(
    con: sqlite3.Connection,
    cycle_ts: str,
    stage_name: str,
    status: str,
    rows_written: int = 1,
) -> None:
    con.execute(
        """
        INSERT INTO vanguard_stage_runs (
            cycle_ts_utc, stage_name, status, rows_written, reason,
            duration_seconds, observations_json, started_at_utc, completed_at_utc
        ) VALUES (?, ?, ?, ?, NULL, 1.0, '{}', ?, ?)
        """,
        (cycle_ts, stage_name, status, rows_written, cycle_ts, cycle_ts),
    )


def test_refresh_diagnostics_detects_expected_anomalies(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)

    old_cycle = "2026-04-28T00:00:00Z"
    new_cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, old_cycle, "slot_1", "stream_1", healthy=False)
    _insert_cycle(con, new_cycle, "slot_1", "stream_1", healthy=False)
    _insert_cycle(con, new_cycle, "slot_2", "stream_2", healthy=True)

    _insert_selection_rows(con, new_cycle, "slot_1", top_rank_count=0, direction_count=0, strong_count=1)
    _insert_selection_rows(con, new_cycle, "slot_2", top_rank_count=24, direction_count=18, strong_count=22)

    _insert_prediction_rows(con, old_cycle, "stream_1", "model_dead", identical_offset=0.1)
    _insert_prediction_rows(con, new_cycle, "stream_1", "model_dead", identical_offset=0.1)
    _insert_prediction_rows(con, new_cycle, "stream_2", "model_live", identical_offset=0.1)

    con.execute(
        """
        INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "slot_1",
            "111",
            "2026-04-28T00:05:00Z",
            "metaapi",
            "OK",
            "",
            json.dumps({"cached_fallback": True}),
            50000.0,
            50000.0,
        ),
    )
    con.execute("INSERT INTO vanguard_open_positions VALUES (?, ?)", ("slot_1", 125.0))
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "orchestrator",
            "OK",
            "2026-04-28T00:00:00Z",
            "2000-01-01T00:00:00Z",
            None,
            "2026-04-28T00:05:00Z",
            123,
            json.dumps({}),
        ),
    )
    _insert_stage_cycle_rows(con, new_cycle, "slot_1")

    refresh_diagnostics(con, _runtime_cfg(), ["slot_1", "slot_2"], manifest_path=manifest_path, cycle_ts_utc=old_cycle)
    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1", "slot_2"], manifest_path=manifest_path, cycle_ts_utc=new_cycle)
    families = {row["family"] for row in result["anomalies"]}

    assert "state_layer_broken" in families
    assert "policy_choke" in families
    assert "account_truth_degraded" in families
    assert "service_drift" in families
    assert "model_identity_mismatch" in families


def test_refresh_diagnostics_emits_recovery_when_dead_slot_clears(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)

    cycle_a = "2026-04-28T00:00:00Z"
    cycle_b = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle_a, "slot_1", "stream_1", healthy=False)
    _insert_cycle(con, cycle_b, "slot_1", "stream_1", healthy=False)
    _insert_selection_rows(con, cycle_b, "slot_1", top_rank_count=0, direction_count=0, strong_count=1)
    _insert_prediction_rows(con, cycle_a, "stream_1", "model_dead", identical_offset=0.2)
    _insert_prediction_rows(con, cycle_b, "stream_1", "model_dead", identical_offset=0.2)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle_b, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute("INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("orchestrator", "OK", cycle_a, cycle_b, None, cycle_b, 123, json.dumps({})))
    _insert_stage_cycle_rows(con, cycle_b, "slot_1")

    refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle_a)
    first = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle_b)
    assert any(row["family"] == "policy_choke" and row["status"] == "ACTIVE" for row in first["transitions"])

    cycle_c = "2026-04-28T00:10:00Z"
    _insert_cycle(con, cycle_c, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle_c, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle_c, "stream_1", "model_dead", identical_offset=0.3)
    _insert_stage_cycle_rows(con, cycle_c, "slot_1")

    second = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path)
    assert any(row["family"] == "policy_choke" and row["status"] == "RECOVERED" for row in second["transitions"])


def test_fetch_snapshot_keeps_service_anomalies_with_profile_filter(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, "2000-01-01T00:00:00Z", None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")

    refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path)
    snapshot = fetch_diagnostics_snapshot(con, ["slot_1"], active_only=True, anomaly_limit=20)
    families = {row["family"] for row in snapshot["active_anomalies"]}

    assert "service_drift" in families


def test_refresh_diagnostics_detects_stage_contract_drift(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, cycle, None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")
    _insert_stage_run(con, cycle, "selection", "FAILED")
    _insert_stage_run(con, cycle, "risk_filters", "OK")

    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    families = {row["family"] for row in result["anomalies"]}
    assert "stage_contract_drift" in families


def test_refresh_diagnostics_detects_selection_handoff_drift(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    con.execute(
        """
        UPDATE vanguard_v5_selection
        SET tradeability_ref_json = ?
        WHERE profile_id = ? AND selection_rank = 1
        """,
        (json.dumps({"decision_stream_id": "stream_1"}), "slot_1"),
    )
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, cycle, None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")

    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    families = {row["family"] for row in result["anomalies"]}
    assert "selection_handoff_drift" in families


def test_fetch_snapshot_and_classifier_use_risk_approval_language(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, cycle, None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")
    con.execute(
        """
        INSERT INTO vanguard_execution_attempts (
            cycle_ts_utc, profile_id, decision_stream_id, trade_id, symbol, direction,
            execution_mode, execution_bridge, attempt_status, reason_code, broker_order_id,
            broker_position_id, response_json, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cycle,
            "slot_1",
            "stream_slot_1",
            "trade-1",
            "SYM00",
            "LONG",
            "auto",
            "metaapi",
            "SKIPPED_POLICY",
            "POLICY_SHORT_NOT_ALLOWED",
            None,
            None,
            json.dumps({}),
            cycle,
        ),
    )

    refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    snapshot = fetch_diagnostics_snapshot(con, ["slot_1"], active_only=True, anomaly_limit=20)
    row = snapshot["slot_funnels"][0]

    assert row["portfolio_approved_rows"] > 0
    assert "POLICY_SHORT_NOT_ALLOWED" in row["execution_attempt_reason_codes_json"]
    from vanguard.analytics.live_diagnostics import classify_funnel_seam
    verdict = classify_funnel_seam(row)
    assert verdict["reason"] == "execution stopped after risk approval: policy_short_not_allowed"


def test_refresh_diagnostics_detects_recent_cycle_error(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute(
        "INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "orchestrator",
            "ERROR",
            "2026-04-28T00:00:00Z",
            "2026-04-28T00:00:00Z",
            cycle,
            cycle,
            123,
            json.dumps({"last_error": "cannot access free variable '_trade_id'"}),
        ),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")

    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    families = {row["family"] for row in result["anomalies"]}
    assert "cycle_error" in families


def test_refresh_diagnostics_detects_exit_policy_drift(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, cycle, None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")
    con.execute(
        "INSERT INTO vanguard_trade_journal VALUES (?, ?, ?, ?, ?, ?)",
        ("trade_1", "slot_1", "EURUSD", "LONG", "OPEN", "journal_exit"),
    )
    con.execute(
        "INSERT INTO vanguard_position_lifecycle (trade_id, policy_id, reviewed_at_utc) VALUES (?, ?, ?)",
        ("trade_1", "lifecycle_exit", cycle),
    )

    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    families = {row["family"] for row in result["anomalies"]}
    assert "exit_policy_drift" in families


def test_refresh_diagnostics_detects_execution_attempt_drift(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _seed_base_tables(con)
    cycle = "2026-04-28T00:05:00Z"
    _insert_cycle(con, cycle, "slot_1", "stream_1", healthy=True)
    _insert_selection_rows(con, cycle, "slot_1", top_rank_count=20, direction_count=12, strong_count=20)
    _insert_prediction_rows(con, cycle, "stream_1", "model_1", identical_offset=0.1)
    con.execute("INSERT INTO vanguard_context_account_latest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("slot_1", "111", cycle, "metaapi", "OK", "", json.dumps({}), 100.0, 100.0))
    con.execute(
        "INSERT INTO vanguard_service_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("orchestrator", "OK", cycle, cycle, None, cycle, 123, json.dumps({})),
    )
    _insert_stage_cycle_rows(con, cycle, "slot_1")
    result = refresh_diagnostics(con, _runtime_cfg(), ["slot_1"], manifest_path=manifest_path, cycle_ts_utc=cycle)
    families = {row["family"] for row in result["anomalies"]}
    assert "execution_attempt_drift" in families
