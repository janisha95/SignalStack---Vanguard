#!/usr/bin/env python3
"""Run V5.1 tradeability from vanguard_predictions + forex pair-state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.selection.tradeability import (
    evaluate_cycle_tradeability,
    load_tradeability_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V5.1 economics-only tradeability.")
    parser.add_argument("--db", default=str(ROOT / "data" / "vanguard_universe.db"))
    parser.add_argument("--config", default=str(ROOT / "config" / "vanguard_v5_tradeability.json"))
    parser.add_argument("--cycle-ts-utc", default=None)
    parser.add_argument("--profile-id", action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    config = load_tradeability_config(args.config)
    with connect_wal(args.db) as con:
        rows = evaluate_cycle_tradeability(
            con,
            config,
            cycle_ts_utc=args.cycle_ts_utc,
            profile_ids=args.profile_id,
            persist=not args.dry_run,
        )

    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("economics_state") or "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    summary = {
        "rows": len(rows),
        "persisted": not args.dry_run,
        "counts": counts,
    }
    if args.as_json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2, sort_keys=True, default=str))
    else:
        print(
            "V5.1 tradeability: "
            f"rows={summary['rows']} persisted={summary['persisted']} counts={summary['counts']}"
        )
        for row in rows[:20]:
            metrics = row.get("metrics") or {}
            reasons = row.get("reasons") or []
            print(
                f"{row['profile_id']} {row['symbol']} {row['direction']} "
                f"{row['economics_state']} pred_pips={metrics.get('predicted_move_pips')} "
                f"after_cost={metrics.get('after_cost_pips')} reasons={','.join(reasons) or '-'}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
