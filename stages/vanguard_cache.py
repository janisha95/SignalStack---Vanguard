"""
vanguard_cache.py — V1 Vanguard Cache Pipeline.

Loads the US equity universe, applies a prefilter, fetches 5m bars via
Alpaca REST, derives 1h bars, and writes everything to vanguard_universe.db.

CLI:
    python3 stages/vanguard_cache.py                         # full run (live/scheduled)
    python3 stages/vanguard_cache.py --dry-run               # show universe size, no fetch
    python3 stages/vanguard_cache.py --symbols AAPL,MSFT     # specific symbols only
    python3 stages/vanguard_cache.py --validate              # validate DB, exit
    python3 stages/vanguard_cache.py --backfill --days 30    # historical download
    python3 stages/vanguard_cache.py --backfill --days 5 --symbols AAPL,MSFT,NVDA
    python3 stages/vanguard_cache.py --backfill --start 2026-01-01 --end 2026-03-01
    python3 stages/vanguard_cache.py --backfill --source mt5 --symbols EURUSD,XAUUSD

All writes go to data/vanguard_universe.db only.

Location: ~/SS/Vanguard/stages/vanguard_cache.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow imports from ~/SS/Vanguard/
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.clock import (
    now_utc, now_et, is_market_open,
    market_open_utc, last_market_open_utc,
    is_prefilter_due, iso_utc,
)
from vanguard.helpers.db import VanguardDB
from vanguard.helpers.bars import (
    normalize_alpaca_bar,
    normalize_mt5_bar,
    aggregate_5m_to_1h,
)
from vanguard.helpers import universe_builder
from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter
from vanguard.data_adapters.mt5_data_adapter import MT5DataAdapter

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = str(_REPO_ROOT / "data" / "vanguard_universe.db")

# How many bars to fetch per cycle (covers ~6.5 hours of 5m bars)
INTRADAY_FETCH_MINUTES = 390

# How often to refresh bars during live mode (seconds)
LIVE_CYCLE_INTERVAL = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_cache")


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(db: VanguardDB, market_hours_check: bool = True) -> dict:
    """
    Run post-run validation checks on vanguard_universe.db.
    Returns {"pass": bool, "checks": {name: {pass, detail}}}
    """
    checks: dict[str, dict] = {}

    # Check 1: at least 1,000 symbols in vanguard_bars_5m
    sym_count = db.count_symbols_5m()
    checks["min_symbols_5m"] = {
        "pass":   sym_count >= 1000,
        "detail": f"{sym_count} symbols in vanguard_bars_5m (need ≥1,000)",
    }

    # Check 2: vanguard_bars_5m has data at all
    bars_5m = db.count_bars_5m()
    checks["bars_5m_nonempty"] = {
        "pass":   bars_5m > 0,
        "detail": f"{bars_5m:,} rows in vanguard_bars_5m",
    }

    # Check 3: vanguard_bars_1h has data
    bars_1h = db.count_bars_1h()
    checks["bars_1h_nonempty"] = {
        "pass":   bars_1h > 0,
        "detail": f"{bars_1h:,} rows in vanguard_bars_1h",
    }

    # Check 4: no stale bars (only during market hours)
    latest_ts = db.latest_bar_ts_utc("vanguard_bars_5m")
    if market_hours_check and is_market_open():
        if latest_ts:
            latest_dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            age_min = (now_utc() - latest_dt).total_seconds() / 60
            checks["bars_fresh"] = {
                "pass":   age_min <= 10,
                "detail": f"Latest 5m bar is {age_min:.1f} min old (need ≤10 min)",
            }
        else:
            checks["bars_fresh"] = {"pass": False, "detail": "No bars in DB"}
    else:
        checks["bars_fresh"] = {
            "pass":   True,
            "detail": f"Market closed — staleness check skipped (latest: {latest_ts})",
        }

    # Check 5: DB size sanity
    db_mb = db.db_size_mb()
    checks["db_size"] = {
        "pass":   db_mb < 500,
        "detail": f"DB size: {db_mb:.1f} MB (red flag >500 MB)",
    }

    overall = all(c["pass"] for c in checks.values())
    return {"pass": overall, "checks": checks}


def print_validate(result: dict) -> None:
    status = "PASS" if result["pass"] else "FAIL"
    logger.info("=== VALIDATION %s ===", status)
    for name, check in result["checks"].items():
        icon = "✓" if check["pass"] else "✗"
        logger.info("  %s %s: %s", icon, name, check["detail"])


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------

def run_prefilter(
    alpaca: AlpacaAdapter,
    symbols_override: list[str] | None = None,
) -> list[str]:
    """
    Return the list of tradeable symbols for this cycle.
    If symbols_override is set, skip the prefilter and use those symbols.
    Filter rules are loaded from config/vanguard_universes.json via universe_builder.
    """
    if symbols_override:
        logger.info("Symbol override: using %d symbols", len(symbols_override))
        return [s.upper() for s in symbols_override]

    return universe_builder.get_equity_universe(alpaca_adapter=alpaca)


# ---------------------------------------------------------------------------
# Fetch + write 5m bars (Alpaca)
# ---------------------------------------------------------------------------

def fetch_and_write_alpaca(
    db: VanguardDB,
    alpaca: AlpacaAdapter,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """
    Fetch 5m bars from Alpaca for symbols in [start, end], derive 1h bars,
    and write both to DB.
    Returns (bars_5m_written, bars_1h_written).
    """
    raw = alpaca.get_5m_bars(symbols, start, end)

    rows_5m: list[dict] = []
    for sym, bars in raw.items():
        for bar in bars:
            rows_5m.append(normalize_alpaca_bar(bar, sym))

    rows_1h = aggregate_5m_to_1h(rows_5m)

    written_5m = db.upsert_bars_5m(rows_5m)
    written_1h = db.upsert_bars_1h(rows_1h)

    logger.info(
        "Alpaca wrote: %d 5m bars, %d 1h bars for %d symbols",
        written_5m, written_1h, len(raw),
    )
    return written_5m, written_1h


# ---------------------------------------------------------------------------
# Fetch + write 5m bars (MT5)
# ---------------------------------------------------------------------------

def fetch_and_write_mt5(
    db: VanguardDB,
    mt5: MT5DataAdapter,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """
    Fetch 5m bars from MT5 for symbols in [start, end], derive 1h bars,
    and write both to DB.
    Returns (bars_5m_written, bars_1h_written).
    """
    raw = mt5.get_bars_5m_multiple(symbols, start, end)

    rows_5m: list[dict] = []
    for sym, bars in raw.items():
        asset_cls = MT5DataAdapter.asset_class(sym)
        for bar in bars:
            rows_5m.append(normalize_mt5_bar(bar, sym, asset_class=asset_cls))

    rows_1h = aggregate_5m_to_1h(rows_5m)

    written_5m = db.upsert_bars_5m(rows_5m)
    written_1h = db.upsert_bars_1h(rows_1h)

    logger.info(
        "MT5 wrote: %d 5m bars, %d 1h bars for %d symbols",
        written_5m, written_1h, len(raw),
    )
    return written_5m, written_1h


# ---------------------------------------------------------------------------
# Backfill mode
# ---------------------------------------------------------------------------

def run_backfill(
    db: VanguardDB,
    alpaca: AlpacaAdapter,
    mt5_adapter: MT5DataAdapter,
    symbols: list[str] | None,
    start: datetime,
    end: datetime,
    source: str,
) -> None:
    """
    Historical bar download for the given symbol list and date range.
    Chunks into daily windows to avoid hitting pagination limits.
    """
    logger.info(
        "=== BACKFILL START | source=%s | %s → %s ===",
        source,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )

    from vanguard.data_adapters.mt5_data_adapter import FTMO_PRIORITY, _FTMO_ASSET_CLASS

    total_5m = total_1h = 0

    # ------------------------------------------------------------------
    # Alpaca path
    # ------------------------------------------------------------------
    if source in ("alpaca", "all"):
        if symbols:
            syms = [s.upper() for s in symbols]
        else:
            syms = run_prefilter(alpaca)

        logger.info("Alpaca backfill: %d symbols", len(syms))

        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
        one_day = timedelta(days=1)
        while cursor < end:
            day_end = min(cursor + one_day, end)
            logger.info(
                "Alpaca chunk: %s → %s",
                cursor.strftime("%Y-%m-%d"),
                day_end.strftime("%Y-%m-%d"),
            )
            w5m, w1h = fetch_and_write_alpaca(db, alpaca, syms, cursor, day_end)
            total_5m += w5m
            total_1h += w1h
            cursor = day_end

    # ------------------------------------------------------------------
    # MT5 path
    # ------------------------------------------------------------------
    if source in ("mt5", "all"):
        if not mt5_adapter.available:
            logger.warning(
                "MT5 not available on this platform (Windows-only) — skipping MT5 backfill"
            )
        else:
            connected = mt5_adapter.connect()
            if not connected:
                logger.warning("MT5 terminal not running — skipping MT5 backfill")
            else:
                # Use provided symbol list or FTMO_PRIORITY
                mt5_targets = [s.upper() for s in symbols] if symbols else FTMO_PRIORITY
                # Resolve symbol names (FTMO may use broker-specific variants)
                raw = mt5_adapter.backfill_ftmo_priority(start, end, symbols=mt5_targets)

                rows_5m: list[dict] = []
                for resolved_sym, bars in raw.items():
                    asset_cls = _FTMO_ASSET_CLASS.get(
                        resolved_sym.upper(),
                        MT5DataAdapter.asset_class(resolved_sym),
                    )
                    for bar in bars:
                        rows_5m.append(
                            normalize_mt5_bar(bar, resolved_sym, asset_class=asset_cls)
                        )
                rows_1h = aggregate_5m_to_1h(rows_5m)
                w5m = db.upsert_bars_5m(rows_5m)
                w1h = db.upsert_bars_1h(rows_1h)
                total_5m += w5m
                total_1h += w1h
                logger.info(
                    "MT5 backfill wrote: %d 5m bars, %d 1h bars for %d symbols",
                    w5m, w1h, len(raw),
                )
                mt5_adapter.disconnect()

    db.set_meta("last_backfill_ts_utc", iso_utc(now_utc()))
    db.set_meta("last_backfill_source", source)

    logger.info(
        "=== BACKFILL COMPLETE | %d 5m bars | %d 1h bars | DB: %.1f MB ===",
        total_5m, total_1h, db.db_size_mb(),
    )


# ---------------------------------------------------------------------------
# Live / single-pass run mode
# ---------------------------------------------------------------------------

def run_once(
    db: VanguardDB,
    alpaca: AlpacaAdapter,
    symbols_override: list[str] | None = None,
    last_prefilter_et: datetime | None = None,
    _prefilter_cache: list[list[str]] | None = None,
) -> tuple[list[str], int, int]:
    """
    Single cache cycle: prefilter (if due) → fetch 5m bars → write.
    Returns (survivors, bars_5m_written, bars_1h_written).
    """
    if _prefilter_cache is None:
        _prefilter_cache = [[]]   # mutable container to pass survivors back

    # Prefilter (or use override/cached)
    if symbols_override:
        survivors = [s.upper() for s in symbols_override]
    elif is_prefilter_due(last_prefilter_et) or not _prefilter_cache[0]:
        survivors = run_prefilter(alpaca)
        _prefilter_cache[0] = survivors
        db.set_meta("last_prefilter_ts_utc", iso_utc(now_utc()))
        db.set_meta("survivor_count", str(len(survivors)))
    else:
        survivors = _prefilter_cache[0]

    if not survivors:
        logger.warning("No survivors after prefilter — skipping fetch")
        return survivors, 0, 0

    # Determine fetch window: last market open → now
    fetch_start = last_market_open_utc()
    fetch_end = now_utc()

    w5m, w1h = fetch_and_write_alpaca(db, alpaca, survivors, fetch_start, fetch_end)
    db.set_meta("last_fetch_ts_utc", iso_utc(now_utc()))
    return survivors, w5m, w1h


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Vanguard Cache Pipeline — V1 data ingestion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run",  action="store_true",
                   help="Show universe size, do not fetch bars")
    p.add_argument("--validate", action="store_true",
                   help="Run validation checks on DB and exit")
    p.add_argument("--symbols",  type=str, default=None,
                   help="Comma-separated symbols (e.g. AAPL,MSFT,NVDA)")
    p.add_argument("--backfill", action="store_true",
                   help="Run historical bar download (REST, not streaming)")
    p.add_argument("--days", type=int, default=90,
                   help="Lookback days for --backfill (default: 90 ≈ 3 months for ML training)")
    p.add_argument("--start", type=str, default=None,
                   help="Start date for --backfill (YYYY-MM-DD)")
    p.add_argument("--end",   type=str, default=None,
                   help="End date for --backfill (YYYY-MM-DD)")
    p.add_argument("--source", type=str, default="alpaca",
                   choices=["alpaca", "mt5", "all"],
                   help="Data source for --backfill: alpaca, mt5, or all (default: alpaca)")
    p.add_argument("--live",   action="store_true",
                   help="Run continuously during market hours (scheduled mode)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    db = VanguardDB(DB_PATH)
    alpaca = AlpacaAdapter()
    mt5_adapter = MT5DataAdapter()

    symbols_override = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else None
    )

    # ------------------------------------------------------------------
    # --validate
    # ------------------------------------------------------------------
    if args.validate:
        result = validate(db)
        print_validate(result)
        sys.exit(0 if result["pass"] else 1)

    # ------------------------------------------------------------------
    # --dry-run
    # ------------------------------------------------------------------
    if args.dry_run:
        logger.info("DRY-RUN: building equity universe (no bar fetch)…")
        if symbols_override:
            logger.info("Symbol override: %d symbols: %s", len(symbols_override), symbols_override)
        else:
            survivors = run_prefilter(alpaca)
            logger.info(
                "DRY-RUN RESULT: %d equity survivors would be fetched",
                len(survivors),
            )
            logger.info("Sample (first 20): %s", survivors[:20])
        return

    # ------------------------------------------------------------------
    # --backfill
    # ------------------------------------------------------------------
    if args.backfill:
        now = now_utc()
        if args.start:
            start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            start_dt = now - timedelta(days=args.days)

        if args.end:
            end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            end_dt = now

        run_backfill(
            db=db,
            alpaca=alpaca,
            mt5_adapter=mt5_adapter,
            symbols=symbols_override,
            start=start_dt,
            end=end_dt,
            source=args.source,
        )
        # After backfill: run validation
        result = validate(db, market_hours_check=False)
        print_validate(result)
        return

    # ------------------------------------------------------------------
    # --live: continuous scheduled mode
    # ------------------------------------------------------------------
    if args.live:
        logger.info("Live mode started — polling every %ds", LIVE_CYCLE_INTERVAL)
        last_prefilter_et: datetime | None = None
        prefilter_cache: list[list[str]] = [[]]

        while True:
            try:
                if not is_market_open():
                    logger.debug("Market closed — sleeping 60s")
                    time.sleep(60)
                    continue

                survivors, w5m, w1h = run_once(
                    db, alpaca,
                    symbols_override=symbols_override,
                    last_prefilter_et=last_prefilter_et,
                    _prefilter_cache=prefilter_cache,
                )
                if is_prefilter_due(last_prefilter_et):
                    last_prefilter_et = now_et()

                logger.info(
                    "Cycle done: %d symbols | %d 5m bars | %d 1h bars",
                    len(survivors), w5m, w1h,
                )
            except KeyboardInterrupt:
                logger.info("Interrupted — exiting")
                break
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)
            time.sleep(LIVE_CYCLE_INTERVAL)
        return

    # ------------------------------------------------------------------
    # Default: single-pass run (fetch current day's bars once)
    # ------------------------------------------------------------------
    logger.info("Single-pass cache run — %s", now_et().strftime("%Y-%m-%d %H:%M ET"))

    survivors, w5m, w1h = run_once(db, alpaca, symbols_override=symbols_override)
    logger.info(
        "Done: %d symbols | %d 5m bars | %d 1h bars | DB: %.1f MB",
        len(survivors), w5m, w1h, db.db_size_mb(),
    )

    result = validate(db, market_hours_check=is_market_open())
    print_validate(result)


if __name__ == "__main__":
    main()
