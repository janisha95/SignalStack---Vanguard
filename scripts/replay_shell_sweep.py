#!/usr/bin/env python3
"""
Replay approved Vanguard forex rows through fixed shell candidates.

This script evaluates hypothetical fixed shells (for example 4/8, 5/10, 6/12)
against approved rows from vanguard_tradeable_portfolio. It uses 1m bars and
checks which happens first within each horizon:

  - SL hit
  - TP hit
  - time-exit at the first available close at/after the horizon target

Base cut defaults to dedupe_60m, which is the current defensible thesis-chain
view from the directional replay.

Assumption on ambiguous bars:
  If both SL and TP fall inside the same 1m bar range, count it as SL-first.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


DB_PATH = Path("/Users/sjani008/SS/Vanguard/data/vanguard_universe.db")
DEFAULT_START_UTC = "2026-04-14T00:00:00Z"
DEFAULT_END_UTC = "2026-04-15T13:00:00Z"
DEFAULT_HORIZONS = (30, 60, 120)
DEFAULT_SHELLS = ("4/8", "5/10", "6/12", "7/14")
DEFAULT_BASE_DEDUPE_MINUTES = 60


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
) -> dict[tuple[str, str, str], str]:
    session_map: dict[tuple[str, str, str], str] = {}
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
        session_map[(row["cycle_ts_utc"], row["symbol"], row["profile_id"])] = str(row["session_bucket"] or "unknown")
    return session_map


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


def replay_shells(
    rows: list[ApprovedRow],
    *,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    pip_sizes: dict[str, float],
    horizons: tuple[int, ...],
    shells: list[tuple[str, float, float]],
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
                outcome = "TIME"
                ambiguous = False
                exit_dt: datetime | None = None
                exit_price: float | None = None
                for bar in symbol_bars:
                    if bar["ts"] <= cycle_dt:
                        continue
                    if bar["ts"] > target_dt:
                        break
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
                records.append(
                    {
                        "cycle_ts_utc": row.cycle_ts_utc,
                        "symbol": row.symbol,
                        "direction": row.direction,
                        "session_bucket": row.session_bucket or "unknown",
                        "shell": shell_name,
                        "stop_pips": stop_pips,
                        "tp_pips": tp_pips,
                        "horizon_minutes": horizon,
                        "entry_price": row.entry_price,
                        "sl": sl,
                        "tp": tp,
                        "exit_ts_utc": iso_utc(exit_dt),
                        "exit_price": exit_price,
                        "outcome": outcome,
                        "ambiguous_bar": int(ambiguous),
                        "signed_move_pips": round(signed_pips, 4),
                        "signed_r": round(signed_r, 4),
                    }
                )
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    by_shell_h: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        by_shell_h[str(rec["shell"])][int(rec["horizon_minutes"])].append(rec)
    out: dict[str, dict[int, dict[str, Any]]] = {}
    for shell, horizon_map in sorted(by_shell_h.items()):
        out[shell] = {}
        for horizon, rows in sorted(horizon_map.items()):
            rs = [float(r["signed_r"]) for r in rows]
            pips = [float(r["signed_move_pips"]) for r in rows]
            out[shell][horizon] = {
                "n": len(rows),
                "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4) if rows else None,
                "avg_r": round(mean(rs), 4) if rows else None,
                "median_r": round(median(rs), 4) if rows else None,
                "avg_pips": round(mean(pips), 4) if rows else None,
                "ambiguous_rate": round(sum(int(r["ambiguous_bar"]) for r in rows) / len(rows), 4) if rows else None,
                "tp_hit_rate": round(sum(1 for r in rows if r["outcome"] == "TP") / len(rows), 4) if rows else None,
                "sl_hit_rate": round(sum(1 for r in rows if r["outcome"] == "SL") / len(rows), 4) if rows else None,
                "time_exit_rate": round(sum(1 for r in rows if r["outcome"] == "TIME") / len(rows), 4) if rows else None,
            }
    return out


def print_summary(summary: dict[str, dict[int, dict[str, Any]]]) -> None:
    for shell, horizon_map in summary.items():
        print(f"\nShell {shell}")
        print("-" * (6 + len(shell)))
        print(f"{'Horizon':>8} {'N':>6} {'WR':>8} {'AvgR':>8} {'MedR':>8} {'AvgPips':>10} {'TP%':>8} {'SL%':>8} {'TIME%':>8} {'Ambig%':>8}")
        for horizon, metrics in horizon_map.items():
            wr = f"{metrics['win_rate']*100:.1f}%"
            avg_r = f"{metrics['avg_r']:.2f}"
            med_r = f"{metrics['median_r']:.2f}"
            avg_pips = f"{metrics['avg_pips']:.2f}"
            tp = f"{metrics['tp_hit_rate']*100:.1f}%"
            sl = f"{metrics['sl_hit_rate']*100:.1f}%"
            te = f"{metrics['time_exit_rate']*100:.1f}%"
            amb = f"{metrics['ambiguous_rate']*100:.1f}%"
            print(f"{horizon:>8} {metrics['n']:>6} {wr:>8} {avg_r:>8} {med_r:>8} {avg_pips:>10} {tp:>8} {sl:>8} {te:>8} {amb:>8}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay fixed shell candidates across Vanguard approved rows")
    parser.add_argument("--db-path", default=str(DB_PATH))
    parser.add_argument("--profile", default="gft_10k")
    parser.add_argument("--start-utc", default=DEFAULT_START_UTC)
    parser.add_argument("--end-utc", default=DEFAULT_END_UTC)
    parser.add_argument("--horizons", default="30,60,120")
    parser.add_argument("--shells", default="4/8,5/10,6/12,7/14")
    parser.add_argument("--base-dedupe-minutes", type=int, default=DEFAULT_BASE_DEDUPE_MINUTES)
    parser.add_argument("--out-dir", default="/Users/sjani008/SS/Vanguard/reports")
    args = parser.parse_args()

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
            session_bucket=session_map.get((row.cycle_ts_utc, row.symbol, row.account_id), "unknown"),
        )
        for row in approved
    ]
    base_rows = dedupe_first_thesis(approved, args.base_dedupe_minutes)
    pip_sizes = load_pip_sizes(con)
    bars_by_symbol = load_future_bars(
        con,
        symbols={row.symbol for row in base_rows},
        start_utc=args.start_utc,
        end_utc=end_plus,
    )
    con.close()

    records = replay_shells(
        base_rows,
        bars_by_symbol=bars_by_symbol,
        pip_sizes=pip_sizes,
        horizons=horizons,
        shells=shells,
    )
    summary = summarize(records)

    out_dir = Path(args.out_dir)
    stamp = args.start_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    end_stamp = args.end_utc.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    json_path = out_dir / f"shell_sweep_{args.profile}_{stamp}_to_{end_stamp}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "profile_id": args.profile,
                "start_utc": args.start_utc,
                "end_utc": args.end_utc,
                "base_dedupe_minutes": args.base_dedupe_minutes,
                "horizons": list(horizons),
                "shells": [shell for shell, _, _ in shells],
                "counts": {
                    "approved_rows": len(approved),
                    "base_rows": len(base_rows),
                    "records": len(records),
                },
                "summary": summary,
            },
            indent=2,
        )
    )

    print(f"Approved rows: {len(approved)}")
    print(f"Base dedupe rows ({args.base_dedupe_minutes}m): {len(base_rows)}")
    print_summary(summary)
    print(f"\nWrote JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
