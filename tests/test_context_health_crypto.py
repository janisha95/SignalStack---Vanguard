from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.context_health import evaluate_context_health
from vanguard.helpers.clock import iso_utc, now_utc


def test_crypto_validation_health_holds_and_blocks_execution() -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE vanguard_crypto_symbol_state (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            symbol TEXT,
            asset_class TEXT,
            quote_ts_utc TEXT,
            source TEXT,
            live_mid REAL,
            spread_bps REAL,
            source_status TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_source_health (
            data_source TEXT,
            asset_class TEXT,
            status TEXT,
            last_updated TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_crypto_symbol_state
        VALUES ('2026-04-11T10:00:00Z', 'gft_10k', 'BTCUSD', 'crypto',
                '2026-04-11T10:00:00Z', 'twelvedata', 70000, 12.0, 'OK')
        """
    )
    con.execute("INSERT INTO vanguard_source_health VALUES ('twelvedata', 'crypto', 'live', '2026-04-11T10:00:00Z')")

    health = evaluate_context_health(
        con,
        config={
            "v6_health": {
                "enabled": True,
                "crypto_validation": {
                    "enabled": True,
                    "block_execution": True,
                    "quote_max_age_ms": 999999999,
                },
            },
        },
        profile={"id": "gft_10k", "context_health_mode": "auto"},
        candidate={"symbol": "BTCUSD", "direction": "LONG", "asset_class": "crypto"},
        cycle_ts_utc="2026-04-11T10:00:00Z",
    )

    assert health["v6_state"] == "HOLD"
    assert health["ready_to_route"] == 0
    codes = {reason["code"] for reason in health["reasons"]}
    assert "CRYPTO_VALIDATION_MODE_EXECUTION_DISABLED" in codes


def test_crypto_health_uses_generic_context_requirements_when_validation_disabled() -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    now = iso_utc(now_utc())
    con.execute(
        """
        CREATE TABLE vanguard_context_ingest_runs (
            ingest_run_id TEXT,
            profile_id TEXT,
            source TEXT,
            finished_ts_utc TEXT,
            positions_written INTEGER,
            orders_written INTEGER,
            source_status TEXT,
            source_error TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT,
            source TEXT,
            symbol TEXT,
            bid REAL,
            ask REAL,
            received_ts_utc TEXT,
            source_status TEXT,
            source_error TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_account_latest (
            profile_id TEXT,
            source TEXT,
            account_number TEXT,
            balance REAL,
            equity REAL,
            free_margin REAL,
            received_ts_utc TEXT,
            source_status TEXT,
            source_error TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_symbol_specs (
            broker TEXT,
            profile_id TEXT,
            symbol TEXT,
            trade_allowed INTEGER,
            updated_ts_utc TEXT
        )
        """
    )
    con.execute("INSERT INTO vanguard_context_ingest_runs VALUES ('r1', 'gft_10k', 'mt5_dwx', ?, 0, 0, 'OK', NULL)", (now,))
    con.execute("INSERT INTO vanguard_context_quote_latest VALUES ('gft_10k', 'mt5_dwx', 'BTCUSD', 69990, 70010, ?, 'OK', NULL)", (now,))
    con.execute("INSERT INTO vanguard_context_account_latest VALUES ('gft_10k', 'mt5_dwx', '414578793', 10000, 10000, 10000, ?, 'OK', NULL)", (now,))
    con.execute("INSERT INTO vanguard_context_symbol_specs VALUES ('gft', 'gft_10k', 'BTCUSD', 1, ?)", (now,))

    health = evaluate_context_health(
        con,
        config={
            "context_sources": {
                "gft_mt5_local": {
                    "enabled": True,
                    "source": "mt5_dwx",
                    "broker": "gft",
                    "profiles": {"gft_10k": {"account_number": "414578793"}},
                }
            },
            "v6_health": {
                "enabled": True,
                "crypto_validation": {"enabled": False},
                "mode_requirements": {
                    "auto": {
                        "required_context": ["quote", "account", "positions", "orders", "symbol_specs", "daemon"],
                        "ready_state_allows_execution": True,
                    }
                },
                "thresholds": {
                    "quote_max_age_ms": 999999999,
                    "account_max_age_ms": 999999999,
                    "positions_max_age_ms": 999999999,
                    "orders_max_age_ms": 999999999,
                    "ingest_run_max_age_ms": 999999999,
                    "symbol_specs_max_age_minutes": 999999,
                },
            },
        },
        profile={"id": "gft_10k", "context_source_id": "gft_mt5_local", "context_health_mode": "auto"},
        candidate={"symbol": "BTCUSD", "direction": "LONG", "asset_class": "crypto"},
        cycle_ts_utc=now,
    )

    assert health["v6_state"] == "READY"
    assert health["ready_to_route"] == 1
    assert health["reasons"] == []


def test_crypto_validation_health_uses_runtime_resolved_source_when_state_source_missing() -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE vanguard_crypto_symbol_state (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            symbol TEXT,
            asset_class TEXT,
            quote_ts_utc TEXT,
            source TEXT,
            live_mid REAL,
            spread_bps REAL,
            source_status TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_source_health (
            data_source TEXT,
            asset_class TEXT,
            status TEXT,
            last_updated TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_crypto_symbol_state
        VALUES ('2026-04-11T10:00:00Z', 'gft_10k', 'BTCUSD', 'crypto',
                '2026-04-11T10:00:00Z', NULL, 70000, 12.0, 'OK')
        """
    )
    con.execute("INSERT INTO vanguard_source_health VALUES ('mt5_dwx', 'crypto', 'live', '2026-04-11T10:00:00Z')")

    health = evaluate_context_health(
        con,
        config={
            "market_data": {
                "primary_source_by_asset_class": {"crypto": "mt5_local"},
                "sources": {"crypto": ["twelvedata"]},
            },
            "data_sources": {
                "mt5_local": {"enabled": True},
                "twelvedata": {"enabled": True},
            },
            "v6_health": {
                "enabled": True,
                "crypto_validation": {
                    "enabled": True,
                    "block_execution": True,
                    "quote_max_age_ms": 999999999,
                },
            },
        },
        profile={"id": "gft_10k", "context_health_mode": "auto"},
        candidate={"symbol": "BTCUSD", "direction": "LONG", "asset_class": "crypto"},
        cycle_ts_utc="2026-04-11T10:00:00Z",
    )

    snapshot = health["source_snapshot"]
    assert snapshot["crypto_source_health"]["data_source"] == "mt5_dwx"
