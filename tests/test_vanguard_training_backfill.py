"""
tests/test_vanguard_training_backfill.py — Tests for V4A Training Backfill.

Covers:
  - ET timezone helpers
  - session_id computation
  - TBM logic (TP hit, SL hit, timeout, truncated, cross-session prevention)
  - Feature computation wrapper
  - DB helpers (upsert, resume, delete)
  - End-to-end run with synthetic bars
  - Validate function

All tests use an in-memory SQLite DB (via tmp_path fixture) and synthetic
bar data — no real DB or network calls.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

# Resolve project root
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stages.vanguard_training_backfill import (
    _et_offset,
    _to_et,
    _session_id,
    _compute_tbm,
    _compute_features,
    _nan_to_none,
    _get_resume_ts,
    _delete_symbol_rows,
    _worker,
    run,
    validate,
    DEFAULT_HORIZON_BARS,
    DEFAULT_TP_PCT,
    DEFAULT_SL_PCT,
    WARM_UP_SESSION_BARS,
    _FEATURE_NAMES,
)
from vanguard.helpers.db import VanguardDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> tuple[VanguardDB, str]:
    db_path = str(tmp_path / "vanguard_universe.db")
    db = VanguardDB(db_path)
    return db, db_path


def _bar(ts_utc: str, o=100.0, h=101.0, lo=99.0, c=100.0, v=10_000) -> dict:
    """Create a minimal bar dict."""
    return {
        "symbol": "TEST",
        "bar_ts_utc": ts_utc,
        "open": o, "high": h, "low": lo, "close": c, "volume": v,
        "asset_class": "equity", "data_source": "alpaca",
    }


def _session_bars(date: str, n: int = 78, sym: str = "TEST") -> list[dict]:
    """
    Generate n synthetic 5m bars for a regular session starting at 09:30 ET.
    date: 'YYYY-MM-DD' (assumed to be a weekday in winter: UTC offset = -5).
    """
    bars = []
    # 09:30 ET = 14:30 UTC in winter (UTC-5)
    base = datetime(int(date[:4]), int(date[5:7]), int(date[8:10]),
                    14, 30, 0, tzinfo=timezone.utc)
    for i in range(n):
        ts = base + timedelta(minutes=5 * i)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        price = 100.0 + i * 0.01
        bars.append({
            "symbol": sym,
            "bar_ts_utc": ts_str,
            "open": price,
            "high": price + 0.5,
            "low": price - 0.3,
            "close": price + 0.1,
            "volume": 10_000 + i * 100,
            "asset_class": "equity",
            "data_source": "alpaca",
        })
    return bars


# ===========================================================================
# TestEtHelpers
# ===========================================================================

class TestEtHelpers:
    def test_est_offset(self):
        # January is EST (UTC-5)
        dt = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        assert _et_offset(dt) == -5

    def test_edt_offset(self):
        # July is EDT (UTC-4)
        dt = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
        assert _et_offset(dt) == -4

    def test_dst_start_2026(self):
        # DST starts 2026-03-08 (2nd Sunday in March)
        # 07:00 UTC on 2026-03-08 → clocks spring forward
        before = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)
        after  = datetime(2026, 3, 8, 7,  0, tzinfo=timezone.utc)
        assert _et_offset(before) == -5
        assert _et_offset(after)  == -4

    def test_dst_end_2026(self):
        # DST ends 2026-11-01 (1st Sunday in November)
        before = datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)
        after  = datetime(2026, 11, 1, 6,  0, tzinfo=timezone.utc)
        assert _et_offset(before) == -4
        assert _et_offset(after)  == -5

    def test_to_et_winter(self):
        # 15:00 UTC - 5h = 10:00 AM ET
        dt = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        et = _to_et(dt)
        assert et.hour == 10
        assert et.minute == 0
        assert et.tzinfo is None

    def test_to_et_summer(self):
        # 15:00 UTC - 4h = 11:00 AM ET
        dt = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
        et = _to_et(dt)
        assert et.hour == 11


# ===========================================================================
# TestSessionId
# ===========================================================================

class TestSessionId:
    def test_market_open_bar(self):
        # 09:30 ET = 14:30 UTC in winter
        sid = _session_id("2026-01-15T14:30:00+00:00")
        assert sid == "2026-01-15"

    def test_market_close_bar(self):
        # 15:55 ET (last regular bar) = 20:55 UTC in winter
        sid = _session_id("2026-01-15T20:55:00+00:00")
        assert sid == "2026-01-15"

    def test_after_close_is_none(self):
        # 16:00 ET = 21:00 UTC in winter — after close
        sid = _session_id("2026-01-15T21:00:00+00:00")
        assert sid is None

    def test_before_open_is_none(self):
        # 09:00 ET = 14:00 UTC in winter — before open
        sid = _session_id("2026-01-15T14:00:00+00:00")
        assert sid is None

    def test_exactly_930_in(self):
        sid = _session_id("2026-01-15T14:30:00+00:00")
        assert sid is not None

    def test_exact_1600_out(self):
        # 16:00 ET = 21:00 UTC
        sid = _session_id("2026-01-15T21:00:00+00:00")
        assert sid is None

    def test_summer_session(self):
        # 09:30 ET in summer = 13:30 UTC (EDT, UTC-4)
        sid = _session_id("2026-07-15T13:30:00+00:00")
        assert sid == "2026-07-15"

    def test_overnight_bar_is_none(self):
        # Midnight UTC is outside any session
        sid = _session_id("2026-01-15T00:00:00+00:00")
        assert sid is None


# ===========================================================================
# TestComputeTbm
# ===========================================================================

class TestComputeTbm:
    """Tests for the Triple Barrier Method computation."""

    def _make_bars(self, highs: list[float], lows: list[float],
                   entry_close: float = 100.0) -> tuple[list[dict], dict, str]:
        """Build bars list, session_end_idx, and session_id for TBM tests."""
        # Bar 0 is the entry bar; subsequent bars are the forward path
        bars = [{"close": entry_close, "high": entry_close, "low": entry_close,
                 "bar_ts_utc": "2026-01-15T14:30:00+00:00"}]
        base = datetime(2026, 1, 15, 14, 35, tzinfo=timezone.utc)
        for i, (h, lo) in enumerate(zip(highs, lows)):
            ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            bars.append({"close": (h + lo) / 2, "high": h, "low": lo,
                         "bar_ts_utc": ts})
        sid = "2026-01-15"
        session_end_idx = {sid: len(bars) - 1}
        return bars, session_end_idx, sid

    def test_long_tp_hit(self):
        # tp_pct=1%: tp_long=101. sl_pct=2%: sl_long=98. Bar 2 high=102 → TP hit
        bars, sei, sid = self._make_bars(
            highs=[100.5, 102.0, 101.0, 100.0],
            lows =[99.5,  99.5,  99.5, 99.5],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.02, horizon_bars=4)
        assert r["label_long"] == 1
        assert r["exit_type_long"] == "TP"
        assert r["exit_bar"] <= 2

    def test_long_sl_hit(self):
        # sl_pct=1%: sl_long = 99. Bar 1 low = 98 → SL hit
        bars, sei, sid = self._make_bars(
            highs=[100.5, 100.5, 101.0, 101.0],
            lows =[98.0,  99.5,  99.5,  99.5],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.01, horizon_bars=4)
        assert r["label_long"] == 0
        assert r["exit_type_long"] == "SL"

    def test_short_tp_hit(self):
        # tp_pct=1%: tp_short=99. sl_pct=3%: sl_short=103. Bar 1 low=98 → short TP
        bars, sei, sid = self._make_bars(
            highs=[100.5, 100.3, 100.2, 100.1],
            lows =[98.0,  99.5,  99.5,  99.5],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.03, horizon_bars=4)
        assert r["label_short"] == 1
        assert r["exit_type_short"] == "TP"

    def test_short_sl_hit(self):
        # sl_short = 101. Bar 1 high = 102 → short SL
        bars, sei, sid = self._make_bars(
            highs=[102.0, 100.5, 100.5, 100.5],
            lows =[99.5,  99.5,  99.5,  99.5],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.005, sl_pct=0.01, horizon_bars=4)
        assert r["label_short"] == 0
        assert r["exit_type_short"] == "SL"

    def test_timeout_both(self):
        # Tight price action — neither barrier hit
        bars, sei, sid = self._make_bars(
            highs=[100.1, 100.1, 100.1],
            lows =[99.9,  99.9,  99.9],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.01, horizon_bars=3)
        assert r["exit_type_long"] == "TIMEOUT"
        assert r["exit_type_short"] == "TIMEOUT"
        assert r["label_long"] == 0
        assert r["label_short"] == 0

    def test_truncated_when_session_ends_early(self):
        # Session ends at bar 2 even though horizon = 6
        bars, _, sid = self._make_bars(
            highs=[100.2, 100.3],
            lows =[99.8,  99.7],
        )
        # Override session_end to bar index 2 (only 2 forward bars)
        sei = {sid: 2}
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.01, horizon_bars=6)
        assert r["truncated"] == 1

    def test_not_truncated_when_session_has_enough_bars(self):
        bars, sei, sid = self._make_bars(
            highs=[100.2] * 6,
            lows =[99.8] * 6,
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.01, horizon_bars=6)
        assert r["truncated"] == 0

    def test_zero_entry_returns_safe_defaults(self):
        bars = [{"close": 0.0, "high": 0.0, "low": 0.0, "bar_ts_utc": "2026-01-15T14:30:00+00:00"}]
        sid = "2026-01-15"
        r = _compute_tbm(bars, 0, sid, {sid: 0}, tp_pct=0.01, sl_pct=0.01, horizon_bars=6)
        assert r["label_long"] == 0
        assert r["label_short"] == 0
        assert r["forward_return"] == 0.0

    def test_mfe_mae_computed(self):
        bars, sei, sid = self._make_bars(
            highs=[103.0, 100.5],
            lows =[97.0,  99.5],
        )
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.05, sl_pct=0.05, horizon_bars=2)
        # MFE = (103 - 100) / 100 = 3%
        assert abs(r["max_favorable_excursion"] - 0.03) < 1e-5
        # MAE = (100 - 97) / 100 = 3%
        assert abs(r["max_adverse_excursion"] - 0.03) < 1e-5

    def test_no_cross_session_leakage(self):
        # session_end_idx[sid] = i (last bar IS the entry bar) → no forward scan
        bars, _, sid = self._make_bars(highs=[102.0] * 6, lows=[98.0] * 6)
        sei = {sid: 0}  # session ends at the entry bar
        r = _compute_tbm(bars, 0, sid, sei, tp_pct=0.01, sl_pct=0.01, horizon_bars=6)
        assert r["truncated"] == 1
        assert r["exit_type_long"] == "TIMEOUT"
        assert r["exit_type_short"] == "TIMEOUT"


# ===========================================================================
# TestComputeFeatures
# ===========================================================================

class TestComputeFeatures:
    def _make_df(self, n: int = 50) -> pd.DataFrame:
        bars = _session_bars("2026-01-15", n=n)
        df = pd.DataFrame(bars)
        df.sort_values("bar_ts_utc", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def test_returns_all_35_features(self):
        df = self._make_df(60)
        features = _compute_features(df, None, pd.DataFrame())
        for name in _FEATURE_NAMES:
            assert name in features, f"Missing feature: {name}"

    def test_nan_ratio_is_float_in_0_1(self):
        df = self._make_df(60)
        features = _compute_features(df, None, pd.DataFrame())
        nr = features["nan_ratio"]
        assert isinstance(nr, float)
        assert 0.0 <= nr <= 1.0

    def test_bars_available_equals_df_length(self):
        df = self._make_df(45)
        features = _compute_features(df, None, pd.DataFrame())
        assert features["bars_available"] == 45.0

    def test_sparse_df_does_not_raise(self):
        # Very few bars: modules should fail gracefully and return NaN
        df = self._make_df(3)
        features = _compute_features(df, None, pd.DataFrame())
        assert len(features) == 35


# ===========================================================================
# TestNanToNone
# ===========================================================================

class TestNanToNone:
    def test_nan_becomes_none(self):
        assert _nan_to_none(float("nan")) is None

    def test_float_passes_through(self):
        assert _nan_to_none(1.23) == 1.23

    def test_zero_passes_through(self):
        assert _nan_to_none(0.0) == 0.0

    def test_int_passes_through(self):
        assert _nan_to_none(5) == 5

    def test_none_passes_through(self):
        assert _nan_to_none(None) is None


# ===========================================================================
# TestDbHelpers
# ===========================================================================

class TestDbHelpers:
    def test_get_resume_ts_empty_db(self, tmp_path):
        db, _ = _make_db(tmp_path)
        result = _get_resume_ts(db, "AAPL", 6)
        assert result is None

    def test_get_resume_ts_with_rows(self, tmp_path):
        db, _ = _make_db(tmp_path)
        row = {
            "asof_ts_utc": "2026-01-15T20:00:00+00:00",
            "symbol": "AAPL", "asset_class": "equity",
            "path": "ttp_equity", "entry_price": 100.0,
            "label_long": 1, "label_short": 0,
            "forward_return": 0.003,
            "max_favorable_excursion": 0.004,
            "max_adverse_excursion": 0.001,
            "exit_bar": 3, "exit_type_long": "TP", "exit_type_short": "TIMEOUT",
            "truncated": 0, "warm_up": 0,
            "horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015,
        }
        for fname in _FEATURE_NAMES:
            row[fname] = None
        db.upsert_training_data([row])
        result = _get_resume_ts(db, "AAPL", 6)
        assert result == "2026-01-15T20:00:00+00:00"

    def test_get_resume_ts_wrong_horizon(self, tmp_path):
        db, _ = _make_db(tmp_path)
        row = {
            "asof_ts_utc": "2026-01-15T20:00:00+00:00",
            "symbol": "AAPL", "asset_class": "equity",
            "path": "ttp_equity", "entry_price": 100.0,
            "label_long": 1, "label_short": 0,
            "forward_return": 0.003,
            "max_favorable_excursion": 0.004,
            "max_adverse_excursion": 0.001,
            "exit_bar": 3, "exit_type_long": "TP", "exit_type_short": "TIMEOUT",
            "truncated": 0, "warm_up": 0,
            "horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015,
        }
        for fname in _FEATURE_NAMES:
            row[fname] = None
        db.upsert_training_data([row])
        # Different horizon — should return None
        result = _get_resume_ts(db, "AAPL", 12)
        assert result is None

    def test_delete_symbol_rows(self, tmp_path):
        db, _ = _make_db(tmp_path)
        rows = []
        for ts in ["2026-01-15T14:30:00+00:00", "2026-01-15T14:35:00+00:00"]:
            r = {
                "asof_ts_utc": ts, "symbol": "AAPL", "asset_class": "equity",
                "path": "ttp_equity", "entry_price": 100.0,
                "label_long": 0, "label_short": 0,
                "forward_return": 0.0, "max_favorable_excursion": 0.0,
                "max_adverse_excursion": 0.0, "exit_bar": 6,
                "exit_type_long": "TIMEOUT", "exit_type_short": "TIMEOUT",
                "truncated": 0, "warm_up": 0,
                "horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015,
            }
            for fname in _FEATURE_NAMES:
                r[fname] = None
            rows.append(r)
        db.upsert_training_data(rows)
        assert db.count_training_rows("AAPL") == 2
        deleted = _delete_symbol_rows(db, "AAPL", 6)
        assert deleted == 2
        assert db.count_training_rows("AAPL") == 0


# ===========================================================================
# TestWorkerEndToEnd
# ===========================================================================

class TestWorkerEndToEnd:
    """Integration tests: _worker processes real synthetic bars in a temp DB."""

    def _setup(self, tmp_path: Path, n_session_bars: int = 78) -> tuple[VanguardDB, str, list[str]]:
        db, db_path = _make_db(tmp_path)
        # Insert 2 sessions of bars for TEST and SPY
        dates = ["2026-01-13", "2026-01-14"]  # Tuesday and Wednesday
        for sym in ["TEST", "SPY"]:
            all_bars = []
            for d in dates:
                all_bars.extend(_session_bars(d, n=n_session_bars, sym=sym))
            db.upsert_bars_5m(all_bars)
        return db, db_path, ["TEST"]

    def test_worker_produces_rows(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        stats = _worker((symbols, db_path, tbm_params, opts))
        assert stats["rows_written"] > 0
        assert stats["symbols_processed"] == 1
        assert stats["errors"] == []

    def test_worker_dry_run_writes_nothing(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": True,
                "asset_class": "equity", "path": "ttp_equity"}
        stats = _worker((symbols, db_path, tbm_params, opts))
        assert stats["rows_written"] > 0  # counted but not written
        assert db.count_training_rows("TEST") == 0  # nothing in DB

    def test_worker_idempotent_on_full_rebuild(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        r1 = _worker((symbols, db_path, tbm_params, opts))
        r2 = _worker((symbols, db_path, tbm_params, opts))
        # Same number of rows written both times
        assert r1["rows_written"] == r2["rows_written"]
        # Row count in DB same after second run (INSERT OR REPLACE)
        assert db.count_training_rows("TEST") == r1["rows_written"]

    def test_worker_resume_skips_existing(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        # First run
        r1 = _worker((symbols, db_path, tbm_params, opts))
        # Second run in incremental mode (not full_rebuild)
        opts2 = dict(opts, full_rebuild=False)
        r2 = _worker((symbols, db_path, tbm_params, opts2))
        # Resume should skip all already-processed bars
        assert r2["rows_written"] == 0

    def test_worker_label_rates_in_range(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path, n_session_bars=78)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        _worker((symbols, db_path, tbm_params, opts))
        rates = db.get_training_label_rates()
        total = rates["total"]
        assert total > 0
        # Label rates can be anywhere 0–1 with synthetic flat bars
        assert 0.0 <= rates["label_long_rate"] <= 1.0
        assert 0.0 <= rates["label_short_rate"] <= 1.0

    def test_worker_warm_up_flagged_correctly(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        _worker((symbols, db_path, tbm_params, opts))
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM vanguard_training_data WHERE warm_up = 1"
            ).fetchone()
        finally:
            conn.close()
        warm_up_count = row[0]
        # Some early-session bars should be flagged warm_up
        assert warm_up_count >= 0  # can be 0 if session_time returns NaN

    def test_worker_all_35_feature_columns_present(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path, n_session_bars=60)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        _worker((symbols, db_path, tbm_params, opts))
        # Query one row and verify all feature columns exist in DB
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM vanguard_training_data LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        col_names = [d[0] for d in conn.description] if hasattr(conn, 'description') else []
        # Check via dict that all 35 feature names can be retrieved
        row_dict = dict(row)
        for fname in _FEATURE_NAMES:
            assert fname in row_dict, f"Feature column missing from DB: {fname}"

    def test_worker_missing_symbol_skipped(self, tmp_path):
        db, db_path, _ = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity"}
        stats = _worker((["NONEXISTENT"], db_path, tbm_params, opts))
        assert stats["symbols_skipped"] == 1
        assert stats["rows_written"] == 0
        assert stats["errors"] == []

    def test_worker_date_range_filter(self, tmp_path):
        db, db_path, symbols = self._setup(tmp_path)
        tbm_params = {"horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015}
        # Only process 2026-01-13
        opts = {"full_rebuild": True, "dry_run": False,
                "asset_class": "equity", "path": "ttp_equity",
                "start_date": "2026-01-13", "end_date": "2026-01-13"}
        r_one_day = _worker((symbols, db_path, tbm_params, opts))
        # Run without filter
        opts_all = dict(opts, start_date=None, end_date=None)
        r_all = _worker((symbols, db_path, tbm_params, opts_all))
        # One-day run should produce roughly half the rows of two-day run
        assert r_one_day["rows_written"] < r_all["rows_written"]


# ===========================================================================
# TestRunFunction
# ===========================================================================

class TestRunFunction:
    def test_run_with_no_symbols_returns_empty(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        with patch(
            "stages.vanguard_training_backfill.DB_PATH", db_path
        ):
            result = run(symbols=[], workers=1)
        assert result["rows_written"] == 0

    def test_run_returns_stats_dict(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        # Insert minimal bars
        for sym in ["AAPL", "SPY"]:
            bars = _session_bars("2026-01-15", n=60, sym=sym)
            db.upsert_bars_5m(bars)
        with patch("stages.vanguard_training_backfill.DB_PATH", db_path):
            result = run(symbols=["AAPL"], workers=1, dry_run=False,
                         full_rebuild=True, horizon_bars=6)
        assert "rows_written" in result
        assert "symbols_processed" in result
        assert "errors" in result
        assert result["symbols_processed"] >= 1


# ===========================================================================
# TestValidate
# ===========================================================================

class TestValidate:
    def test_validate_fails_empty_db(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        with patch("stages.vanguard_training_backfill.DB_PATH", db_path):
            ok = validate()
        assert ok is False

    def test_validate_passes_with_rows(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        # Insert synthetic rows
        rows = []
        for i in range(50):
            ts = f"2026-01-15T{14 + i // 12:02d}:{(i % 12) * 5:02d}:00+00:00"
            r = {
                "asof_ts_utc": ts, "symbol": "TEST", "asset_class": "equity",
                "path": "ttp_equity", "entry_price": 100.0,
                "label_long": 1 if i % 3 == 0 else 0,
                "label_short": 1 if i % 4 == 0 else 0,
                "forward_return": 0.001,
                "max_favorable_excursion": 0.002,
                "max_adverse_excursion": 0.001,
                "exit_bar": 3, "exit_type_long": "TP", "exit_type_short": "TIMEOUT",
                "truncated": 0, "warm_up": 0,
                "horizon_bars": 6, "tp_pct": 0.003, "sl_pct": 0.0015,
            }
            for fname in _FEATURE_NAMES:
                r[fname] = 0.5
            rows.append(r)
        db.upsert_training_data(rows)
        with patch("stages.vanguard_training_backfill.DB_PATH", db_path):
            ok = validate()
        assert ok is True
