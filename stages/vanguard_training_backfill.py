"""
vanguard_training_backfill.py — V4A Vanguard Training Backfill.

Replays V3 factor computation on historical 5m bars and generates TBM
(Triple Barrier Method) labels for both long and short directions.

Design:
  - Per-ticker sliding window: for each symbol, load ALL bars, then compute
    features and labels for every in-session bar using a rolling 200-bar window.
    ~10x faster than Meridian's per-date loop.
  - ProcessPoolExecutor: symbols split across workers; each worker loads its
    own DB connection (WAL supports concurrent reads).
  - Session-aware TBM: no cross-session leakage (US equities: 9:30–16:00 ET).
  - Resume-safe: incremental append by default; --full-rebuild rebuilds all.

TBM defaults: tp_mult=2.0, sl_mult=1.0, horizon_bars=6

CLI:
  python3 stages/vanguard_training_backfill.py
  python3 stages/vanguard_training_backfill.py --full-rebuild
  python3 stages/vanguard_training_backfill.py --symbols AAPL,SPY
  python3 stages/vanguard_training_backfill.py --workers 6
  python3 stages/vanguard_training_backfill.py --dry-run
  python3 stages/vanguard_training_backfill.py --validate
  python3 stages/vanguard_training_backfill.py --start-date 2026-01-01
  python3 stages/vanguard_training_backfill.py --tp-pct 0.005 --sl-pct 0.0025

Location: ~/SS/Vanguard/stages/vanguard_training_backfill.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# sys.path must be set before any vanguard.* imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from vanguard.helpers.db import VanguardDB
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.factors import (
    price_location,
    momentum,
    volume,
    market_context,
    session_time,
    smc_5m,
    smc_htf_1h,
    quality,
)
from vanguard.features.feature_computer import (
    FEATURE_NAMES as SHARED_FEATURE_NAMES,
    compute_all_features,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_training_backfill")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "vanguard_universe.db")

SPY_SYMBOL           = "SPY"
BARS_5M_LIMIT        = 50_000   # max 5m bars per symbol (~16 months at 78/day)
BARS_1H_LIMIT        = 2_000    # max 1h bars per symbol
BARS_LOOKBACK        = 200      # sliding window size for feature computation
MIN_BARS_FOR_FEATURE = 20       # skip bars with fewer than this in context
WARM_UP_SESSION_BARS = 15       # bars_since_session_open < 15 → warm_up = 1
BATCH_WRITE_SIZE     = 2_000    # rows per DB write batch

DEFAULT_HORIZON_BARS = 6
DEFAULT_TP_MULT      = 2.0
DEFAULT_SL_MULT      = 1.0
TBM_CONFIG_PATH      = Path(__file__).resolve().parent.parent / "config" / "vanguard_tbm_params.json"

# ---------------------------------------------------------------------------
# Factor module registry (same order as vanguard_factor_engine)
# ---------------------------------------------------------------------------

_FACTOR_MODULES = [
    price_location,
    momentum,
    volume,
    market_context,
    session_time,
    smc_5m,
    smc_htf_1h,
    quality,   # must be last — nan_ratio overwritten after all modules run
]

# Single source of truth: V4A writes the exact same feature columns exported by
# vanguard.features.feature_computer, including the BTC-derived crypto features.
_FEATURE_NAMES = list(SHARED_FEATURE_NAMES)


def load_tbm_params_map(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load per-asset-class TBM parameters from config."""
    path = Path(config_path or TBM_CONFIG_PATH)
    with open(path) as fh:
        data = json.load(fh)
    return {str(asset_class): dict(params) for asset_class, params in data.items()}

# ---------------------------------------------------------------------------
# Eastern Time helpers (DST-aware, no pytz)
# ---------------------------------------------------------------------------

