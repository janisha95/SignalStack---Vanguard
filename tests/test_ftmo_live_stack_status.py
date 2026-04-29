from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.ftmo_live_stack_status import build_status, render_operator_summary


def test_build_status_reports_ready_with_live_pids_and_recent_logs(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    current_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    prior_minute_ts = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_path = tmp_path / "status.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE vanguard_signal_decision_log (profile_id TEXT, cycle_ts_utc TEXT)")
    con.execute(
        "CREATE TABLE vanguard_health (symbol TEXT, cycle_ts_utc TEXT, status TEXT, asset_class TEXT)"
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_account_latest (
            profile_id TEXT,
            account_number TEXT,
            received_ts_utc TEXT,
            balance REAL,
            equity REAL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT,
            symbol TEXT,
            quote_ts_utc TEXT,
            received_ts_utc TEXT,
            source_status TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_service_state (
            service_name TEXT PRIMARY KEY,
            status TEXT,
            last_started_ts_utc TEXT,
            last_success_ts_utc TEXT,
            last_error_ts_utc TEXT,
            updated_ts_utc TEXT,
            pid INTEGER,
            details_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_crypto_symbol_state (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            symbol TEXT,
            source_status TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_open_positions (
            profile_id TEXT,
            broker_position_id TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            entry_price REAL,
            current_sl REAL,
            current_tp REAL,
            opened_at_utc TEXT,
            unrealized_pnl REAL,
            last_synced_at_utc TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO vanguard_signal_decision_log VALUES ('ftmo_demo_100k', ?)",
        (prior_minute_ts,),
    )
    con.execute(
        "INSERT INTO vanguard_health VALUES ('BCHUSD', ?, 'ACTIVE', 'crypto')",
        (prior_minute_ts,),
    )
    con.execute(
        "INSERT INTO vanguard_context_account_latest VALUES ('ftmo_demo_100k', '1513128213', '2026-04-16T17:56:35Z', 94003.07, 94003.07)"
    )
    con.execute(
        "INSERT INTO vanguard_context_account_latest VALUES ('ftmo_demo_100k', '1513137489', ?, 9618.41, 9618.41)",
        (current_ts,),
    )
    con.execute(
        "INSERT INTO vanguard_context_quote_latest VALUES ('ftmo_demo_100k', 'BCHUSD', ?, ?, 'OK')",
        (current_ts, current_ts),
    )
    con.execute(
        "INSERT INTO vanguard_crypto_symbol_state VALUES (?, 'ftmo_demo_100k', 'BCHUSD', 'OK')",
        (current_ts,),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'v1_precompute',
            'ok',
            ?,
            ?,
            NULL,
            ?,
            ?,
            ?
        )
        """,
        (
            prior_minute_ts,
            current_ts,
            current_ts,
            os.getpid(),
            json.dumps({"cycle_ts_utc": current_ts, "v1_result": {"v1_source_health": 12}}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'dwx_reconciliation',
            'warn',
            ?,
            ?,
            NULL,
            ?,
            ?,
            ?
        )
        """,
        (
            prior_minute_ts,
            current_ts,
            current_ts,
            os.getpid(),
            json.dumps({"profile_id": "ftmo_demo_100k", "unresolved_count": 1}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'ttp_playwright_daemon',
            'ok',
            ?,
            ?,
            NULL,
            ?,
            ?,
            ?
        )
        """,
        (
            prior_minute_ts,
            current_ts,
            current_ts,
            os.getpid(),
            json.dumps({"profile_id": "ttp_10k_swing", "account_id": "SEV10K2767", "positions_count": 2}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'ttp_reconciliation',
            'ok',
            ?,
            ?,
            NULL,
            ?,
            ?,
            ?
        )
        """,
        (
            prior_minute_ts,
            current_ts,
            current_ts,
            os.getpid(),
            json.dumps({"profile_id": "ttp_10k_swing", "unresolved_count": 0}),
        ),
    )
    con.commit()
    con.close()

    current_pid = os.getpid()
    orch_pid = tmp_path / "orch.pid"
    lifecycle_pid = tmp_path / "lifecycle.pid"
    monitor_pid = tmp_path / "monitor.pid"
    datasette_pid = tmp_path / "datasette.pid"
    v1_pid = tmp_path / "v1.pid"
    for pid_file in (orch_pid, lifecycle_pid, monitor_pid, datasette_pid, v1_pid):
        pid_file.write_text(str(current_pid))

    orch_log = tmp_path / "orch.log"
    lifecycle_log = tmp_path / "lifecycle.log"
    monitor_log = tmp_path / "monitor.log"
    v1_log = tmp_path / "v1.log"
    for log in (orch_log, lifecycle_log, monitor_log, v1_log):
        log.write_text("ok\n")

    status = build_status(
        profile_id="ftmo_demo_100k",
        db_path=db_path,
        orchestrator_pid_file=orch_pid,
        lifecycle_pid_file=lifecycle_pid,
        monitor_pid_file=monitor_pid,
        datasette_pid_file=datasette_pid,
        v1_precompute_pid_file=v1_pid,
        orchestrator_log=orch_log,
        lifecycle_log=lifecycle_log,
        monitor_log=monitor_log,
        v1_precompute_log=v1_log,
        require_monitor=False,
    )

    assert status.startup_ready is True
    assert status.payload["cycles"]["latest_cycle_ts_utc"] == prior_minute_ts
    assert status.payload["account_context"]["account_number"] == "1513137489"
    assert status.payload["account_context"]["received_ts_utc"] == current_ts
    crypto_summary = status.payload["asset_summaries"]["crypto"]
    assert crypto_summary["latest_quote_ts_utc"] == current_ts
    assert status.payload["v1_precompute"]["status"] == "ok"
    assert status.payload["dwx_reconciliation"]["status"] == "warn"
    assert status.payload["ttp_playwright_daemon"]["status"] == "ok"
    assert status.payload["ttp_reconciliation"]["status"] == "ok"

    summary = render_operator_summary(status)
    assert "stack: HEALTHY" in summary
    assert "services: orchestrator=UP lifecycle=UP monitor=UP datasette=UP v1_precompute=UP" in summary
    assert "open_positions=0" in summary
    assert "crypto: status=fresh" in summary
    assert "quotes=1/" in summary
    assert "quote_ts=" in summary
    assert "state_rows=" in summary
    assert "reconciliation: status=warn" in summary
    assert "unresolved=1" in summary
    assert "ttp_daemon: status=ok" in summary
    assert "account=SEV10K2767" in summary
    assert "ttp_reconciliation: status=ok" in summary


def test_render_operator_summary_treats_between_cycles_as_waiting_not_stale(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    cycle_ts = (now - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    quote_ts = (now - timedelta(seconds=150)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_path = tmp_path / "status_waiting.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE vanguard_signal_decision_log (profile_id TEXT, cycle_ts_utc TEXT)")
    con.execute("CREATE TABLE vanguard_health (symbol TEXT, cycle_ts_utc TEXT, status TEXT, asset_class TEXT)")
    con.execute(
        "CREATE TABLE vanguard_context_account_latest (profile_id TEXT, account_number TEXT, received_ts_utc TEXT, balance REAL, equity REAL)"
    )
    con.execute(
        "CREATE TABLE vanguard_context_quote_latest (profile_id TEXT, symbol TEXT, quote_ts_utc TEXT, received_ts_utc TEXT, source_status TEXT)"
    )
    con.execute(
        "CREATE TABLE vanguard_service_state (service_name TEXT PRIMARY KEY, status TEXT, last_started_ts_utc TEXT, last_success_ts_utc TEXT, last_error_ts_utc TEXT, updated_ts_utc TEXT, pid INTEGER, details_json TEXT)"
    )
    con.execute(
        "CREATE TABLE vanguard_crypto_symbol_state (cycle_ts_utc TEXT, profile_id TEXT, symbol TEXT, source_status TEXT)"
    )
    con.execute(
        """
        CREATE TABLE vanguard_open_positions (
            profile_id TEXT,
            broker_position_id TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            entry_price REAL,
            current_sl REAL,
            current_tp REAL,
            opened_at_utc TEXT,
            unrealized_pnl REAL,
            last_synced_at_utc TEXT
        )
        """
    )
    con.execute("INSERT INTO vanguard_signal_decision_log VALUES ('ftmo_demo_100k', ?)", (cycle_ts,))
    con.execute("INSERT INTO vanguard_health VALUES ('BCHUSD', ?, 'ACTIVE', 'crypto')", (cycle_ts,))
    con.execute(
        "INSERT INTO vanguard_context_account_latest VALUES ('ftmo_demo_100k', '1513137489', ?, 9618.41, 9618.41)",
        (quote_ts,),
    )
    for symbol in ("BCHUSD", "BNBUSD", "ETHUSD", "LTCUSD", "SOLUSD"):
        con.execute(
            "INSERT INTO vanguard_context_quote_latest VALUES ('ftmo_demo_100k', ?, ?, ?, 'OK')",
            (symbol, quote_ts, quote_ts),
        )
        con.execute(
            "INSERT INTO vanguard_crypto_symbol_state VALUES (?, 'ftmo_demo_100k', ?, 'OK')",
            (quote_ts, symbol),
        )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'v1_precompute', 'ok', ?, ?, NULL, ?, ?, ?
        )
        """,
        (
            cycle_ts,
            quote_ts,
            quote_ts,
            os.getpid(),
            json.dumps({"cycle_ts_utc": quote_ts}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'dwx_reconciliation', 'ok', ?, ?, NULL, ?, ?, ?
        )
        """,
        (
            cycle_ts,
            quote_ts,
            quote_ts,
            os.getpid(),
            json.dumps({"profile_id": "ftmo_demo_100k", "unresolved_count": 0}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'ttp_playwright_daemon', 'warn', ?, ?, NULL, ?, ?, ?
        )
        """,
        (
            cycle_ts,
            quote_ts,
            quote_ts,
            os.getpid(),
            json.dumps({"profile_id": "ttp_10k_swing", "account_id": "DEMOSW1794"}),
        ),
    )
    con.execute(
        """
        INSERT INTO vanguard_service_state VALUES (
            'ttp_reconciliation', 'warn', ?, ?, NULL, ?, ?, ?
        )
        """,
        (
            cycle_ts,
            quote_ts,
            quote_ts,
            os.getpid(),
            json.dumps({"profile_id": "ttp_10k_swing", "unresolved_count": 2}),
        ),
    )
    con.commit()
    con.close()

    current_pid = os.getpid()
    orch_pid = tmp_path / "orch.pid"
    lifecycle_pid = tmp_path / "lifecycle.pid"
    monitor_pid = tmp_path / "monitor.pid"
    datasette_pid = tmp_path / "datasette.pid"
    v1_pid = tmp_path / "v1.pid"
    for pid_file in (orch_pid, lifecycle_pid, monitor_pid, datasette_pid, v1_pid):
        pid_file.write_text(str(current_pid))

    orch_log = tmp_path / "orch.log"
    lifecycle_log = tmp_path / "lifecycle.log"
    monitor_log = tmp_path / "monitor.log"
    v1_log = tmp_path / "v1.log"
    for log in (orch_log, lifecycle_log, monitor_log, v1_log):
        log.write_text("ok\n")

    status = build_status(
        profile_id="ftmo_demo_100k",
        db_path=db_path,
        orchestrator_pid_file=orch_pid,
        lifecycle_pid_file=lifecycle_pid,
        monitor_pid_file=monitor_pid,
        datasette_pid_file=datasette_pid,
        v1_precompute_pid_file=v1_pid,
        orchestrator_log=orch_log,
        lifecycle_log=lifecycle_log,
        monitor_log=monitor_log,
        v1_precompute_log=v1_log,
        require_monitor=False,
    )

    summary = render_operator_summary(status)
    assert status.payload["asset_summaries"]["crypto"]["freshness_state"] == "waiting_for_next_cycle"
    assert "stack: HEALTHY" in summary
    assert "crypto: status=waiting_for_next_cycle" in summary
    assert "reconciliation: status=ok" in summary
    assert "ttp_daemon: status=warn" in summary
    assert "ttp_reconciliation: status=warn" in summary
