from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.selection.crypto_symbol_state import build_crypto_symbol_state


def test_build_crypto_symbol_state_from_latest_bar(tmp_path: Path) -> None:
    db_path = tmp_path / "vanguard_universe_crypto.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE vanguard_bars_1m (
            symbol TEXT,
            bar_ts_utc TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            asset_class TEXT,
            data_source TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_positions_latest (
            profile_id TEXT,
            source TEXT,
            ticket TEXT,
            symbol TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_bars_1m
        VALUES ('BTCUSD', '2026-04-11T10:00:00Z', 69900, 70100, 69800, 70000, 0, 'crypto', 'twelvedata')
        """
    )
    con.commit()
    con.close()

    rows = build_crypto_symbol_state(
        db_path=db_path,
        runtime_config={
            "profiles": [{"id": "gft_10k", "is_active": True}],
            "universes": {"gft": {"symbols": {"crypto": ["BTCUSD"]}}},
            "crypto_spread_config": {"fallback_spreads_absolute": {"BTCUSD": 80.0}},
            "market_data": {"primary_source_by_asset_class": {"crypto": "twelvedata"}},
        },
        context_config={
            "schema_version": "crypto_context_v1",
            "defaults": {
                "trade_allowed": True,
                "baseline_spread_bps": {"BTCUSD": 11.0},
                "liquidity_buckets": {"high": ["BTCUSD"]},
            },
        },
        cycle_ts_utc="2026-04-11T10:00:00Z",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["source_status"] == "OK"
    assert row["symbol"] == "BTCUSD"
    assert row["live_mid"] == 70000.0
    assert round(row["spread_bps"], 6) == round(80.0 / 70000.0 * 10000, 6)
    assert row["liquidity_bucket"] == "high"

    persisted = sqlite3.connect(db_path).execute(
        "SELECT symbol, source_status FROM vanguard_crypto_symbol_state"
    ).fetchone()
    assert persisted == ("BTCUSD", "OK")


def test_build_crypto_symbol_state_prefers_context_quote(tmp_path: Path) -> None:
    db_path = tmp_path / "vanguard_universe_crypto_context.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT,
            broker TEXT,
            account_number TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            bid REAL,
            ask REAL,
            mid REAL,
            spread_price REAL,
            spread_pips REAL,
            spread_rt REAL,
            quote_ts_utc TEXT,
            received_ts_utc TEXT,
            source TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_symbol_specs (
            broker TEXT,
            profile_id TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            min_lot REAL,
            max_lot REAL,
            lot_step REAL,
            pip_size REAL,
            tick_size REAL,
            tick_value REAL,
            contract_size REAL,
            trade_allowed INTEGER,
            source TEXT,
            updated_ts_utc TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_positions_latest (
            profile_id TEXT,
            source TEXT,
            ticket TEXT,
            symbol TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_quote_latest
        VALUES ('gft_10k', 'gft', '1', 'BTCUSD', 'BTCUSD.x', 69990, 70010, 70000, 20,
                NULL, 0.000571, '2026-04-11T10:00:00Z', '2026-04-11T10:00:01Z',
                'mt5_dwx', 'OK', NULL, '{}')
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_symbol_specs
        VALUES ('gft', 'gft_10k', 'BTCUSD', 'BTCUSD.x', 0.01, 100, 0.01, NULL,
                0.01, NULL, NULL, 1, 'mt5_dwx', '2026-04-11T10:00:01Z', 'PARTIAL', NULL, '{}')
        """
    )
    con.commit()
    con.close()

    rows = build_crypto_symbol_state(
        db_path=db_path,
        runtime_config={
            "profiles": [{"id": "gft_10k", "is_active": True}],
            "universes": {"gft": {"symbols": {"crypto": ["BTCUSD"]}}},
        },
        context_config={
            "schema_version": "crypto_context_v1",
            "defaults": {
                "validation_mode": False,
                "trade_allowed": True,
                "baseline_spread_bps": {"BTCUSD": 11.0},
            },
        },
        cycle_ts_utc="2026-04-11T10:00:05Z",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "mt5_dwx"
    assert row["source_status"] == "OK"
    assert row["live_bid"] == 69990.0
    assert row["live_ask"] == 70010.0
    assert round(row["spread_bps"], 6) == round(20.0 / 70000.0 * 10000, 6)
    assert row["min_qty"] == 0.01
    assert row["qty_step"] == 0.01
    assert row["trade_allowed"] == 1
    assert json.loads(row["asset_state_json"])["context_quote_used"] is True


def test_build_crypto_symbol_state_no_bar_fallback_when_validation_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "vanguard_universe_crypto_no_fallback.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE vanguard_context_positions_latest (
            profile_id TEXT,
            source TEXT,
            ticket TEXT,
            symbol TEXT
        )
        """
    )
    con.commit()
    con.close()

    rows = build_crypto_symbol_state(
        db_path=db_path,
        runtime_config={
            "profiles": [{"id": "gft_10k", "is_active": True}],
            "universes": {"gft": {"symbols": {"crypto": ["BTCUSD"]}}},
            "crypto_spread_config": {"fallback_spreads_absolute": {"BTCUSD": 80.0}},
        },
        context_config={
            "schema_version": "crypto_context_v1",
            "defaults": {"validation_mode": False, "trade_allowed": True},
        },
        cycle_ts_utc="2026-04-11T10:00:00Z",
    )

    assert rows[0]["source_status"] == "MISSING"
    assert rows[0]["source_error"] == "context_quote_missing"
    assert rows[0]["trade_allowed"] == 0


def test_build_crypto_symbol_state_reuses_context_quote_across_profiles(tmp_path: Path) -> None:
    db_path = tmp_path / "vanguard_universe_crypto_profile_fallback.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT,
            broker TEXT,
            account_number TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            bid REAL,
            ask REAL,
            mid REAL,
            spread_price REAL,
            spread_pips REAL,
            spread_rt REAL,
            quote_ts_utc TEXT,
            received_ts_utc TEXT,
            source TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_symbol_specs (
            broker TEXT,
            profile_id TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            min_lot REAL,
            max_lot REAL,
            lot_step REAL,
            pip_size REAL,
            tick_size REAL,
            tick_value REAL,
            contract_size REAL,
            trade_allowed INTEGER,
            source TEXT,
            updated_ts_utc TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_positions_latest (
            profile_id TEXT,
            source TEXT,
            ticket TEXT,
            symbol TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_quote_latest
        VALUES ('gft_10k', 'gft', '1', 'ETHUSD', 'ETHUSD.x', 2220, 2222, 2221, 2,
                NULL, 0.0009, '2026-04-11T10:00:00Z', '2026-04-11T10:00:01Z',
                'mt5_dwx', 'OK', NULL, '{}')
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_symbol_specs
        VALUES ('gft', 'gft_10k', 'ETHUSD', 'ETHUSD.x', 0.01, 100, 0.01, NULL,
                0.01, NULL, NULL, 1, 'mt5_dwx', '2026-04-11T10:00:01Z', 'PARTIAL', NULL, '{}')
        """
    )
    con.commit()
    con.close()

    rows = build_crypto_symbol_state(
        db_path=db_path,
        runtime_config={
            "profiles": [
                {"id": "gft_10k", "is_active": True},
                {"id": "gft_5k", "is_active": True},
            ],
            "universes": {"gft": {"symbols": {"crypto": ["ETHUSD"]}}},
            "market_data": {"primary_source_by_asset_class": {"crypto": "mt5_dwx"}},
        },
        context_config={
            "schema_version": "crypto_context_v1",
            "defaults": {
                "validation_mode": False,
                "trade_allowed": True,
                "source": "mt5_dwx",
            },
        },
        cycle_ts_utc="2026-04-11T10:00:05Z",
    )

    by_profile = {row["profile_id"]: row for row in rows}
    assert by_profile["gft_10k"]["source"] == "mt5_dwx"
    assert by_profile["gft_5k"]["source"] == "mt5_dwx"
    assert by_profile["gft_5k"]["live_mid"] == 2221.0
    assert json.loads(by_profile["gft_5k"]["asset_state_json"])["context_quote_used"] is True


