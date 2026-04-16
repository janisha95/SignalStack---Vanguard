#!/usr/bin/env python3
"""Build vanguard_forex_pair_state from latest live context rows."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vanguard.selection.forex_pair_state import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_DB,
    build_forex_pair_state,
    load_pair_state_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Vanguard forex pair-state rows.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cycle-ts-utc", default=None)
    parser.add_argument("--profile-id", action="append", default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols to restrict the build.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print built rows as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = None
    if args.symbols:
        symbols = [s.strip().replace("/", "").upper() for s in args.symbols.split(",") if s.strip()]
    rows = build_forex_pair_state(
        db_path=args.db,
        pair_state_config=load_pair_state_config(args.config),
        cycle_ts_utc=args.cycle_ts_utc,
        profile_ids=args.profile_id,
        symbols=symbols,
        persist=not args.dry_run,
    )
    ok = sum(1 for row in rows if row["source_status"] == "OK")
    missing = len(rows) - ok
    print(f"forex_pair_state rows={len(rows)} ok={ok} missing_or_error={missing} persisted={not args.dry_run}")
    by_profile: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_profile.setdefault(row["profile_id"], {"rows": 0, "ok": 0})
        bucket["rows"] += 1
        if row["source_status"] == "OK":
            bucket["ok"] += 1
    for profile_id, stats in sorted(by_profile.items()):
        print(f"profile={profile_id} rows={stats['rows']} ok={stats['ok']}")
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
