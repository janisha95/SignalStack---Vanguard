from __future__ import annotations

import sqlite3
import tempfile

from vanguard.execution.signal_decision_log import ensure_table, insert_decision, update_decision


def _make_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def test_insert_and_update_signal_decision() -> None:
    db_path = _make_db()
    ensure_table(db_path)

    decision_id = insert_decision(
        db_path,
        cycle_ts_utc="2026-04-15T13:10:00Z",
        profile_id="gft_10k",
        symbol="EURUSD",
        direction="LONG",
        asset_class="forex",
        session_bucket="ny",
        entry_price=1.1,
        edge_score=0.55,
        selection_rank=2,
        risk_usd=25.0,
        risk_pct=0.0025,
        lot_size=0.5,
        stop_pips=20.0,
        tp_pips=40.0,
        timeout_policy_minutes=60,
        analysis_dedupe_minutes=120,
        open_positions_count=3,
        max_slots=5,
        free_slots=2,
        execution_decision="EXECUTED",
        decision_reason_code="MANUAL_FORWARD_TRACKED",
        decision_reason_json={"source": "test"},
        incumbent_snapshot_json=[{"symbol": "GBPUSD"}],
    )

    update_decision(
        db_path,
        decision_id,
        trade_id="trade-1",
        broker_position_id="ticket-1",
        execution_decision="EXECUTION_FAILED",
        decision_reason_code="BROKER_REJECTED",
        decision_reason_json={"error": "rejected"},
    )

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT trade_id, broker_position_id, execution_decision, decision_reason_code
            FROM vanguard_signal_decision_log
            WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()

    assert row == ("trade-1", "ticket-1", "EXECUTION_FAILED", "BROKER_REJECTED")

