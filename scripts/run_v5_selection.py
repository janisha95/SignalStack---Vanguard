#!/usr/bin/env python3
"""Run V5.2 selection from vanguard_v5_tradeability."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.selection.shortlisting import evaluate_cycle_selection


DEFAULT_CONFIG = ROOT / "config" / "vanguard_v5_selection.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V5.2 selection and optional shortlist mirror.")
    parser.add_argument("--db", default=str(ROOT / "data" / "vanguard_universe.db"))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cycle-ts-utc", default=None)
    parser.add_argument("--profile-id", action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mirror-shortlist", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    config = json.loads(Path(args.config).expanduser().read_text())
    with connect_wal(args.db) as con:
        rows = evaluate_cycle_selection(
            con,
            config,
            cycle_ts_utc=args.cycle_ts_utc,
            profile_ids=args.profile_id,
            persist=not args.dry_run,
            mirror_shortlist=args.mirror_shortlist and not args.dry_run,
        )

    counts: dict[str, int] = {}
    selected = 0
    for row in rows:
        state = str(row.get("selection_state") or "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
        selected += int(row.get("selected") or 0)
    summary = {
        "rows": len(rows),
        "selected": selected,
        "persisted": not args.dry_run,
        "mirrored_shortlist": args.mirror_shortlist and not args.dry_run,
        "counts": counts,
    }
    if args.as_json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2, sort_keys=True, default=str))
    else:
        print(
            "V5.2 selection: "
            f"rows={summary['rows']} selected={summary['selected']} "
            f"persisted={summary['persisted']} mirrored={summary['mirrored_shortlist']} "
            f"counts={summary['counts']}"
        )
        for row in rows[:20]:
            print(
                f"{row['profile_id']} {row['symbol']} {row['direction']} "
                f"{row['selection_state']} selected={row['selected']} "
                f"tier={row['route_tier']} label={row['display_label']} "
                f"rank={row['selection_rank']} reason={row['selection_reason']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
