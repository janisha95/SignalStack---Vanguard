from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.execution.position_manager import PositionManager
from vanguard.execution.trade_journal import ensure_table as ensure_trade_journal
from vanguard.execution.trade_journal import insert_approval_row, update_filled


def _config() -> dict:
    return {
        "position_manager": {
            "enabled": False,
            "default_policy_by_asset_class": {
                "forex": "forward_return_12bar_forex_v1",
            },
            "policies": {
                "forward_return_12bar_forex_v1": {
                    "asset_class": "forex",
                    "model_family": "forward_return",
                    "model_horizon_bars": 12,
                    "bar_interval_minutes": 5,
                    "hard_close_bars": 12,
                    "extension_disallowed_post_entry_health_codes": [
                        "FATAL_POSITION_MISSING",
                        "BROKER_STATE_INVALID",
                    ],
                }
            },
        }
    }


def _bootstrap_aux_tables(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT NOT NULL,
                source TEXT NOT NULL,
                ticket TEXT NOT NULL,
                broker TEXT,
                account_number TEXT,
                symbol TEXT,
                direction TEXT,
                lots REAL,
                entry_price REAL,
                current_price REAL,
                sl REAL,
                tp REAL,
                floating_pnl REAL,
                swap REAL,
                open_time_utc TEXT,
                holding_minutes REAL,
                received_ts_utc TEXT NOT NULL,
                source_status TEXT NOT NULL,
                source_error TEXT,
                raw_payload_json TEXT,
                PRIMARY KEY (profile_id, source, ticket)
            );
            CREATE TABLE IF NOT EXISTS vanguard_v5_tradeability (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                direction TEXT,
                economics_state TEXT,
                reasons_json TEXT
            );
            CREATE TABLE IF NOT EXISTS vanguard_v5_selection (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                direction TEXT,
                selection_state TEXT,
                selection_rank INTEGER
            );
            CREATE TABLE IF NOT EXISTS vanguard_v6_context_health (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                direction TEXT,
                v6_state TEXT,
                ready_to_route INTEGER,
                reasons_json TEXT
            );
            CREATE TABLE IF NOT EXISTS vanguard_forex_pair_state (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                source_status TEXT
            );
            """
        )
        con.commit()


def _insert_open_trade(db_path: str, approved_cycle_ts: str = "2026-04-12T22:00:00Z") -> str:
    ensure_trade_journal(db_path)
    trade_id = insert_approval_row(
        db_path=db_path,
        profile_id="ftmo_demo_100k",
        candidate={
            "symbol": "EURUSD",
            "asset_class": "forex",
            "side": "LONG",
            "entry_price": 1.1000,
            "stop_price": 1.0990,
            "tp_price": 1.1020,
            "approved_qty": 10,
            "policy_id": "forward_return_12bar_forex_v1",
            "model_id": "lgbm_shap14_forward_return_v1",
            "model_family": "forward_return",
            "original_risk_dollars": 500,
            "original_rr_multiple": 2.0,
            "original_r_multiple_target": 2.0,
        },
        policy_decision={"policy_id": "forward_return_12bar_forex_v1"},
        cycle_ts_utc=approved_cycle_ts,
    )
    update_filled(
        db_path=db_path,
        trade_id=trade_id,
        broker_position_id="12345",
        fill_price=1.1001,
        fill_qty=10,
        filled_at_utc="2026-04-12T22:00:10Z",
        expected_entry=1.1000,
        side="LONG",
    )
    return trade_id


def test_position_manager_holds_when_signals_unavailable(tmp_path):
    db_path = str(tmp_path / "pm.db")
    _bootstrap_aux_tables(db_path)
    _insert_open_trade(db_path)

    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_context_positions_latest (
                profile_id, source, ticket, symbol, direction, lots, entry_price,
                current_price, sl, tp, floating_pnl, open_time_utc, received_ts_utc,
                source_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ftmo_demo_100k", "ftmo_mt5_local", "12345", "EURUSD", "LONG", 10, 1.1001,
                1.1002, 1.0990, 1.1020, 10.0, "2026-04-12T22:00:10Z",
                "2026-04-12T22:05:00Z", "OK",
            ),
        )
        con.commit()

    manager = PositionManager(_config(), db_path)
    reviews = manager.review_profile({"id": "ftmo_demo_100k"}, reviewed_at_utc="2026-04-12T22:05:00Z")

    assert len(reviews) == 1
    assert reviews[0]["review_action"] == "HOLD"
    assert "SIGNAL_UNAVAILABLE" in reviews[0]["review_reason_codes_json"]


def test_position_manager_hard_closes_at_bar_horizon(tmp_path):
    db_path = str(tmp_path / "pm.db")
    _bootstrap_aux_tables(db_path)
    _insert_open_trade(db_path, approved_cycle_ts="2026-04-12T22:00:00Z")

    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_context_positions_latest (
                profile_id, source, ticket, symbol, direction, lots, entry_price,
                current_price, sl, tp, floating_pnl, open_time_utc, received_ts_utc,
                source_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ftmo_demo_100k", "ftmo_mt5_local", "12345", "EURUSD", "LONG", 10, 1.1001,
                1.1015, 1.0990, 1.1020, 35.0, "2026-04-12T22:00:10Z",
                "2026-04-12T23:05:00Z", "OK",
            ),
        )
        con.execute(
            "INSERT INTO vanguard_v5_tradeability VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "LONG", "PASS", "[]"),
        )
        con.execute(
            "INSERT INTO vanguard_v5_tradeability VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "SHORT", "FAIL", "[\"AFTER_COST_NEGATIVE\"]"),
        )
        con.execute(
            "INSERT INTO vanguard_v5_selection VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "LONG", "SELECTED", 1),
        )
        con.execute(
            "INSERT INTO vanguard_v5_selection VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "SHORT", "REJECTED", None),
        )
        con.execute(
            "INSERT INTO vanguard_forex_pair_state VALUES (?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "OK"),
        )
        con.execute(
            "INSERT INTO vanguard_v6_context_health VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-12T23:00:00Z", "ftmo_demo_100k", "EURUSD", "LONG", "READY", 1, "[]"),
        )
        con.commit()

    manager = PositionManager(_config(), db_path)
    reviews = manager.review_profile({"id": "ftmo_demo_100k"}, reviewed_at_utc="2026-04-12T23:05:00Z")

    assert len(reviews) == 1
    assert reviews[0]["review_action"] == "CLOSE"
    assert "EXTENDED_HOLD_60M" in reviews[0]["review_reason_codes_json"]

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT review_action FROM vanguard_position_lifecycle_latest WHERE trade_id = ?",
            (reviews[0]["trade_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "CLOSE"