def test_build_crypto_symbol_state_validation_prefers_runtime_source_over_stale_context_quote(tmp_path: Path) -> None:
    db_path = tmp_path / "vanguard_universe_crypto_runtime_source.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT,
            broker TEXT,
            account_number TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            bid REAL,
            ask REAL,
            mid REAL,
            spread_price REAL,
            spread_pips REAL,
            spread_rt REAL,
            quote_ts_utc TEXT,
            received_ts_utc TEXT,
            source TEXT,
            source_status TEXT,
            source_error TEXT,
            raw_payload_json TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_context_positions_latest (
            profile_id TEXT,
            source TEXT,
            ticket TEXT,
            symbol TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_bars_1m (
            symbol TEXT,
            bar_ts_utc TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            asset_class TEXT,
            data_source TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_quote_latest
        VALUES ('gft_10k', 'gft', '1', 'BNBUSD', 'BNBUSD.x', 596.9, 597.5, 597.2, 0.6,
                NULL, 0.001005, '2026-04-12T02:32:43Z', '2026-04-12T02:32:43Z',
                'mt5_dwx', 'OK', NULL, '{}')
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_bars_1m
        VALUES ('BNBUSD', '2026-04-12T06:42:00Z', 597.0, 598.0, 596.5, 597.5, 0, 'crypto', 'twelvedata')
        """
    )
    con.commit()
    con.close()

    rows = build_crypto_symbol_state(
        db_path=db_path,
        runtime_config={
            "profiles": [{"id": "gft_10k", "is_active": True}],
            "universes": {"gft": {"symbols": {"crypto": ["BNBUSD"]}}},
            "market_data": {"primary_source_by_asset_class": {"crypto": "twelvedata"}},
            "crypto_spread_config": {"fallback_spreads_absolute": {"BNBUSD": 0.6}},
        },
        context_config={
            "schema_version": "crypto_context_v1",
            "defaults": {
                "validation_mode": True,
                "trade_allowed": True,
                "baseline_spread_bps": {"BNBUSD": 10.0},
                "emergency_default_spread_bps": 20.0,
            },
        },
        cycle_ts_utc="2026-04-12T06:42:23Z",
    )

    assert len(rows) == 1
    row = rows[0]
    state = json.loads(row["asset_state_json"])
    assert row["source"] == "twelvedata"
    assert row["quote_ts_utc"] == "2026-04-12T06:42:00Z"
    assert row["live_mid"] == 597.5
    assert state["active_source"] == "twelvedata"
    assert state["context_quote_used"] is False
    assert state["bar_source"] == "twelvedata"
