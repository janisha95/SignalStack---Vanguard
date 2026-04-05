#!/usr/bin/env python3
"""
Backfill historical 1m bars from Alpaca REST API for US equities.
Aggregates to 5m bars. Ready to run — no CC build needed.

Usage:
    python3 scripts/backfill_alpaca_historical.py                     # 30 days, 2500 symbols
    python3 scripts/backfill_alpaca_historical.py --days 90           # 90 days
    python3 scripts/backfill_alpaca_historical.py --symbols AAPL,MSFT # specific symbols
    python3 scripts/backfill_alpaca_historical.py --max-symbols 500   # limit symbols
    python3 scripts/backfill_alpaca_historical.py --resume            # skip done symbols
    python3 scripts/backfill_alpaca_historical.py --dry-run           # preview only

Column conventions (actual vanguard_bars_1m / vanguard_bars_5m schema):
    bar_ts_utc  = bar END time (Alpaca t = open time → +1 min)
    data_source = "alpaca_iex"
    asset_class = "equity"
"""

import os
import sys
import time
import sqlite3
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Bootstrap: load .env so APCA_API_KEY_ID / ALPACA_KEY are available
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_alpaca")

DB_PATH = str(_REPO_ROOT / "data" / "vanguard_universe.db")
ALPACA_DATA_BASE = "https://data.alpaca.markets/v2"


