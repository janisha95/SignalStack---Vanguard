"""
vanguard_factor_engine.py — V3 Vanguard Factor Engine (Part 2).

Reads ACTIVE symbols from vanguard_health, fetches 5m and 1h bars,
runs registered factor modules, and writes feature JSON to vanguard_features.

Part 1 modules:
  • price_location  (5 features)
  • momentum        (5 features)

Part 2 modules:
  • volume          (5 features)
  • market_context  (5 features)

Module registry pattern: new modules plug in by appending to FACTOR_MODULES.
Each module must implement:
    compute(df_5m, df_1h, spy_df) -> dict[str, float]

CLI:
  python3 stages/vanguard_factor_engine.py
  python3 stages/vanguard_factor_engine.py --symbols AAPL,MSFT,SPY
  python3 stages/vanguard_factor_engine.py --validate
  python3 stages/vanguard_factor_engine.py --dry-run
  python3 stages/vanguard_factor_engine.py --debug AAPL

Location: ~/SS/Vanguard/stages/vanguard_factor_engine.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
logger = logging.getLogger("vanguard_factor_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "vanguard_universe.db")

SPY_SYMBOL      = "SPY"
BARS_5M_LOOKBACK = 200   # ~16.5 hours of 5m bars
BARS_1H_LOOKBACK = 48    # 2 days of 1h bars

BENCHMARK_SYMBOLS = {
    "equity": "SPY",
    "index": "SPX",
    "forex": "DXY",
    "metal": "XAU/USD",
    "crypto": "BTC/USD",
    "energy": "WTI",
    "commodity": "WTI",
    "agriculture": "CORN",
}

BENCHMARK_FALLBACKS = {
    "forex": "EUR/USD",
}

NA_FEATURES_BY_ASSET_CLASS = {
    "forex": set(),
    "crypto": set(),
}

# ---------------------------------------------------------------------------
# Module registry — add new modules here, engine picks them up automatically
# ---------------------------------------------------------------------------

FACTOR_MODULES = [
    price_location,
    momentum,
    volume,
    market_context,
    session_time,
    smc_5m,
    smc_htf_1h,
    quality,          # must be last — nan_ratio overwritten by engine after all modules
]

# Expected feature names (all registered modules)
_EXPECTED_FEATURES = (
    price_location.FEATURE_NAMES
    + momentum.FEATURE_NAMES
    + volume.FEATURE_NAMES
    + market_context.FEATURE_NAMES
    + session_time.FEATURE_NAMES
    + smc_5m.FEATURE_NAMES
    + smc_htf_1h.FEATURE_NAMES
    + quality.FEATURE_NAMES
)
_EXPECTED_FEATURES = tuple(SHARED_FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------

def _load_bars_df(
    db: VanguardDB,
    symbol: str,
    table: str,
    limit: int,
) -> pd.DataFrame:
    """Load bars for a symbol into a DataFrame (chronological order)."""
    rows = db.get_bars_for_symbol(symbol, table=table, limit=limit)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _ensure_factor_matrix_schema(db: VanguardDB) -> None:
    """Ensure explicit factor-matrix storage exists alongside legacy JSON rows."""
    with db.connect() as con:
        feature_columns = ",\n                    ".join(
            f"{name} REAL" for name in _EXPECTED_FEATURES
        )
        try:
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS vanguard_factor_matrix (
                    cycle_ts_utc TEXT NOT NULL,
                    symbol       TEXT NOT NULL,
                    asset_class  TEXT NOT NULL,
                    {feature_columns},
                    PRIMARY KEY (cycle_ts_utc, symbol)
                )
                """
            )
            con.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vg_factor_matrix_cycle
                    ON vanguard_factor_matrix(cycle_ts_utc, asset_class)
                """
            )
            existing_columns = {
                str(row[1])
                for row in con.execute("PRAGMA table_info(vanguard_factor_matrix)").fetchall()
            }
            for feature_name in _EXPECTED_FEATURES:
                if feature_name not in existing_columns:
                    con.execute(
                        f"ALTER TABLE vanguard_factor_matrix ADD COLUMN {feature_name} REAL"
                    )
            con.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Skipping factor-matrix schema migration due to lock/contention: %s", exc)


def _write_factor_matrix(db: VanguardDB, rows: list[dict]) -> int:
    """Write explicit feature rows for the current cycle."""
    if not rows:
        return 0
    _ensure_factor_matrix_schema(db)
    columns = ["cycle_ts_utc", "symbol", "asset_class", *_EXPECTED_FEATURES]
    placeholders = ", ".join(f":{col}" for col in columns)
    with db.connect() as con:
        try:
            con.executemany(
                f"""
                INSERT OR REPLACE INTO vanguard_factor_matrix
                    ({", ".join(columns)})
                VALUES
                    ({placeholders})
                """,
                [
                    {
                        **{name: None for name in _EXPECTED_FEATURES},
                        **row,
                    }
                    for row in rows
                ],
            )
            con.commit()
        except sqlite3.OperationalError as exc:
            logger.warning("Skipping factor-matrix write due to lock/contention: %s", exc)
            return 0
    return len(rows)


def _get_benchmark_symbol(asset_class: str) -> str | None:
    return BENCHMARK_SYMBOLS.get(asset_class, SPY_SYMBOL if asset_class == "equity" else None)


def _apply_asset_class_overrides(
    features: dict[str, float],
    asset_class: str,
    df_5m: pd.DataFrame | None = None,
) -> dict[str, float]:
    adjusted = dict(features)
    for feature_name in NA_FEATURES_BY_ASSET_CLASS.get(asset_class, set()):
        adjusted[feature_name] = float("nan")
    if asset_class in {"forex", "crypto"} and df_5m is not None and len(df_5m) > 0:
        try:
            last_ts = pd.Timestamp(str(df_5m.sort_values("bar_ts_utc").iloc[-1]["bar_ts_utc"]), tz="UTC")
            minutes = last_ts.hour * 60 + last_ts.minute
            day_pct = round(minutes / 1440.0, 6)
            adjusted["session_phase"] = day_pct
            adjusted["time_in_session_pct"] = day_pct
            adjusted["bars_since_session_open"] = float(minutes // 5)
        except Exception:
            pass
    return adjusted


# ---------------------------------------------------------------------------
# Per-symbol computation
# ---------------------------------------------------------------------------

def compute_features(
    symbol: str,
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
    asset_class: str = "equity",
    debug: bool = False,
) -> dict[str, float]:
    """
    Run all registered factor modules for one symbol.
    Partial module failures produce NaN for that module's features.
    """
    shared_df = df_5m.copy()
    shared_df.attrs["df_1h"] = df_1h
    shared_df.attrs["benchmark_df"] = spy_df
    shared_df.attrs["asset_class"] = asset_class
    shared_df.attrs["symbol"] = symbol
    shared_df.attrs["debug"] = debug
    shared_df.attrs["fillna_zero"] = True
    shared_features = compute_all_features(shared_df)
    return shared_features.iloc[0].to_dict() if not shared_features.empty else {}

    combined: dict[str, float] = {}

    for module in FACTOR_MODULES:
        module_name = module.__name__.split(".")[-1]
        try:
            features = module.compute(df_5m, df_1h, spy_df)
            combined.update(features)
        except Exception as exc:
            logger.warning("Module %s failed for %s: %s", module_name, symbol, exc)
            # Fill NaN for this module's features
            for name in getattr(module, "FEATURE_NAMES", []):
                combined.setdefault(name, float("nan"))

    # Overwrite nan_ratio placeholder with actual value (quality module sets 0.0)
    if "nan_ratio" in combined:
        nan_count_actual = sum(
            1 for v in combined.values()
            if isinstance(v, float) and math.isnan(v)
        )
        total = len(combined)
        combined["nan_ratio"] = round(nan_count_actual / total, 6) if total else 0.0

    combined = _apply_asset_class_overrides(combined, asset_class, df_5m)

    if debug:
        print(f"\n=== DEBUG FEATURES: {symbol} ===")
        for k, v in combined.items():
            print(f"  {k:<40}: {v}")
        nan_count = sum(1 for v in combined.values() if isinstance(v, float) and math.isnan(v))
        print(f"  NaN count: {nan_count}/{len(combined)}")

    # Preserve the measured nan_ratio but never write None/NaN feature values to V4B.
    combined = {
        k: (0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else v)
        for k, v in combined.items()
    }

    return combined


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    survivors: list[dict] | None = None,
    symbols: list[str] | None = None,
    cycle_ts: str | None = None,
    dry_run: bool = False,
    debug_symbol: str | None = None,
) -> list[dict]:
    """
    Compute features for ACTIVE symbols (or specified list).

    Parameters
    ----------
    symbols      : explicit list; None = all ACTIVE from most recent health cycle
    dry_run      : compute but do not write to DB
    debug_symbol : print feature trace for this symbol

    Returns
    -------
    List of {symbol, cycle_ts_utc, features_json} dicts.
    """
    db       = VanguardDB(DB_PATH)
    now      = now_utc()
    cycle_ts = cycle_ts or iso_utc(now)

    logger.info("V3 Factor Engine | cycle=%s | dry_run=%s", cycle_ts, dry_run)

    # Determine target survivor rows
    if survivors is not None:
        targets = [
            {
                "symbol": row["symbol"].upper(),
                "asset_class": row.get("asset_class") or "equity",
            }
            for row in survivors
        ]
        logger.info("Computing features for %d V2 survivors", len(targets))
    elif symbols:
        targets = [{"symbol": s.upper(), "asset_class": "equity"} for s in symbols]
        logger.info("Computing features for %d specified symbols", len(targets))
    else:
        targets = db.get_active_health_rows()
        if not targets:
            logger.error("No ACTIVE symbols in vanguard_health — run V2 prefilter first")
            return []
        logger.info("Computing features for %d ACTIVE symbols", len(targets))

    results:      list[dict] = []
    matrix_rows:  list[dict] = []
    nan_counts:   list[int]  = []
    t_start = time.time()

    grouped_targets: dict[str, list[str]] = {}
    for row in targets:
        grouped_targets.setdefault(row["asset_class"], []).append(row["symbol"])

    for asset_class, class_symbols in grouped_targets.items():
        benchmark_symbol = _get_benchmark_symbol(asset_class)
        benchmark_df_5m = (
            _load_bars_df(db, benchmark_symbol, "vanguard_bars_5m", BARS_5M_LOOKBACK)
            if benchmark_symbol
            else pd.DataFrame()
        )
        if benchmark_df_5m.empty:
            fallback_symbol = BENCHMARK_FALLBACKS.get(asset_class)
            if fallback_symbol:
                benchmark_df_5m = _load_bars_df(db, fallback_symbol, "vanguard_bars_5m", BARS_5M_LOOKBACK)
        for sym in class_symbols:
            df_5m = _load_bars_df(db, sym, "vanguard_bars_5m", BARS_5M_LOOKBACK)
            df_1h = _load_bars_df(db, sym, "vanguard_bars_1h", BARS_1H_LOOKBACK)

            if df_5m.empty:
                logger.debug("No 5m bars for %s — skipping", sym)
                continue

            features = compute_features(
                sym,
                df_5m,
                df_1h,
                benchmark_df_5m,
                asset_class=asset_class,
                debug=(debug_symbol and sym.upper() == debug_symbol.upper()),
            )

            nan_count = sum(1 for v in features.values() if math.isnan(v))
            nan_counts.append(nan_count)
            if nan_count > len(features) * 0.10:
                logger.warning(
                    "%s: high NaN rate (%d/%d features)",
                    sym, nan_count, len(features),
                )

            results.append({
                "symbol":        sym,
                "cycle_ts_utc":  cycle_ts,
                "features_json": json.dumps(features),
            })
            matrix_rows.append({
                "cycle_ts_utc": cycle_ts,
                "symbol": sym,
                "asset_class": asset_class,
                **features,
            })

    elapsed = time.time() - t_start
    total_features = len(_EXPECTED_FEATURES)
    avg_nan = sum(nan_counts) / len(nan_counts) if nan_counts else 0
    nan_rate_pct = (avg_nan / total_features * 100) if total_features else 0

    logger.info(
        "V3 Factor Engine: %d symbols | %d features each | "
        "NaN rate: %.1f%% | elapsed: %.1fs",
        len(results), total_features, nan_rate_pct, elapsed,
    )

    if not dry_run:
        written = db.upsert_features(results)
        matrix_written = _write_factor_matrix(db, matrix_rows)
        logger.info(
            "Wrote %d feature rows to vanguard_features and %d rows to vanguard_factor_matrix",
            written,
            matrix_written,
        )
    else:
        logger.info("[DRY RUN] Would write %d rows (skipped)", len(results))

    return results


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(symbols: list[str] | None = None) -> bool:
    """
    Validate the most recent feature cycle.
    Checks:
      - Feature rows exist
      - SPY is present
      - All expected features are present per row
      - NaN rate below 20%
    """
    db = VanguardDB(DB_PATH)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT MAX(cycle_ts_utc) FROM vanguard_features"
        ).fetchone()
        cycle_ts = row[0] if row else None
    finally:
        conn.close()

    if not cycle_ts:
        print("[VALIDATE] FAIL — no feature data in DB. Run factor engine first.")
        return False

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT symbol, features_json FROM vanguard_features "
            "WHERE cycle_ts_utc = ?",
            (cycle_ts,),
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    if total == 0:
        print("[VALIDATE] FAIL — zero feature rows")
        return False

    print(f"\n[VALIDATE] Most recent cycle: {cycle_ts}")
    print(f"  Total symbols: {total}")
    print(f"  Expected features per symbol: {len(_EXPECTED_FEATURES)}")

    # Check feature completeness
    total_nan   = 0
    total_feats = 0
    missing_keys: set[str] = set()

    for sym, feat_json in rows:
        try:
            feats = json.loads(feat_json)
        except Exception:
            print(f"  WARN: {sym} — invalid JSON")
            continue

        for k in _EXPECTED_FEATURES:
            if k not in feats:
                missing_keys.add(k)
            else:
                total_feats += 1
                if math.isnan(feats[k]):
                    total_nan += 1

    nan_rate = total_nan / total_feats * 100 if total_feats else 0

    print(f"  NaN rate: {nan_rate:.1f}%")

    if missing_keys:
        print(f"  WARN: Missing feature keys: {sorted(missing_keys)}")

    if nan_rate > 50:
        print(f"[VALIDATE] FAIL — NaN rate {nan_rate:.1f}% > 50%")
        return False

    print(f"[VALIDATE] PASS — {total} symbols | NaN rate {nan_rate:.1f}%")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V3 Vanguard Factor Engine"
    )
    p.add_argument(
        "--symbols", default=None,
        help="Comma-separated symbols (default: ACTIVE from vanguard_health)",
    )
    p.add_argument(
        "--validate", action="store_true",
        help="Validate most recent feature cycle and exit",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute features but do not write to DB",
    )
    p.add_argument(
        "--debug", default=None, metavar="SYMBOL",
        help="Print detailed feature trace for one symbol",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    symbol_list: list[str] | None = None
    if args.symbols:
        symbol_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.validate and not symbol_list:
        ok = validate()
        sys.exit(0 if ok else 1)

    run(
        symbols=symbol_list,
        dry_run=args.dry_run,
        debug_symbol=args.debug,
    )

    validate(symbol_list)


if __name__ == "__main__":
    main()
