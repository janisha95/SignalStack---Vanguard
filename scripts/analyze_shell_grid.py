#!/usr/bin/env python3
"""
Analyze shell performance across dedupe cadence, session, direction, and timeout horizon.

For each approved Vanguard forex row:
  - apply a fixed shell
  - simulate first-hit SL/TP or timeout exit
  - compute realized R
  - compute unrealized excursion over the full horizon window:
      * MFE_R = max favorable excursion / stop_pips
      * MAE_R = max adverse excursion / stop_pips

This is intended to identify the best-performing combinations by:
  direction × session × dedupe cadence × timeout horizon × shell
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
DEFAULT_START_UTC = "2026-04-14T00:00:00Z"
DEFAULT_END_UTC = "2026-04-15T13:00:00Z"


@dataclass
class ApprovedRow:
    cycle_ts_utc: str
    account_id: str
    symbol: str
    direction: str
    entry_price: float
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
        SELECT cycle_ts_utc, account_id, symbol, direction, entry_price
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
        )
        for row in rows
    ]


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


def load_future_bars(
    con: sqlite3.Connection,
    *,
    symbols: set[str],
    start_utc: str,
    end_utc: str,
) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params = [start_utc, end_utc, *sorted(symbols)]
    sql = f"""
        SELECT symbol, bar_ts_utc, open, high, low, close
        FROM vanguard_bars_1m
        WHERE bar_ts_utc >= ?
          AND bar_ts_utc <= ?
          AND symbol IN ({placeholders})
        ORDER BY symbol, bar_ts_utc
    """
    bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in con.execute(sql, params):
        if row["close"] is None or row["high"] is None or row["low"] is None:
            continue
        bars[row["symbol"]].append(
            {
                "ts": parse_utc(row["bar_ts_utc"]),
                "open": float(row["open"]) if row["open"] is not None else None,
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    return bars


def first_bar_at_or_after(bars: list[dict[str, Any]], target_dt: datetime) -> dict[str, Any] | None:
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid]["ts"] < target_dt:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(bars):
        return None
    return bars[lo]


def parse_shells(value: str) -> list[tuple[str, float, float]]:
    shells: list[tuple[str, float, float]] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        stop, tp = item.split("/")
        shells.append((item, float(stop), float(tp)))
    return shells


def compute_contract(row: ApprovedRow, pip_size: float, stop_pips: float, tp_pips: float) -> tuple[float, float]:
    if row.direction.upper() == "LONG":
        sl = row.entry_price - stop_pips * pip_size
        tp = row.entry_price + tp_pips * pip_size
    else:
        sl = row.entry_price + stop_pips * pip_size
        tp = row.entry_price - tp_pips * pip_size
    return sl, tp


def signed_move_pips(row: ApprovedRow, future_price: float, pip_size: float) -> float:
    raw = (future_price - row.entry_price) / pip_size
    return raw if row.direction.upper() == "LONG" else -raw


def classify_bar_hit(row: ApprovedRow, high: float, low: float, sl: float, tp: float) -> tuple[str | None, bool]:
    if row.direction.upper() == "LONG":
        sl_hit = low <= sl
        tp_hit = high >= tp
    else:
        sl_hit = high >= sl
        tp_hit = low <= tp
    if sl_hit and tp_hit:
        return "SL", True
    if sl_hit:
        return "SL", False
    if tp_hit:
        return "TP", False
    return None, False


def excursion_r(row: ApprovedRow, bars: list[dict[str, Any]], pip_size: float, stop_pips: float) -> tuple[float, float]:
    if not bars:
        return 0.0, 0.0
    if row.direction.upper() == "LONG":
        mfe_pips = max((bar["high"] - row.entry_price) / pip_size for bar in bars)
        mae_pips = min((bar["low"] - row.entry_price) / pip_size for bar in bars)
    else:
        mfe_pips = max((row.entry_price - bar["low"]) / pip_size for bar in bars)
        mae_pips = min((row.entry_price - bar["high"]) / pip_size for bar in bars)
    return mfe_pips / stop_pips, mae_pips / stop_pips


def replay_grid(
    rows: list[ApprovedRow],
    *,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    pip_sizes: dict[str, float],
    horizons: tuple[int, ...],
    shells: list[tuple[str, float, float]],
    dedupe_minutes: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        symbol_bars = bars_by_symbol.get(row.symbol, [])
        if not symbol_bars:
            continue
        cycle_dt = parse_utc(row.cycle_ts_utc)
        pip_size = pip_sizes.get(row.symbol.upper(), fallback_pip_size(row.symbol))
        for shell_name, stop_pips, tp_pips in shells:
            sl, tp = compute_contract(row, pip_size, stop_pips, tp_pips)
            for horizon in horizons:
                target_dt = cycle_dt + timedelta(minutes=horizon)
                window_bars = [bar for bar in symbol_bars if cycle_dt < bar["ts"] <= target_dt]
                outcome = "TIME"
                ambiguous = False
                exit_dt: datetime | None = None
                exit_price: float | None = None
                for bar in window_bars:
                    hit, amb = classify_bar_hit(row, bar["high"], bar["low"], sl, tp)
                    if hit is not None:
                        outcome = hit
                        ambiguous = amb
                        exit_dt = bar["ts"]
                        exit_price = sl if hit == "SL" else tp
                        break
                if exit_dt is None:
                    future = first_bar_at_or_after(symbol_bars, target_dt)
                    if future is None:
                        continue
                    exit_dt = future["ts"]
                    exit_price = future["close"]
                signed_pips = signed_move_pips(row, exit_price, pip_size)
                signed_r = signed_pips / stop_pips
                mfe_r, mae_r = excursion_r(row, window_bars, pip_size, stop_pips)
                records.append(
                    {
                        "cycle_ts_utc": row.cycle_ts_utc,
                        "symbol": row.symbol,
                        "direction": row.direction,
                        "session_bucket": row.session_bucket or "unknown",
                        "dedupe_minutes": dedupe_minutes,
                        "shell": shell_name,
                        "stop_pips": stop_pips,
                        "tp_pips": tp_pips,
                        "horizon_minutes": horizon,
                        "outcome": outcome,
                        "ambiguous_bar": int(ambiguous),
                        "signed_r": round(signed_r, 4),
                        "signed_move_pips": round(signed_pips, 4),
                        "mfe_r": round(mfe_r, 4),
                        "mae_r": round(mae_r, 4),
                    }
                )
    return records


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        key = (
            rec["dedupe_minutes"],
            rec["session_bucket"],
            rec["direction"],
            rec["horizon_minutes"],
            rec["shell"],
        )
        grouped[key].append(rec)
    rows: list[dict[str, Any]] = []
    for key, group in grouped.items():
        dedupe_minutes, session_bucket, direction, horizon_minutes, shell = key
        realized = [float(r["signed_r"]) for r in group]
        mfe = [float(r["mfe_r"]) for r in group]
        mae = [float(r["mae_r"]) for r in group]
        row = {
            "dedupe_minutes": dedupe_minutes,
            "session_bucket": session_bucket,
            "direction": direction,
            "horizon_minutes": horizon_minutes,
            "shell": shell,
            "n": len(group),
            "win_rate": round(sum(1 for r in realized if r > 0) / len(realized), 4),
            "avg_realized_r": round(mean(realized), 4),
            "median_realized_r": round(median(realized), 4),
            "avg_unrealized_mfe_r": round(mean(mfe), 4),
            "median_unrealized_mfe_r": round(median(mfe), 4),
            "avg_unrealized_mae_r": round(mean(mae), 4),
            "median_unrealized_mae_r": round(median(mae), 4),
            "tp_hit_rate": round(sum(1 for r in group if r["outcome"] == "TP") / len(group), 4),
            "sl_hit_rate": round(sum(1 for r in group if r["outcome"] == "SL") / len(group), 4),
            "time_exit_rate": round(sum(1 for r in group if r["outcome"] == "TIME") / len(group), 4),
            "ambiguous_rate": round(sum(int(r["ambiguous_bar"]) for r in group) / len(group), 4),
        }
        rows.append(row)
    return sorted(
        rows,
        key=lambda r: (
            -r["avg_realized_r"],
            -r["win_rate"],
            -r["avg_unrealized_mfe_r"],
            r["median_unrealized_mae_r"],
        ),
    )


def print_best(rows: list[dict[str, Any]], *, limit: int) -> None:
    print(f"{'Dedup':>5} {'Session':>8} {'Dir':>5} {'Horz':>5} {'Shell':>8} {'N':>5} {'WR':>8} {'AvgR':>8} {'MFE_R':>8} {'MAE_R':>8} {'TIME%':>8}")
    for row in rows[:limit]:
        print(
            f"{row['dedupe_minutes']:>5} "
            f"{row['session_bucket']:>8} "
            f"{row['direction']:>5} "
            f"{row['horizon_minutes']:>5} "
            f"{row['shell']:>8} "
            f"{row['n']:>5} "
            f"{row['win_rate']*100:>7.1f}% "
            f"{row['avg_realized_r']:>8.2f} "
            f"{row['avg_unrealized_mfe_r']:>8.2f} "
            f"{row['avg_unrealized_mae_r']:>8.2f} "
            f"{row['time_exit_rate']*100:>7.1f}%"
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze shell grid across dedupe/session/direction/horizon")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--profile", default="gft_10k")
    parser.add_argument("--start-utc", default=DEFAULT_START_UTC)
    parser.add_argument("--end-utc", default=DEFAULT_END_UTC)
    parser.add_argument("--dedupe-windows", default="30,60,90,120")
    parser.add_argument("--horizons", default="30,60,120")
    parser.add_argument("--shells", default="15/30,20/40,25/50,30/60")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--out-dir", default="/Users/sjani008/SS/Vanguard/reports")
    args = parser.parse_args()

    dedupe_windows = tuple(int(x.strip()) for x in args.dedupe_windows.split(",") if x.strip())
    horizons = tuple(int(x.strip()) for x in args.horizons.split(",") if x.strip())
    shells = parse_shells(args.shells)
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
            cycle_ts_utc=row.cycle_ts_utc,
            account_id=row.account_id,
            symbol=row.symbol,
            direction=row.direction,
            entry_price=row.entry_price,
            session_bucket=resolve_session_bucket(
                session_map,
                cycle_ts_utc=row.cycle_ts_utc,
                symbol=row.symbol,
                profile_id=row.account_id,
            ),
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

    all_records: list[dict[str, Any]] = []
    for window in dedupe_windows:
        cut_rows = dedupe_first_thesis(approved, window)
        all_records.extend(
            replay_grid(
                cut_rows,
                bars_by_symbol=bars_by_symbol,
                pip_sizes=pip_sizes,
                horizons=horizons,
                shells=shells,
                dedupe_minutes=window,
            )
        )

    summary_rows = summarize(all_records)
    print(f"Approved rows: {len(approved)}")
    print_best(summary_rows, limit=args.top)

    out_dir = Path(args.out_dir)
    stamp = args.start_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    end_stamp = args.end_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    csv_path = out_dir / f"shell_grid_{args.profile}_{stamp}_to_{end_stamp}.csv"
    json_path = out_dir / f"shell_grid_{args.profile}_{stamp}_to_{end_stamp}.json"
    write_csv(csv_path, summary_rows)
    json_path.write_text(
        json.dumps(
            {
                "profile_id": args.profile,
                "start_utc": args.start_utc,
                "end_utc": args.end_utc,
                "dedupe_windows": list(dedupe_windows),
                "horizons": list(horizons),
                "shells": [shell for shell, _, _ in shells],
                "rows": summary_rows,
            },
            indent=2,
        )
    )
    print(f"\nWrote CSV: {csv_path}")
    print(f"Wrote JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