def get_headers():
    key    = os.environ.get("APCA_API_KEY_ID")    or os.environ.get("ALPACA_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET")
    if not key or not secret:
        raise ValueError(
            "Alpaca credentials not found. Set APCA_API_KEY_ID + APCA_API_SECRET_KEY "
            "(or ALPACA_KEY + ALPACA_SECRET) in .env or environment."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def get_tradeable_symbols(headers, max_symbols=2500):
    resp = requests.get(
        "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity",
        headers=headers, timeout=30,
    )
    assets = resp.json()
    survivors = []
    for a in assets:
        if not a.get("tradable"):
            continue
        if a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        sym = a.get("symbol", "")
        if len(sym) > 5 or "." in sym:
            continue
        survivors.append(sym)
    survivors.sort()
    logger.info(f"Found {len(survivors)} tradeable, using {min(len(survivors), max_symbols)}")
    return survivors[:max_symbols]


def get_done_symbols(con, days):
    """Return set of symbols that already have sufficient 1m bars for --resume."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = con.execute(
            "SELECT symbol, COUNT(*) FROM vanguard_bars_1m "
            "WHERE asset_class='equity' AND bar_ts_utc >= ? "
            "GROUP BY symbol HAVING COUNT(*) > ?",
            (cutoff, days * 30),
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def fetch_bars(symbol, start, end, headers):
    """Fetch all 1m bars for symbol via Alpaca REST (paginated)."""
    all_bars = []
    token    = None
    while True:
        params = {
            "start":      start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeframe":  "1Min",
            "limit":      10000,
            "feed":       "iex",
            "adjustment": "raw",
        }
        if token:
            params["page_token"] = token
        resp = requests.get(
            f"{ALPACA_DATA_BASE}/stocks/{symbol}/bars",
            headers=headers, params=params, timeout=30,
        )
        if resp.status_code == 429:
            logger.warning(f"Rate limited on {symbol}, sleeping 60s")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for {symbol}: {resp.text[:120]}")
            break
        data  = resp.json()
        bars  = data.get("bars") or []
        if not bars:
            break
        all_bars.extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
    return all_bars


def write_1m(con, symbol, bars):
    """
    Write 1m bars to vanguard_bars_1m.

    Alpaca b["t"] is bar OPEN time (ISO-8601 UTC).
    Vanguard convention: bar_ts_utc = bar END time = open + 1 min.
    """
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for b in bars:
        open_dt  = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        bar_end  = (open_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append((
            symbol,
            bar_end,
            b["o"], b["h"], b["l"], b["c"],
            b.get("v", 0),
            b.get("n", 0),    # tick_volume = number of trades
            "equity",
            "alpaca_iex",
            now,
        ))
    con.executemany(
        """
        INSERT OR IGNORE INTO vanguard_bars_1m
            (symbol, bar_ts_utc, open, high, low, close,
             volume, tick_volume, asset_class, data_source, ingest_ts_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    return len(rows)


def aggregate_5m(con, symbol):
    """
    Aggregate 1m bars in DB to 5m and upsert into vanguard_bars_5m.

    bar_ts_utc is the bar END time. To bucket correctly:
        bar_open = bar_ts_utc - 1 min
        5m bucket end = floor(bar_open / 5min) + 5 min
    """
    df = pd.read_sql(
        "SELECT * FROM vanguard_bars_1m WHERE symbol = ? AND asset_class = 'equity' "
        "ORDER BY bar_ts_utc",
        con, params=(symbol,),
    )
    if df.empty:
        return 0

    df["bar_ts_utc"] = pd.to_datetime(df["bar_ts_utc"], utc=True)
    bar_open         = df["bar_ts_utc"] - pd.Timedelta(minutes=1)
    df["grp"]        = bar_open.dt.floor("5min") + pd.Timedelta(minutes=5)

    agg = df.groupby("grp").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()

    now  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        (
            symbol,
            r["grp"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            r["open"], r["high"], r["low"], r["close"],
            int(r["volume"]),
            "equity",
            "alpaca_iex",
        )
        for _, r in agg.iterrows()
    ]
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_bars_5m
            (symbol, bar_ts_utc, open, high, low, close, volume,
             asset_class, data_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",          type=int, default=30)
    ap.add_argument("--symbols",       type=str, default=None)
    ap.add_argument("--max-symbols",   type=int, default=2500)
    ap.add_argument("--resume",        action="store_true")
    ap.add_argument("--skip-aggregate", action="store_true")
    ap.add_argument("--dry-run",       action="store_true")
    args = ap.parse_args()

    headers = get_headers()
    con     = sqlite3.connect(DB_PATH)

    symbols = args.symbols.split(",") if args.symbols else get_tradeable_symbols(headers, args.max_symbols)

    if args.resume:
        done   = get_done_symbols(con, args.days)
        before = len(symbols)
        symbols = [s for s in symbols if s not in done]
        logger.info(f"Resume: {len(done)} done, {len(symbols)} remaining (was {before})")

    if args.dry_run:
        logger.info(f"DRY RUN: {len(symbols)} symbols × {args.days} days")
        con.close()
        return

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    logger.info(f"Backfilling {len(symbols)} symbols, {start.date()} → {end.date()}")

    req_count = 0
    min_start = time.time()
    total_1m  = 0
    total_5m  = 0
    errors    = 0

    for i, sym in enumerate(symbols):
        try:
            # Rate limit: 200/min, leave headroom
            req_count += 1
            if req_count >= 180:
                elapsed = time.time() - min_start
                if elapsed < 60:
                    sl = 61 - elapsed
                    logger.info(f"Rate pause {sl:.0f}s...")
                    time.sleep(sl)
                req_count = 0
                min_start = time.time()

            bars = fetch_bars(sym, start, end, headers)
            if bars:
                n1        = write_1m(con, sym, bars)
                total_1m += n1
                if not args.skip_aggregate:
                    n5        = aggregate_5m(con, sym)
                    total_5m += n5

            if (i + 1) % 50 == 0:
                logger.info(f"[{i+1}/{len(symbols)}] {total_1m:,} 1m bars, {total_5m:,} 5m bars, {errors} errors")

        except Exception as e:
            logger.error(f"Error {sym}: {e}")
            errors += 1

    con.close()
    logger.info(f"DONE: {len(symbols)} symbols, {total_1m:,} 1m bars, {total_5m:,} 5m bars, {errors} errors")


if __name__ == "__main__":
    main()
