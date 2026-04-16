#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

import sys

sys.path.insert(0, str(_REPO_ROOT))

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.execution.timeout_policy import refresh_timeout_policy_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Vanguard timeout-policy analysis tables")
    parser.add_argument("--db-path", default=get_shadow_db_path())
    parser.add_argument("--trade-id", action="append", default=[])
    parser.add_argument("--no-shell-replays", action="store_true")
    args = parser.parse_args()

    refresh_timeout_policy_data(
        args.db_path,
        trade_ids=args.trade_id or None,
        refresh_shell_replays_flag=not args.no_shell_replays,
    )

    with sqlite3.connect(args.db_path) as con:
        rows = con.execute(
            """
            SELECT session_bucket, dedupe_mode, timeout_minutes, n, wins, losses,
                   ROUND(win_rate * 100.0, 1) AS wr_pct,
                   ROUND(avg_move_pips, 3) AS avg_pips,
                   ROUND(profit_factor, 3) AS pf
            FROM vanguard_timeout_policy_summary
            ORDER BY session_bucket, dedupe_mode, timeout_minutes
            """
        ).fetchall()
    print(f"Refreshed timeout policy data in {args.db_path}")
    for row in rows:
        print(
            f"{row[0]} dedupe={row[1]} timeout={row[2]}m "
            f"n={row[3]} wins={row[4]} losses={row[5]} wr={row[6]}% avg={row[7]} pf={row[8]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