def _et_offset(dt_utc: datetime) -> int:
    """Return UTC→ET hour offset: -4 (EDT) or -5 (EST)."""
    year = dt_utc.year
    # DST start: 2nd Sunday in March at 07:00 UTC
    march1 = datetime(year, 3, 1, 7, 0, tzinfo=timezone.utc)
    first_sun_mar = march1 + timedelta(days=(6 - march1.weekday()) % 7)
    dst_start = first_sun_mar + timedelta(weeks=1)
    # DST end: 1st Sunday in November at 06:00 UTC
    nov1 = datetime(year, 11, 1, 6, 0, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -4 if dst_start <= dt_utc < dst_end else -5


def _to_et(dt_utc: datetime) -> datetime:
    """Convert UTC tz-aware datetime to naive Eastern Time datetime."""
    return (dt_utc + timedelta(hours=_et_offset(dt_utc))).replace(tzinfo=None)


def _session_id(bar_ts_utc: str) -> str | None:
    """
    Return 'YYYY-MM-DD' ET date if bar is within regular US equity session
    (09:30 – 16:00 ET), else None.
    """
    dt = datetime.fromisoformat(bar_ts_utc.replace("Z", "+00:00"))
    et = _to_et(dt)
    h, m = et.hour, et.minute
    if (h > 9 or (h == 9 and m >= 30)) and h < 16:
        return et.strftime("%Y-%m-%d")
    return None


# ---------------------------------------------------------------------------
# TBM: Triple Barrier Method
# ---------------------------------------------------------------------------

def _compute_tbm(
    bars: list[dict],
    i: int,
    sid: str,
    session_end_idx: dict[str, int],
    tp_mult: float,
    sl_mult: float,
    horizon_bars: int,
) -> dict:
    """
    Compute TBM labels for long and short directions from entry bar i.

    Scans forward up to horizon_bars within the same session.
    Both labels computed in a single forward pass.
    """
    entry = float(bars[i].get("close") or 0.0)
    if entry <= 0.0:
        return {
            "label_long": 0, "label_short": 0,
            "forward_return": 0.0,
            "max_favorable_excursion": 0.0,
            "max_adverse_excursion": 0.0,
            "exit_bar": 0,
            "exit_type_long": "TIMEOUT",
            "exit_type_short": "TIMEOUT",
            "truncated": 0,
            "tp_pct": 0.0,
            "sl_pct": 0.0,
        }

    atr_window = bars[max(0, i - 14) : i + 1]
    highs = np.array([float(bar.get("high") or 0.0) for bar in atr_window], dtype=float)
    lows = np.array([float(bar.get("low") or 0.0) for bar in atr_window], dtype=float)
    closes = np.array([float(bar.get("close") or 0.0) for bar in atr_window], dtype=float)
    if len(highs) > 1:
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        atr_14 = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
    else:
        atr_14 = 0.001
    if not np.isfinite(atr_14) or atr_14 <= 0.0:
        atr_14 = 0.001

    tp_dist = tp_mult * atr_14
    sl_dist = sl_mult * atr_14
    tp_pct = tp_dist / entry if entry > 0.0 else 0.0
    sl_pct = sl_dist / entry if entry > 0.0 else 0.0

    tp_long  = entry + tp_dist
    sl_long  = entry - sl_dist
    tp_short = entry - tp_dist
    sl_short = entry + sl_dist

    sess_end  = session_end_idx.get(sid, i)
    max_fwd   = min(i + horizon_bars, sess_end)
    truncated = int(max_fwd < i + horizon_bars)

    label_long      = 0
    label_short     = 0
    # default exit bar = bars actually available (may be < horizon_bars if truncated)
    available       = max_fwd - i
    exit_bar_long   = available
    exit_bar_short  = available
    exit_type_long  = "TIMEOUT"
    exit_type_short = "TIMEOUT"

    max_high   = entry
    min_low    = entry
    last_close = entry

    for j in range(i + 1, max_fwd + 1):
        bar_j  = bars[j]
        h      = float(bar_j.get("high")  or 0.0)
        lo     = float(bar_j.get("low")   or 0.0)
        cl     = float(bar_j.get("close") or last_close)
        bar_num = j - i

        if h > max_high: max_high   = h
        if lo < min_low: min_low    = lo
        last_close = cl

        # Long: TP = high reaches tp_long, SL = low reaches sl_long
        if exit_type_long == "TIMEOUT":
            if h >= tp_long and lo <= sl_long:
                exit_type_long = "SL"; label_long = 0; exit_bar_long = bar_num
            elif h >= tp_long:
                exit_type_long = "TP"; label_long = 1; exit_bar_long = bar_num
            elif lo <= sl_long:
                exit_type_long = "SL"; label_long = 0; exit_bar_long = bar_num

        # Short: TP = low reaches tp_short, SL = high reaches sl_short
        if exit_type_short == "TIMEOUT":
            if lo <= tp_short and h >= sl_short:
                exit_type_short = "SL"; label_short = 0; exit_bar_short = bar_num
            elif lo <= tp_short:
                exit_type_short = "TP"; label_short = 1; exit_bar_short = bar_num
            elif h >= sl_short:
                exit_type_short = "SL"; label_short = 0; exit_bar_short = bar_num

        if exit_type_long != "TIMEOUT" and exit_type_short != "TIMEOUT":
            break

    fwd_return = (last_close - entry) / entry if entry > 0.0 else 0.0
    mfe        = (max_high - entry)  / entry  if entry > 0.0 else 0.0
    mae        = (entry - min_low)   / entry  if entry > 0.0 else 0.0
    exit_bar   = min(exit_bar_long, exit_bar_short) if available else 0

    return {
        "label_long":              label_long,
        "label_short":             label_short,
        "forward_return":          round(fwd_return, 6),
        "max_favorable_excursion": round(mfe, 6),
        "max_adverse_excursion":   round(mae, 6),
        "exit_bar":                exit_bar,
        "exit_type_long":          exit_type_long,
        "exit_type_short":         exit_type_short,
        "truncated":               truncated,
        "tp_pct":                  round(tp_pct, 6),
        "sl_pct":                  round(sl_pct, 6),
    }


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------

def _compute_features(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame | None,
    spy_df: pd.DataFrame,
    asset_class: str = "equity",
    symbol: str = "",
) -> dict[str, float]:
    """Run all 8 factor modules; overwrite nan_ratio placeholder after."""
    shared_df = df_5m.copy()
    shared_df.attrs["df_1h"] = df_1h
    shared_df.attrs["benchmark_df"] = spy_df
    shared_df.attrs["asset_class"] = asset_class
    shared_df.attrs["symbol"] = symbol
    shared_df.attrs["fillna_zero"] = True
    features = compute_all_features(shared_df)
    return features.iloc[0].to_dict() if not features.empty else {}

    combined: dict[str, float] = {}
    for mod in _FACTOR_MODULES:
        try:
            combined.update(mod.compute(df_5m, df_1h, spy_df))
        except Exception:
            for name in getattr(mod, "FEATURE_NAMES", []):
                combined.setdefault(name, float("nan"))
    if "nan_ratio" in combined:
        nan_count = sum(
            1 for v in combined.values() if isinstance(v, float) and math.isnan(v)
        )
        combined["nan_ratio"] = round(nan_count / len(combined), 6) if combined else 0.0
    return combined


def _nan_to_none(v: Any) -> Any:
    """Convert float NaN to None (SQLite stores as NULL)."""
    return None if isinstance(v, float) and math.isnan(v) else v


# ---------------------------------------------------------------------------
# DB helpers (standalone, used inside worker and main)
# ---------------------------------------------------------------------------

def _get_resume_ts(db: VanguardDB, symbol: str, horizon_bars: int) -> str | None:
    """Return MAX(asof_ts_utc) for symbol + horizon_bars (resume point)."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT MAX(asof_ts_utc) FROM vanguard_training_data "
            "WHERE symbol = ? AND horizon_bars = ?",
            (symbol, horizon_bars),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def _delete_symbol_rows(db: VanguardDB, symbol: str, horizon_bars: int) -> int:
    """Delete all training rows for symbol + horizon_bars. Returns row count."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "DELETE FROM vanguard_training_data WHERE symbol = ? AND horizon_bars = ?",
            (symbol, horizon_bars),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker — must be module-level for ProcessPoolExecutor pickling
# ---------------------------------------------------------------------------

def _worker(args: tuple) -> dict:
    """
    Process a chunk of symbols.
    Returns {rows_written, symbols_processed, symbols_skipped, errors}.
    """
    # Ensure sys.path is set in spawned worker process
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

    symbols, db_path, tbm_params, opts = args
    horizon_bars = tbm_params["horizon_bars"]
    tp_mult      = tbm_params["tp_mult"]
    sl_mult      = tbm_params["sl_mult"]
    tbm_profile  = tbm_params.get("version")

    db = VanguardDB(db_path)

    # Load SPY once per worker (readonly — WAL concurrent reads are safe)
    spy_rows = db.get_bars_for_symbol(SPY_SYMBOL, table="vanguard_bars_5m", limit=BARS_5M_LIMIT)
    spy_df = pd.DataFrame(spy_rows) if spy_rows else pd.DataFrame()
    if not spy_df.empty:
        spy_df.sort_values("bar_ts_utc", inplace=True)
        spy_df.reset_index(drop=True, inplace=True)

    stats: dict[str, Any] = {
        "rows_written":      0,
        "symbols_processed": 0,
        "symbols_skipped":   0,
        "errors":            [],
    }

    for symbol in symbols:
        try:
            bars_5m_rows = db.get_bars_for_symbol(
                symbol, table="vanguard_bars_5m", limit=BARS_5M_LIMIT
            )
            if not bars_5m_rows:
                stats["symbols_skipped"] += 1
                continue

            bars_1h_rows = db.get_bars_for_symbol(
                symbol, table="vanguard_bars_1h", limit=BARS_1H_LIMIT
            )

            df_5m = pd.DataFrame(bars_5m_rows)
            df_5m.sort_values("bar_ts_utc", inplace=True)
            df_5m.reset_index(drop=True, inplace=True)

            df_1h: pd.DataFrame | None
            if bars_1h_rows:
                df_1h = pd.DataFrame(bars_1h_rows)
                df_1h.sort_values("bar_ts_utc", inplace=True)
                df_1h.reset_index(drop=True, inplace=True)
            else:
                df_1h = None

            bars_list = df_5m.to_dict("records")
            n         = len(bars_list)

            # Resume / full-rebuild
            if opts.get("full_rebuild"):
                if not opts.get("dry_run"):
                    _delete_symbol_rows(db, symbol, horizon_bars)
                resume_after: str | None = None
            else:
                resume_after = _get_resume_ts(db, symbol, horizon_bars)

            # Pre-compute session IDs for every bar
            session_ids = [_session_id(b["bar_ts_utc"]) for b in bars_list]

            # session_end_idx: sid → index of last in-session bar for that day
            session_end_idx: dict[str, int] = {}
            for idx, sid in enumerate(session_ids):
                if sid is not None:
                    session_end_idx[sid] = idx

            asset_class = opts.get("asset_class", "equity")
            path        = opts.get("path", "ttp_equity")
            start_date  = opts.get("start_date")
            end_date    = opts.get("end_date")
            dry_run     = opts.get("dry_run", False)

            batch: list[dict] = []

            for i in range(n):
                sid = session_ids[i]
                if sid is None:
                    continue  # pre/post-market bar

                ts = bars_list[i]["bar_ts_utc"]

                # Date-range filter
                if start_date and ts[:10] < start_date:
                    continue
                if end_date and ts[:10] > end_date:
                    continue

                # Resume: skip already-processed bars
                if resume_after and ts <= resume_after:
                    continue

                # Minimum context required for features
                if i < MIN_BARS_FOR_FEATURE:
                    continue

                # ---- Sliding-window feature computation ----
                lo_idx   = max(0, i - BARS_LOOKBACK + 1)
                df_slice = df_5m.iloc[lo_idx : i + 1].copy()
                df_slice.reset_index(drop=True, inplace=True)

                features = _compute_features(
                    df_slice,
                    df_1h,
                    spy_df,
                    asset_class=asset_class,
                    symbol=symbol,
                )

                # warm_up flag via bars_since_session_open feature
                bsso = features.get("bars_since_session_open")
                if bsso is not None and not (isinstance(bsso, float) and math.isnan(bsso)):
                    warm_up_flag = int(float(bsso) < WARM_UP_SESSION_BARS)
                else:
                    warm_up_flag = 0

                # ---- TBM ----
                tbm = _compute_tbm(
                    bars_list, i, sid, session_end_idx,
                    tp_mult, sl_mult, horizon_bars,
                )

                entry_price = float(bars_list[i].get("close") or 0.0)

                row: dict[str, Any] = {
                    "asof_ts_utc":  ts,
                    "symbol":       symbol,
                    "asset_class":  asset_class,
                    "path":         path,
                    "entry_price":  entry_price,
                    "warm_up":      warm_up_flag,
                    **tbm,
                    "horizon_bars": horizon_bars,
                    "tp_pct":       tbm["tp_pct"],
                    "sl_pct":       tbm["sl_pct"],
                    "tbm_profile":  tbm_profile,
                }
                # Copy 35 features, converting NaN → None for SQLite
                for fname in _FEATURE_NAMES:
                    row[fname] = _nan_to_none(features.get(fname))

                batch.append(row)

                if len(batch) >= BATCH_WRITE_SIZE:
                    if not dry_run:
                        db.upsert_training_data(batch)
                    stats["rows_written"] += len(batch)
                    batch.clear()

            # Flush remaining
            if batch:
                if not dry_run:
                    db.upsert_training_data(batch)
                stats["rows_written"] += len(batch)

            stats["symbols_processed"] += 1

        except Exception as exc:
            stats["errors"].append(f"{symbol}: {exc}")

    return stats


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    db_path: str = DB_PATH,
    symbols: list[str] | None = None,
    workers: int = 4,
    dry_run: bool = False,
    full_rebuild: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    asset_class: str | None = None,
    path: str | None = None,
    horizon_bars: int | None = None,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    tbm_config_path: str | Path | None = None,
) -> dict:
    """Run the training backfill. Returns summary stats dict."""
    db = VanguardDB(db_path)
    tbm_by_asset_class = load_tbm_params_map(tbm_config_path)

    targets = [s.upper() for s in symbols] if symbols else db.get_symbols_with_bars("vanguard_bars_5m")
    if not targets:
        logger.warning("No symbols found in vanguard_bars_5m — nothing to do")
        return {"rows_written": 0, "symbols_processed": 0, "errors": []}

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT symbol, COALESCE(asset_class, 'equity') AS asset_class "
            "FROM vanguard_bars_5m GROUP BY symbol"
        ).fetchall()
    finally:
        conn.close()
    asset_class_by_symbol = {row[0]: row[1] for row in rows}

    grouped_targets: dict[str, list[str]] = {}
    for symbol in targets:
        resolved_asset_class = asset_class_by_symbol.get(symbol, asset_class or "equity")
        if asset_class and resolved_asset_class != asset_class:
            continue
        grouped_targets.setdefault(resolved_asset_class, []).append(symbol)

    if not grouped_targets:
        logger.warning("No symbols matched the requested asset_class filter")
        return {"rows_written": 0, "symbols_processed": 0, "errors": []}

    def _run_asset_class(asset_class_name: str, asset_symbols: list[str]) -> tuple[int, int, list[str]]:
        params = dict(tbm_by_asset_class.get(asset_class_name, tbm_by_asset_class.get("equity", {})))
        if horizon_bars is not None:
            params["horizon_bars"] = horizon_bars
        if tp_pct is not None:
            params["tp_mult"] = tp_pct
        if sl_pct is not None:
            params["sl_mult"] = sl_pct
        if "horizon_bars" not in params or "tp_mult" not in params or "sl_mult" not in params:
            raise RuntimeError(f"Missing TBM params for asset_class={asset_class_name}")

        opts = {
            "full_rebuild": full_rebuild,
            "dry_run":      dry_run,
            "start_date":   start_date,
            "end_date":     end_date,
            "asset_class":  asset_class_name,
            "path":         path or f"vanguard_{asset_class_name}",
        }

        logger.info(
            "V4A Backfill | asset_class=%s | symbols=%d | workers=%d | horizon=%d | "
            "tp_mult=%.2f | sl_mult=%.2f | tbm=%s | full_rebuild=%s | dry_run=%s",
            asset_class_name,
            len(asset_symbols),
            workers,
            params["horizon_bars"],
            params["tp_mult"],
            params["sl_mult"],
            params.get("version"),
            full_rebuild,
            dry_run,
        )

        effective_workers = max(1, min(workers, len(asset_symbols)))
        chunk_size = math.ceil(len(asset_symbols) / effective_workers)
        chunks = [asset_symbols[i : i + chunk_size] for i in range(0, len(asset_symbols), chunk_size)]

        class_rows = 0
        class_symbols = 0
        class_errors: list[str] = []

        if effective_workers == 1:
            result = _worker((asset_symbols, db_path, params, opts))
            class_rows += result["rows_written"]
            class_symbols += result["symbols_processed"]
            class_errors += result["errors"]
            return class_rows, class_symbols, class_errors

        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(_worker, (chunk, db_path, params, opts)): chunk
                for chunk in chunks
            }
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                    class_rows += result["rows_written"]
                    class_symbols += result["symbols_processed"]
                    class_errors += result["errors"]
                    logger.info(
                        "Worker done | asset_class=%s | %d rows | %d symbols",
                        asset_class_name,
                        result["rows_written"],
                        result["symbols_processed"],
                    )
                except Exception as exc:
                    logger.error("Worker raised | asset_class=%s | %s", asset_class_name, exc)
                    class_errors.append(str(exc))
        return class_rows, class_symbols, class_errors

    t0 = time.perf_counter()
    total_rows = 0
    total_symbols = 0
    all_errors: list[str] = []

    for asset_class_name in sorted(grouped_targets):
        class_rows, class_symbols, class_errors = _run_asset_class(
            asset_class_name,
            grouped_targets[asset_class_name],
        )
        total_rows += class_rows
        total_symbols += class_symbols
        all_errors += class_errors

    elapsed = time.perf_counter() - t0
    rows_per_min = (total_rows / elapsed * 60) if elapsed > 0 else 0

    logger.info(
        "V4A done | rows=%d | symbols=%d | elapsed=%.1fs | %.0f rows/min",
        total_rows, total_symbols, elapsed, rows_per_min,
    )
    if all_errors:
        for err in all_errors[:10]:
            logger.warning("Error: %s", err)

    return {
        "rows_written": total_rows,
        "symbols_processed": total_symbols,
        "errors": all_errors,
        "elapsed_s": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(horizon_bars: int = DEFAULT_HORIZON_BARS) -> bool:
    """Validate vanguard_training_data. Returns True if checks pass."""
    db = VanguardDB(DB_PATH)
    rates   = db.get_training_label_rates()
    total   = rates["total"]
    ll_rate = rates["label_long_rate"]
    ls_rate = rates["label_short_rate"]
    symbols = db.get_training_symbols()

    print(f"\n[VALIDATE] vanguard_training_data")
    print(f"  Total rows        : {total:,}")
    print(f"  Symbols with data : {len(symbols)}")
    print(f"  label_long  rate  : {ll_rate:.1%}")
    print(f"  label_short rate  : {ls_rate:.1%}")

    if total == 0:
        print("\n[VALIDATE] FAIL — no rows. Run backfill first.")
        return False

    # Check label rates in expected range
    if not (0.15 <= ll_rate <= 0.65):
        print(f"\n[VALIDATE] WARN — label_long rate {ll_rate:.1%} outside 15–65%")
    if not (0.15 <= ls_rate <= 0.65):
        print(f"\n[VALIDATE] WARN — label_short rate {ls_rate:.1%} outside 15–65%")

    # Check forward_return is centered near 0
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT AVG(forward_return) FROM vanguard_training_data"
        ).fetchone()
    finally:
        conn.close()
    avg_fwd = row[0] if (row and row[0] is not None) else 0.0
    print(f"  avg forward_return: {avg_fwd:.5f}")
    if abs(avg_fwd) > 0.005:
        print(f"\n[VALIDATE] WARN — avg forward_return {avg_fwd:.5f} may indicate bias")

    # Per-symbol breakdown
    print(f"\n  Per-symbol breakdown (first 15):")
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(*), AVG(label_long), AVG(label_short) "
            "FROM vanguard_training_data "
            "GROUP BY symbol ORDER BY symbol LIMIT 15"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        print(f"    {r[0]:<8} rows={r[1]:>5,}  long={r[2]:.1%}  short={r[3]:.1%}")

    passed = total > 0 and len(symbols) > 0
    print(f"\n[VALIDATE] {'PASS' if passed else 'FAIL'} — "
          f"{total:,} rows across {len(symbols)} symbols")
    return passed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V4A Vanguard Training Backfill — generates TBM training dataset"
    )
    p.add_argument("--symbols", default=None,
                   help="Comma-separated symbols (default: all in DB)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel worker count (default: 4)")
    p.add_argument("--full-rebuild", action="store_true",
                   help="Delete existing rows and rebuild from scratch")
    p.add_argument("--resume", action="store_true",
                   help="Resume from latest row per symbol (default mode)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute but do not write to DB")
    p.add_argument("--validate", action="store_true",
                   help="Validate existing training data and exit")
    p.add_argument("--start-date", default=None, metavar="YYYY-MM-DD",
                   help="Only process bars on/after this date")
    p.add_argument("--end-date", default=None, metavar="YYYY-MM-DD",
                   help="Only process bars on/before this date")
    p.add_argument("--asset-class", default=None,
                   help="Restrict to one asset class; default uses all configured classes found in bars")
    p.add_argument("--horizon-bars", type=int, default=None,
                   help="Override TBM horizon in 5m bars for all selected asset classes")
    p.add_argument("--tp-pct", type=float, default=None,
                   help="Override take-profit fraction for all selected asset classes")
    p.add_argument("--sl-pct", type=float, default=None,
                   help="Override stop-loss fraction for all selected asset classes")
    p.add_argument("--tbm-config", default=str(TBM_CONFIG_PATH),
                   help=f"Path to TBM config JSON (default: {TBM_CONFIG_PATH})")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.validate:
        ok = validate(horizon_bars=args.horizon_bars)
        sys.exit(0 if ok else 1)

    symbol_list: list[str] | None = None
    if args.symbols:
        symbol_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    run(
        symbols=symbol_list,
        workers=args.workers,
        dry_run=args.dry_run,
        full_rebuild=args.full_rebuild,
        start_date=args.start_date,
        end_date=args.end_date,
        asset_class=args.asset_class,
        horizon_bars=args.horizon_bars,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        tbm_config_path=args.tbm_config,
    )

    validate(horizon_bars=args.horizon_bars)


if __name__ == "__main__":
    main()
