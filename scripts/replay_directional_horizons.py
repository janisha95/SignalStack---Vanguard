#!/usr/bin/env python3
"""
Replay approved Vanguard forex rows across multiple forward horizons.

Stage 1 only: no shell logic. This script measures raw directional followthrough
after approval, using the approved entry price from vanguard_tradeable_portfolio
and the first available 1m bar at or after each requested horizon target.

Outputs multiple cuts:
  - all_approved
  - deduped first-thesis for each requested dedupe window

Same-side thesis chains reset on:
  - opposite-side approval for the same symbol
  - gap > dedupe_window_minutes

Also reports session-based performance using vanguard_forex_pair_state.session_bucket.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


DB_PATH = Path("/Users/sjani008/SS/Vanguard/data/vanguard_universe.db")
DEFAULT_START_UTC = "2026-04-14T00:00:00Z"  # Apr 13 20:00 ET
DEFAULT_END_UTC = "2026-04-15T13:00:00Z"    # Apr 15 09:00 ET
DEFAULT_HORIZONS = (10, 15, 30, 60, 120, 180, 240, 300, 360)
DEFAULT_DEDUPE_WINDOWS = (0, 30, 60, 90, 120)


@dataclass
class ApprovedRow:
    cycle_ts_utc: str
    account_id: str
    symbol: str
    direction: str
    entry_price: float
    stop_price: float | None
    tp_price: float | None
    risk_dollars: float | None
    rank: int | None
    model_source: str | None
    session_bucket: str | None = None


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_approved_rows(
    con: sqlite3.Connection,
    *,
    profile_id: str,
    start_utc: str,
    end_utc: str,
) -> list[ApprovedRow]:
    rows = con.execute(
        """
        SELECT cycle_ts_utc, account_id, symbol, direction, entry_price, stop_price,
               tp_price, risk_dollars, rank, model_source
        FROM vanguard_tradeable_portfolio
        WHERE account_id = ?
          AND status = 'APPROVED'
          AND cycle_ts_utc >= ?
          AND cycle_ts_utc <= ?
        ORDER BY cycle_ts_utc, symbol, direction
        """,
        (profile_id, start_utc, end_utc),
    ).fetchall()
    return [
        ApprovedRow(
            cycle_ts_utc=row["cycle_ts_utc"],
            account_id=row["account_id"],
            symbol=row["symbol"],
            direction=row["direction"],
            entry_price=float(row["entry_price"]),
            stop_price=float(row["stop_price"]) if row["stop_price"] is not None else None,
            tp_price=float(row["tp_price"]) if row["tp_price"] is not None else None,
            risk_dollars=float(row["risk_dollars"]) if row["risk_dollars"] is not None else None,
            rank=int(row["rank"]) if row["rank"] is not None else None,
            model_source=row["model_source"],
        )
        for row in rows
    ]


def load_pip_sizes(con: sqlite3.Connection) -> dict[str, float]:
    pip_sizes: dict[str, float] = {}
    for row in con.execute(
        """
        SELECT symbol, pip_size
        FROM vanguard_context_symbol_specs
        WHERE pip_size IS NOT NULL
        """
    ):
        symbol = str(row["symbol"] or "").upper()
        pip = float(row["pip_size"])
        if symbol and pip > 0:
            pip_sizes[symbol] = pip
    for row in con.execute(
        """
        SELECT symbol, pip_size
        FROM vanguard_symbol_specs
        WHERE pip_size IS NOT NULL
        """
    ):
        symbol = str(row["symbol"] or "").upper()
        pip = float(row["pip_size"])
        if symbol and pip > 0:
            pip_sizes.setdefault(symbol, pip)
    return pip_sizes


def fallback_pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


def dedupe_first_thesis(rows: list[ApprovedRow], chain_gap_minutes: int) -> list[ApprovedRow]:
    deduped: list[ApprovedRow] = []
    last_seen: dict[str, tuple[str, datetime]] = {}
    gap = timedelta(minutes=chain_gap_minutes)

    for row in rows:
        cycle_dt = parse_utc(row.cycle_ts_utc)
        prev = last_seen.get(row.symbol)
        keep = True
        if prev is not None:
            prev_dir, prev_dt = prev
            if row.direction == prev_dir and cycle_dt - prev_dt <= gap:
                keep = False
        if keep:
            deduped.append(row)
        last_seen[row.symbol] = (row.direction, cycle_dt)
    return deduped


def load_session_buckets(
    con: sqlite3.Connection,
    *,
    profile_id: str,
    start_utc: str,
    end_utc: str,
) -> dict[tuple[str, str], list[tuple[datetime, str]]]:
    session_map: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)
    for row in con.execute(
        """
        SELECT cycle_ts_utc, symbol, profile_id, session_bucket
        FROM vanguard_forex_pair_state
        WHERE profile_id = ?
          AND cycle_ts_utc >= ?
          AND cycle_ts_utc <= ?
        """,
        (profile_id, start_utc, end_utc),
    ):
        session_map[(row["symbol"], row["profile_id"])].append(
            (parse_utc(row["cycle_ts_utc"]), str(row["session_bucket"] or "unknown"))
        )
    return session_map


def resolve_session_bucket(
    session_map: dict[tuple[str, str], list[tuple[datetime, str]]],
    *,
    cycle_ts_utc: str,
    symbol: str,
    profile_id: str,
    tolerance_seconds: int = 60,
) -> str:
    series = session_map.get((symbol, profile_id))
    if not series:
        return "unknown"
    target = parse_utc(cycle_ts_utc)
    timestamps = [ts for ts, _ in series]
    idx = bisect_left(timestamps, target)
    candidates: list[tuple[float, str]] = []
    if idx < len(series):
        ts, bucket = series[idx]
        candidates.append((abs((ts - target).total_seconds()), bucket))
    if idx > 0:
        ts, bucket = series[idx - 1]
        candidates.append((abs((ts - target).total_seconds()), bucket))
    if not candidates:
        return "unknown"
    best_seconds, best_bucket = min(candidates, key=lambda item: item[0])
    return best_bucket if best_seconds <= tolerance_seconds else "unknown"


def load_future_bars(
    con: sqlite3.Connection,
    *,
    symbols: set[str],
    start_utc: str,
    end_utc: str,
) -> dict[str, list[tuple[datetime, float]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params = [start_utc, end_utc, *sorted(symbols)]
    sql = f"""
        SELECT symbol, bar_ts_utc, close
        FROM vanguard_bars_1m
        WHERE bar_ts_utc >= ?
          AND bar_ts_utc <= ?
          AND symbol IN ({placeholders})
        ORDER BY symbol, bar_ts_utc
    """
    bars: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in con.execute(sql, params):
        if row["close"] is None:
            continue
        bars[row["symbol"]].append((parse_utc(row["bar_ts_utc"]), float(row["close"])))
    return bars


def first_bar_at_or_after(
    bars: list[tuple[datetime, float]],
    target_dt: datetime,
) -> tuple[datetime, float] | None:
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] < target_dt:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(bars):
        return None
    return bars[lo]


def signed_move_pips(row: ApprovedRow, future_price: float, pip_size: float) -> float:
    raw = (future_price - row.entry_price) / pip_size
    return raw if row.direction.upper() == "LONG" else -raw


def summarize(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_h: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_h[int(rec["horizon_minutes"])].append(rec)
    out: dict[int, dict[str, Any]] = {}
    for horizon, rows in sorted(by_h.items()):
        moves = [float(r["signed_move_pips"]) for r in rows]
        positive = [m for m in moves if m > 0]
        out[horizon] = {
            "n": len(rows),
            "resolved": len(rows),
            "win_rate": round(sum(1 for m in moves if m > 0) / len(moves), 4) if rows else None,
            "avg_move_pips": round(mean(moves), 4) if rows else None,
            "median_move_pips": round(median(moves), 4) if rows else None,
            "avg_positive_move_pips": round(mean(positive), 4) if positive else None,
            "avg_bar_delay_minutes": round(mean(float(r["bar_delay_minutes"]) for r in rows), 4) if rows else None,
        }
    return out


def summarize_by_session(records: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        buckets[str(rec.get("session_bucket") or "unknown")].append(rec)
    return {session: summarize(rows) for session, rows in sorted(buckets.items())}


def replay_rows(
    rows: list[ApprovedRow],
    *,
    bars_by_symbol: dict[str, list[tuple[datetime, float]]],
    pip_sizes: dict[str, float],
    horizons: tuple[int, ...],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        symbol_bars = bars_by_symbol.get(row.symbol, [])
        if not symbol_bars:
            continue
        cycle_dt = parse_utc(row.cycle_ts_utc)
        pip_size = pip_sizes.get(row.symbol.upper(), fallback_pip_size(row.symbol))
        for horizon in horizons:
            target_dt = cycle_dt + timedelta(minutes=horizon)
            future = first_bar_at_or_after(symbol_bars, target_dt)
            if future is None:
                continue
            future_dt, future_price = future
            records.append({
                "cycle_ts_utc": row.cycle_ts_utc,
                "symbol": row.symbol,
                "direction": row.direction,
                "session_bucket": row.session_bucket or "unknown",
                "entry_price": row.entry_price,
                "future_bar_ts_utc": iso_utc(future_dt),
                "future_price": future_price,
                "horizon_minutes": horizon,
                "bar_delay_minutes": round((future_dt - target_dt).total_seconds() / 60.0, 4),
                "signed_move_pips": round(signed_move_pips(row, future_price, pip_size), 4),
                "pip_size": pip_size,
                "rank": row.rank,
                "model_source": row.model_source,
            })
    return records


def print_summary(title: str, summary: dict[int, dict[str, Any]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    print(f"{'Horizon':>8} {'N':>6} {'WR':>8} {'AvgPips':>10} {'MedPips':>10} {'AvgDelay':>10}")
    for horizon, metrics in summary.items():
        wr = f"{metrics['win_rate']*100:.1f}%" if metrics["win_rate"] is not None else "—"
        avg = f"{metrics['avg_move_pips']:.2f}" if metrics["avg_move_pips"] is not None else "—"
        med = f"{metrics['median_move_pips']:.2f}" if metrics["median_move_pips"] is not None else "—"
        delay = f"{metrics['avg_bar_delay_minutes']:.2f}" if metrics["avg_bar_delay_minutes"] is not None else "—"
        print(f"{horizon:>8} {metrics['n']:>6} {wr:>8} {avg:>10} {med:>10} {delay:>10}")


def print_session_summary(title: str, session_summary: dict[str, dict[int, dict[str, Any]]]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for session, summary in session_summary.items():
        print(f"\n[{session}]")
        print(f"{'Horizon':>8} {'N':>6} {'WR':>8} {'AvgPips':>10} {'MedPips':>10}")
        for horizon, metrics in summary.items():
            wr = f"{metrics['win_rate']*100:.1f}%" if metrics["win_rate"] is not None else "—"
            avg = f"{metrics['avg_move_pips']:.2f}" if metrics["avg_move_pips"] is not None else "—"
            med = f"{metrics['median_move_pips']:.2f}" if metrics["median_move_pips"] is not None else "—"
            print(f"{horizon:>8} {metrics['n']:>6} {wr:>8} {avg:>10} {med:>10}")


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay approved Vanguard rows across directional horizons")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--profile", default="gft_10k")
    parser.add_argument("--start-utc", default=DEFAULT_START_UTC)
    parser.add_argument("--end-utc", default=DEFAULT_END_UTC)
    parser.add_argument("--dedupe-windows", default="0,30,60,90,120")
    parser.add_argument("--horizons", default="10,15,30,60,120,180,240,300,360")
    parser.add_argument("--out-dir", default="/Users/sjani008/SS/Vanguard/reports")
    args = parser.parse_args()

    horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
    dedupe_windows = tuple(int(x.strip()) for x in args.dedupe_windows.split(",") if x.strip())
    end_plus = iso_utc(parse_utc(args.end_utc) + timedelta(minutes=max(horizons) + 30))

    con = sqlite3.connect(args.db_path)
    con.row_factory = sqlite3.Row

    approved = load_approved_rows(
        con,
        profile_id=args.profile,
        start_utc=args.start_utc,
        end_utc=args.end_utc,
    )
    session_map = load_session_buckets(
        con,
        profile_id=args.profile,
        start_utc=args.start_utc,
        end_utc=args.end_utc,
    )
    approved = [
        ApprovedRow(
            **{
                **row.__dict__,
                "session_bucket": resolve_session_bucket(
                    session_map,
                    cycle_ts_utc=row.cycle_ts_utc,
                    symbol=row.symbol,
                    profile_id=row.account_id,
                ),
            }
        )
        for row in approved
    ]
    pip_sizes = load_pip_sizes(con)
    bars_by_symbol = load_future_bars(
        con,
        symbols={row.symbol for row in approved},
        start_utc=args.start_utc,
        end_utc=end_plus,
    )
    con.close()

    cuts: dict[str, list[ApprovedRow]] = {"all_approved": approved}
    for window in dedupe_windows:
        if window <= 0:
            continue
        cuts[f"dedupe_{window}m"] = dedupe_first_thesis(approved, window)

    cut_records: dict[str, list[dict[str, Any]]] = {}
    cut_summaries: dict[str, dict[int, dict[str, Any]]] = {}
    cut_session_summaries: dict[str, dict[str, dict[int, dict[str, Any]]]] = {}
    for name, rows in cuts.items():
        records = replay_rows(
            rows,
            bars_by_symbol=bars_by_symbol,
            pip_sizes=pip_sizes,
            horizons=horizons,
        )
        cut_records[name] = records
        cut_summaries[name] = summarize(records)
        cut_session_summaries[name] = summarize_by_session(records)

    payload = {
        "profile_id": args.profile,
        "start_utc": args.start_utc,
        "end_utc": args.end_utc,
        "dedupe_windows": list(dedupe_windows),
        "horizons": list(horizons),
        "counts": {
            "approved_rows": len(approved),
            "symbols": len({row.symbol for row in approved}),
            "rows_by_cut": {name: len(rows) for name, rows in cuts.items()},
        },
        "summaries": cut_summaries,
        "session_summaries": cut_session_summaries,
    }

    out_dir = Path(args.out_dir)
    stamp = args.start_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    end_stamp = args.end_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    json_path = out_dir / f"directional_replay_{args.profile}_{stamp}_to_{end_stamp}.json"
    csv_paths: dict[str, Path] = {
        name: out_dir / f"directional_replay_{name}_{args.profile}_{stamp}_to_{end_stamp}.csv"
        for name in cut_records
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2))
    for name, records in cut_records.items():
        write_csv(csv_paths[name], records)

    print(f"Approved rows: {len(approved)}")
    print(f"Symbols:       {len({row.symbol for row in approved})}")
    for name, rows in cuts.items():
        print(f"{name}: {len(rows)} rows")
    for name, summary in cut_summaries.items():
        print_summary(name.replace("_", " ").title(), summary)
    # Session analysis on the core cuts that matter for comparison.
    for name in ["all_approved", *[n for n in cut_records if n != "all_approved"]]:
        print_session_summary(f"{name.replace('_', ' ').title()} by Session", cut_session_summaries[name])
    print(f"\nWrote JSON: {json_path}")
    for name, path in csv_paths.items():
        print(f"Wrote CSV ({name}): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
