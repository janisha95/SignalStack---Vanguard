from __future__ import annotations

import json
import sqlite3

from vanguard.selection.forex_pair_state import build_forex_pair_state


def test_build_forex_pair_state_uses_live_context_and_shared_quotes(tmp_path):
    db_path = tmp_path / "vanguard_universe_pair_state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE vanguard_context_quote_latest (
            profile_id TEXT NOT NULL,
            broker TEXT,
            account_number TEXT,
            symbol TEXT NOT NULL,
            broker_symbol TEXT,
            bid REAL,
            ask REAL,
            mid REAL,
            spread_price REAL,
            spread_pips REAL,
            spread_rt REAL,
            quote_ts_utc TEXT,
            received_ts_utc TEXT NOT NULL,
            source TEXT NOT NULL,
            source_status TEXT NOT NULL,
            source_error TEXT,
            raw_payload_json TEXT,
            PRIMARY KEY (profile_id, source, symbol)
        );
        CREATE TABLE vanguard_context_positions_latest (
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
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_quote_latest
            (profile_id, broker, account_number, symbol, broker_symbol, bid, ask, mid,
             spread_price, spread_pips, spread_rt, quote_ts_utc, received_ts_utc,
             source, source_status, source_error, raw_payload_json)
        VALUES
            ('gft_10k', 'gft', '414578793', 'EURUSD', 'EURUSD.x', 1.1, 1.1002, 1.1001,
             0.0002, 2.0, 0.00036, '2026-04-11T20:00:00Z', '2026-04-11T20:00:01Z',
             'mt5_dwx', 'OK', NULL, '{}')
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_context_positions_latest
            (profile_id, source, ticket, broker, account_number, symbol, direction,
             lots, entry_price, received_ts_utc, source_status, raw_payload_json)
        VALUES
            ('gft_10k', 'mt5_dwx', '1', 'gft', '414578793', 'EURUSD', 'LONG',
             0.1, 1.09, '2026-04-11T20:00:01Z', 'OK', '{}')
        """
    )
    con.commit()
    con.close()

    runtime_config = {
        "profiles": [
            {"id": "gft_10k", "is_active": True, "context_source_id": "gft_mt5_local"},
            {"id": "gft_5k", "is_active": True, "context_source_id": "gft_mt5_local"},
        ],
        "context_sources": {
            "gft_mt5_local": {"source": "mt5_dwx", "broker": "gft"},
        },
        "universes": {
            "gft_universe": {"symbols": {"forex": ["EURUSD"]}},
        },
    }
    cfg = {
        "schema_version": "forex_pair_state_v1",
        "session_order": ["ny"],
        "sessions_utc": {"ny": {"start": "00:00", "end": "24:00", "liquidity_bucket": "HIGH"}},
        "correlation_buckets": {"USD_MAJOR": ["EURUSD"]},
    }

    rows = build_forex_pair_state(
        db_path=db_path,
        runtime_config=runtime_config,
        pair_state_config=cfg,
        cycle_ts_utc="2026-04-11T20:01:00Z",
    )

    assert len(rows) == 2
    by_profile = {row["profile_id"]: row for row in rows}
    assert by_profile["gft_10k"]["source_status"] == "OK"
    assert by_profile["gft_10k"]["spread_pips"] == 2.0
    assert by_profile["gft_10k"]["same_currency_open_count"] == 1
    assert by_profile["gft_10k"]["usd_concentration"] == 0.5
    assert json.loads(by_profile["gft_10k"]["open_exposure_overlap_json"])["same_symbol_open"] is True
    assert by_profile["gft_5k"]["source_status"] == "OK"
    assert by_profile["gft_5k"]["source_error"] == "quote_shared_from_profile=gft_10k"

    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM vanguard_forex_pair_state").fetchone()[0] == 2
    con.close()
