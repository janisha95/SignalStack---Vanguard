from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone

from stages.vanguard_risk_filters import _build_active_theses, load_accounts
from vanguard.risk.policy_engine import evaluate_candidate


def _make_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _seed_account_profiles(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS account_profiles (
                id TEXT PRIMARY KEY,
                is_active INTEGER DEFAULT 1,
                account_size REAL DEFAULT 10000
            )
            """
        )
        con.executemany(
            "INSERT OR REPLACE INTO account_profiles (id, is_active, account_size) VALUES (?, ?, ?)",
            [
                ("acc_a", 1, 10000.0),
                ("acc_b", 1, 20000.0),
                ("acc_disabled", 0, 5000.0),
            ],
        )
        con.commit()


def _seed_trade_journal(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                broker_position_id TEXT,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
            """
        )
        con.commit()


def test_load_accounts_reads_active_rows_from_temp_db() -> None:
    db_path = _make_db()
    _seed_account_profiles(db_path)

    accounts = load_accounts(db_path=db_path)

    assert [account["id"] for account in accounts] == ["acc_a", "acc_b"]


def test_load_accounts_respects_filter_for_temp_db() -> None:
    db_path = _make_db()
    _seed_account_profiles(db_path)

    accounts = load_accounts(account_filter="acc_b", db_path=db_path)

    assert len(accounts) == 1
    assert accounts[0]["id"] == "acc_b"


def test_build_active_theses_uses_broker_truth_over_stale_open_journal() -> None:
    db_path = _make_db()
    _seed_trade_journal(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp,
                approval_reasoning_json, status, broker_position_id, filled_at_utc, last_synced_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trade-open",
                "acc_fx",
                "EURUSD",
                "forex",
                "LONG",
                "2026-04-14T10:00:00Z",
                1.0,
                1.0890,
                1.0850,
                1.0970,
                "{}",
                "OPEN",
                "broker-123",
                "2026-04-14T10:01:00Z",
                "2026-04-14T10:01:00Z",
            ),
        )
        con.commit()

    theses = _build_active_theses(
        profile_id="acc_fx",
        cycle_dt=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
        db_path=db_path,
        policy={
            "position_limits": {
                "block_same_direction_reentry": True,
                "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                "reentry_cooldown_minutes": 120,
            }
        },
        live_truth={"open_positions": []},
    )

    assert theses == []


def test_build_active_theses_ignores_recent_forward_tracked_rows_without_broker_open_position() -> None:
    db_path = _make_db()
    _seed_trade_journal(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp,
                approval_reasoning_json, status, last_synced_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trade-forward",
                "acc_fx",
                "USDJPY",
                "forex",
                "SHORT",
                "2026-04-14T10:45:00Z",
                1.0,
                154.0,
                154.5,
                153.0,
                "{}",
                "FORWARD_TRACKED",
                "2026-04-14T10:45:00Z",
            ),
        )
        con.commit()

    theses = _build_active_theses(
        profile_id="acc_fx",
        cycle_dt=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
        db_path=db_path,
        policy={
            "position_limits": {
                "block_same_direction_reentry": True,
                "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                "reentry_cooldown_minutes": 120,
            }
        },
        live_truth={"open_positions": []},
    )

    assert theses == []


def test_build_active_theses_uses_live_broker_open_positions() -> None:
    theses = _build_active_theses(
        profile_id="acc_fx",
        cycle_dt=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
        db_path=_make_db(),
        policy={
            "position_limits": {
                "block_same_direction_reentry": True,
                "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                "reentry_cooldown_minutes": 120,
            }
        },
        live_truth={
            "open_positions": [
                {
                    "symbol": "USDJPY",
                    "side": "SHORT",
                    "open_ts_utc": "2026-04-14T11:15:00Z",
                }
            ]
        },
    )

    assert len(theses) == 1
    assert theses[0]["symbol"] == "USDJPY"
    assert theses[0]["side"] == "SHORT"
    assert theses[0]["status"] == "OPEN"


def test_policy_engine_blocks_same_direction_active_thesis() -> None:
    decision = evaluate_candidate(
        candidate={
            "symbol": "EURUSD",
            "direction": "LONG",
            "asset_class": "forex",
            "entry_price": 1.0890,
            "intraday_atr": 0.0015,
            "spread_pips": 0.8,
            "live_account_equity": 10000.0,
            "pip_value_usd_per_standard_lot": 10.0,
        },
        profile={"id": "gft_5k"},
        policy={
            "side_controls": {"allow_long": True, "allow_short": True},
            "position_limits": {
                "block_same_direction_reentry": True,
                "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                "reentry_cooldown_minutes": 120,
                "block_duplicate_symbols": True,
                "max_open_positions": 5,
            },
            "drawdown_rules": {},
            "reject_rules": {
                "enforce_drawdown_pause": False,
                "enforce_blackout": False,
                "enforce_duplicate_block": True,
                "enforce_position_limit": True,
                "enforce_streak_minimum": False,
            },
            "sizing_by_asset_class": {
                "forex": {
                    "method": "risk_per_stop_pips",
                    "risk_per_trade_pct": 0.005,
                    "min_sl_pips": 10,
                    "min_tp_pips": 20,
                    "contract_size": 100000.0,
                    "pip_value_usd_per_standard_lot": {"default": 10.0},
                }
            },
        },
        overrides=None,
        blackouts=[],
        account_state={
            "equity": 10000.0,
            "open_positions": [],
            "active_theses": [
                {
                    "symbol": "EURUSD",
                    "side": "LONG",
                    "status": "FORWARD_TRACKED",
                    "source": "journal",
                    "age_minutes": 15.0,
                }
            ],
        },
        cycle_ts_utc=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
    )

    assert decision.decision == "BLOCKED_ACTIVE_THESIS_REENTRY"
    assert decision.reject_reason == "BLOCKED_ACTIVE_THESIS_REENTRY"
