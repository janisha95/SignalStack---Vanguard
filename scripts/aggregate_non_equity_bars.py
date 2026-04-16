#!/usr/bin/env python3
"""
Aggregate historical non-equity 1m bars into 5m and 1h bars.

Reads existing rows from vanguard_bars_1m and materializes missing higher-
timeframe bars into vanguard_bars_5m and vanguard_bars_1h using Vanguard's
bar end-time convention.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "vanguard_universe.db"
DEFAULT_ASSET_CLASSES = ("forex", "crypto", "metal", "commodity")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vanguard.helpers.bars import aggregate_1m_to_5m, aggregate_5m_to_1h
from vanguard.helpers.bars import parse_utc


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _load_symbols(con: sqlite3.Connection, asset_classes: tuple[str, ...]) -> list[tuple[str, str, str]]:
    placeholders = ",".join("?" for _ in asset_classes)
    try:
        rows = con.execute(
            f"""
            SELECT symbol, asset_class, COALESCE(data_source, 'unknown') AS data_source
            FROM vanguard_universe_members
            WHERE is_active = 1 AND asset_class IN ({placeholders})
            ORDER BY asset_class, symbol
            """,
            asset_classes,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        rows = con.execute(
            f"""
            SELECT DISTINCT symbol, asset_class, COALESCE(data_source, 'unknown') AS data_source
            FROM vanguard_bars_1m
            WHERE asset_class IN ({placeholders})
            ORDER BY asset_class, symbol
            """,
            asset_classes,
        ).fetchall()
    return [(row["symbol"], row["asset_class"], row["data_source"]) for row in rows]


def _load_1m_bars(
    con: sqlite3.Connection,
    symbol: str,
    asset_class: str,
    data_source: str,
    start_ts_utc: str | None = None,
) -> list[dict]:
    query = """
        SELECT symbol, bar_ts_utc, open, high, low, close, volume,
               asset_class, data_source
        FROM vanguard_bars_1m
        WHERE symbol = ? AND asset_class = ? AND data_source = ?
    """
    params: list[str] = [symbol, asset_class, data_source]
    if start_ts_utc:
        query += " AND bar_ts_utc >= ?"
        params.append(start_ts_utc)
    query += " ORDER BY bar_ts_utc"
    rows = con.execute(query, params).fetchall()
    if rows:
        return [dict(row) for row in rows]

    # Fallback: universe member source can differ from the actual historical source.
    query = """
        SELECT symbol, bar_ts_utc, open, high, low, close, volume,
               asset_class, data_source
        FROM vanguard_bars_1m
        WHERE symbol = ? AND asset_class = ?
    """
    params = [symbol, asset_class]
    if start_ts_utc:
        query += " AND bar_ts_utc >= ?"
        params.append(start_ts_utc)
    query += " ORDER BY bar_ts_utc"
    rows = con.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _load_5m_bars(
    con: sqlite3.Connection,
    symbol: str,
    asset_class: str,
    data_source: str,
    start_ts_utc: str | None = None,
) -> list[dict]:
    query = """
        SELECT symbol, bar_ts_utc, open, high, low, close, volume,
               asset_class, data_source
        FROM vanguard_bars_5m
        WHERE symbol = ? AND asset_class = ? AND data_source = ?
    """
    params: list[str] = [symbol, asset_class, data_source]
    if start_ts_utc:
        query += " AND bar_ts_utc >= ?"
        params.append(start_ts_utc)
    query += " ORDER BY bar_ts_utc"
    rows = con.execute(query, params).fetchall()
    if rows:
        return [dict(row) for row in rows]

    # Fallback: universe member source can differ from the actual historical source.
    query = """
        SELECT symbol, bar_ts_utc, open, high, low, close, volume,
               asset_class, data_source
        FROM vanguard_bars_5m
        WHERE symbol = ? AND asset_class = ?
    """
    params = [symbol, asset_class]
    if start_ts_utc:
        query += " AND bar_ts_utc >= ?"
        params.append(start_ts_utc)
    query += " ORDER BY bar_ts_utc"
    rows = con.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _max_bar_ts(
    con: sqlite3.Connection,
    table: str,
    symbol: str,
    asset_class: str | None = None,
    data_source: str | None = None,
) -> str | None:
    query = f"SELECT MAX(bar_ts_utc) FROM {table} WHERE symbol = ?"
    params: list[str] = [symbol]
    if asset_class is not None:
        query += " AND asset_class = ?"
        params.append(asset_class)
    if data_source is not None:
        query += " AND data_source = ?"
        params.append(data_source)
    return con.execute(query, params).fetchone()[0]


def _offset_iso(ts_utc: str | None, *, minutes: int) -> str | None:
    if not ts_utc:
        return None
    return (parse_utc(ts_utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_5m(con: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        """
        INSERT OR IGNORE INTO vanguard_bars_5m
            (symbol, bar_ts_utc, open, high, low, close, volume, asset_class, data_source)
        VALUES
            (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume, :asset_class, :data_source)
        """,
        rows,
    )
    return con.total_changes - before


def _insert_1h(con: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        """
        INSERT OR IGNORE INTO vanguard_bars_1h
            (symbol, bar_ts_utc, open, high, low, close, volume)
        VALUES
            (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume)
        """,
        rows,
    )
    return con.total_changes - before


def aggregate_asset_classes(
    db_path: Path = DB_PATH,
    asset_classes: tuple[str, ...] = DEFAULT_ASSET_CLASSES,
    dry_run: bool = False,
    full_rebuild: bool = False,
) -> dict[str, int]:
    con = _connect(db_path)
    try:
        symbols = _load_symbols(con, asset_classes)
        print(f"Symbols to process: {len(symbols)}")
        totals = {
            "symbols": len(symbols),
            "bars_1m": 0,
            "bars_5m_attempted": 0,
            "bars_5m_inserted": 0,
            "bars_1h_attempted": 0,
            "bars_1h_inserted": 0,
        }

        for index, (symbol, asset_class, data_source) in enumerate(symbols, start=1):
            start_1m_ts = None
            start_5m_ts = None
            if not full_rebuild:
                max_5m_ts = _max_bar_ts(con, "vanguard_bars_5m", symbol, asset_class, data_source)
                max_1h_ts = _max_bar_ts(con, "vanguard_bars_1h", symbol)
                if max_5m_ts:
                    start_1m_ts = _offset_iso(max_5m_ts, minutes=10)
                if max_1h_ts:
                    start_5m_ts = _offset_iso(max_1h_ts, minutes=65)

            bars_1m = _load_1m_bars(con, symbol, asset_class, data_source, start_ts_utc=start_1m_ts)
            totals["bars_1m"] += len(bars_1m)
            rows_5m = aggregate_1m_to_5m(bars_1m)
            bars_5m_for_1h = _load_5m_bars(
                con,
                symbol,
                asset_class,
                data_source,
                start_ts_utc=start_5m_ts,
            )
            rows_1h = aggregate_5m_to_1h(bars_5m_for_1h)
            totals["bars_5m_attempted"] += len(rows_5m)
            totals["bars_1h_attempted"] += len(rows_1h)

            inserted_5m = 0
            inserted_1h = 0
            if not dry_run:
                inserted_5m = _insert_5m(con, rows_5m)
                inserted_1h = _insert_1h(con, rows_1h)
                con.commit()

            totals["bars_5m_inserted"] += inserted_5m
            totals["bars_1h_inserted"] += inserted_1h
            print(
                f"[{index:>3}/{len(symbols)}] {asset_class:<9} {symbol:<12} "
                f"1m={len(bars_1m):>8,} -> 5m={len(rows_5m):>7,} (+{inserted_5m:>7,}) "
                f"-> 1h={len(rows_1h):>6,} (+{inserted_1h:>6,})"
            )

        return totals
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate non-equity 1m bars into 5m and 1h bars")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument(
        "--asset-classes",
        default=",".join(DEFAULT_ASSET_CLASSES),
        help="Comma-separated asset classes to aggregate",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-rebuild", action="store_true")
    args = parser.parse_args()

    asset_classes = tuple(part.strip() for part in args.asset_classes.split(",") if part.strip())
    totals = aggregate_asset_classes(
        db_path=Path(args.db),
        asset_classes=asset_classes,
        dry_run=args.dry_run,
        full_rebuild=args.full_rebuild,
    )
    print("\nTotals:")
    for key, value in totals.items():
        print(f"  {key}: {value:,}")


if __name__ == "__main__":
    main()
