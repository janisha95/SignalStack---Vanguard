"""
backfill_twelvedata.py — Historical 1m bar backfill via Twelve Data REST API.

Usage examples:
    # Backfill all configured symbols, last 30 days
    python3 scripts/backfill_twelvedata.py

    # Backfill last N days for all configured symbols
    python3 scripts/backfill_twelvedata.py --days 7

    # Backfill a single symbol
    python3 scripts/backfill_twelvedata.py --symbol EUR/USD --days 14

    # Explicit date range
    python3 scripts/backfill_twelvedata.py --symbol XAU/USD --start-date 2026-01-01 --end-date 2026-01-31

    # All symbols, specific asset class only
    python3 scripts/backfill_twelvedata.py --asset-class forex --days 10

Notes:
    - Twelve Data Grow plan: 5000 bars max per request (≈3.5 days of 1m bars 24h/day)
    - For ranges longer than 3.5 days the script paginates automatically
    - Rate limit: 377 req/min; sleep 1s between symbols to avoid hitting it
    - Env var: TWELVEDATA_API_KEY (or TWELVE_DATA_API_KEY)

Location: ~/SS/Vanguard/scripts/backfill_twelvedata.py
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
# Bootstrap repo path + load .env
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env before importing the adapter so os.environ has the API key.
# override=True: if shell has the var set to empty string, .env wins.
from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env", override=True)

from vanguard.data_adapters.twelvedata_adapter import load_from_config, TwelveDataAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_twelvedata")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_OUTPUTSIZE    = 5000          # Twelve Data Grow plan cap per request
_MINUTES_PER_DAY   = 1440          # 24h × 60m (forex/crypto run 24h)
_INTER_SYMBOL_SLEEP = 1.0          # seconds between symbols (rate-limit pacing)
_INTER_PAGE_SLEEP   = 1.5          # seconds between paginated requests for same symbol


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

def backfill_symbol_paginated(
    adapter: TwelveDataAdapter,
    symbol: str,
    start_date: str,
    end_date: str,
) -> int:
    """
    Backfill a symbol over an arbitrary date range, paginating if needed.

    Twelve Data returns bars in reverse chronological order (newest first).
    We page backward: fetch up to 5000 bars ending at `end_date`, then
    push `end_date` back to the start of the last fetched batch.

    Returns total bars written.
    """
    total_written  = 0
    current_end    = end_date
    start_dt       = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    while True:
        logger.info("[backfill] %s — fetching up to %d bars ending %s",
                    symbol, _MAX_OUTPUTSIZE, current_end)
        written = adapter.fetch_historical(
            symbol=symbol,
            start_date=start_date,
            end_date=current_end,
            outputsize=_MAX_OUTPUTSIZE,
        )
        total_written += written
        logger.info("[backfill] %s — wrote %d bars (total so far: %d)",
                    symbol, written, total_written)

        # If fewer bars returned than the cap, we've reached the start of history
        if written < _MAX_OUTPUTSIZE:
            break

        # Estimate the earliest bar timestamp: outputsize bars at 1min each
        # Push current_end back by that many minutes to fetch the next page
        # We parse the start_date boundary to avoid infinite looping
        bars_span_seconds = written * 60
        new_end_dt = datetime.strptime(current_end, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ) - timedelta(seconds=bars_span_seconds)

        if new_end_dt <= start_dt:
            break

        current_end = new_end_dt.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(_INTER_PAGE_SLEEP)

    return total_written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backfill 1m bars from Twelve Data into vanguard_universe.db"
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of calendar days back to backfill (default: 30). Ignored if --start-date given.",
    )
    p.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Backfill a single symbol (e.g. EUR/USD). Default: all configured symbols.",
    )
    p.add_argument(
        "--asset-class",
        type=str,
        default=None,
        dest="asset_class",
        help="Restrict to one asset class (forex, index, metal, energy, crypto, agriculture).",
    )
    p.add_argument(
        "--start-date",
        type=str,
        default=None,
        dest="start_date",
        help="Start date YYYY-MM-DD (UTC). Overrides --days.",
    )
    p.add_argument(
        "--end-date",
        type=str,
        default=None,
        dest="end_date",
        help="End date YYYY-MM-DD (UTC). Default: today.",
    )
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to twelvedata_symbols.json. Default: config/twelvedata_symbols.json.",
    )
    p.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to vanguard_universe.db. Default: data/vanguard_universe.db.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would be fetched without making API calls.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    # Resolve dates
    now_utc   = datetime.now(timezone.utc)
    end_date  = args.end_date or now_utc.strftime("%Y-%m-%d")
    if args.start_date:
        start_date = args.start_date
    else:
        start_dt   = now_utc - timedelta(days=args.days)
        start_date = start_dt.strftime("%Y-%m-%d")

    logger.info("[backfill] Date range: %s → %s", start_date, end_date)

    # Diagnostic — show what the env contains BEFORE building adapter
    _k1 = os.environ.get("TWELVEDATA_API_KEY", "")
    _k2 = os.environ.get("TWELVE_DATA_API_KEY", "")
    logger.info(
        "[backfill] env: TWELVEDATA_API_KEY=%s  TWELVE_DATA_API_KEY=%s",
        f"{_k1[:4]}…{_k1[-4:]}" if len(_k1) >= 8 else repr(_k1),
        f"{_k2[:4]}…{_k2[-4:]}" if len(_k2) >= 8 else repr(_k2),
    )

    # Build adapter
    adapter = load_from_config(
        config_path=args.config,
        db_path=args.db,
    )

    if not adapter.api_key:
        logger.error(
            "[backfill] No API key found. Set TWELVE_DATA_API_KEY in .env or environment. "
            "TWELVEDATA_API_KEY=%r  TWELVE_DATA_API_KEY=%r",
            os.environ.get("TWELVEDATA_API_KEY"), os.environ.get("TWELVE_DATA_API_KEY"),
        )
        sys.exit(1)

    _k = adapter.api_key
    logger.info("[backfill] Adapter api_key: %s", f"{_k[:4]}…{_k[-4:]}" if len(_k) >= 8 else repr(_k))

    # Determine symbol list
    if args.symbol:
        # Single symbol — auto-detect asset class if not in config
        symbol_map = {args.symbol: adapter.symbols.get(args.symbol, "unknown")}
    elif args.asset_class:
        symbol_map = {
            sym: cls
            for sym, cls in adapter.symbols.items()
            if cls == args.asset_class
        }
        if not symbol_map:
            logger.error("[backfill] No symbols found for asset class '%s'", args.asset_class)
            sys.exit(1)
    else:
        symbol_map = dict(adapter.symbols)

    logger.info("[backfill] %d symbol(s) to backfill", len(symbol_map))

    if args.dry_run:
        logger.info("[backfill] DRY RUN — would backfill:")
        for sym, cls in sorted(symbol_map.items()):
            logger.info("  %-20s  %s", sym, cls)
        return

    # Run backfill
    grand_total = 0
    errors      = []

    for i, (symbol, asset_class) in enumerate(sorted(symbol_map.items()), start=1):
        logger.info("[backfill] [%d/%d] %s (%s)", i, len(symbol_map), symbol, asset_class)
        try:
            written = backfill_symbol_paginated(
                adapter=adapter,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date + " 23:59:59",
            )
            grand_total += written
            logger.info("[backfill] %s: %d bars total", symbol, written)
        except Exception as exc:
            logger.error("[backfill] %s FAILED: %s", symbol, exc)
            errors.append((symbol, str(exc)))

        if i < len(symbol_map):
            time.sleep(_INTER_SYMBOL_SLEEP)

    logger.info(
        "[backfill] Done. %d symbols processed, %d total bars written, %d errors.",
        len(symbol_map), grand_total, len(errors),
    )
    if errors:
        logger.warning("[backfill] Failed symbols:")
        for sym, err in errors:
            logger.warning("  %s: %s", sym, err)
        sys.exit(1)


if __name__ == "__main__":
    main()
