#!/usr/bin/env python3
"""
vanguard_training_backfill_fast.py — compatibility wrapper for V4A backfill.

Phase 1 standardizes V4A on the live vanguard_training_data contract and
per-asset-class TBM configuration. The earlier experimental "fast" script had
drifted from the canonical schema, so this wrapper now delegates to the main
V4A backfill implementation while preserving the familiar CLI entrypoint.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stages.vanguard_training_backfill import run

DEFAULT_DB = ROOT / "data" / "vanguard_universe.db"
DEFAULT_TBM_CONFIG = ROOT / "config" / "vanguard_tbm_params.json"


def run_fast_backfill(
    db_path: Path = DEFAULT_DB,
    days: int | None = None,
    workers: int = 6,
    batch_size: int = 250,
    symbols: str | None = None,
    resume: bool = True,
    horizon: int | None = None,
    dry_run: bool = False,
    asset_class: str | None = None,
    tbm_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Compatibility entrypoint for the old fast backfill CLI.

    batch_size is accepted for CLI compatibility but no longer used here.
    """
    _ = batch_size
    start_date = None
    if days is not None:
        start_date = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")

    symbol_list = None
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]

    return run(
        symbols=symbol_list,
        workers=workers,
        dry_run=dry_run,
        full_rebuild=not resume,
        start_date=start_date,
        end_date=None,
        asset_class=asset_class,
        horizon_bars=horizon,
        tp_pct=None,
        sl_pct=None,
        tbm_config_path=tbm_config_path or DEFAULT_TBM_CONFIG,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast Vanguard training backfill")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB))
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers")
    parser.add_argument("--batch-size", type=int, default=250, help="Unused compatibility flag")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated tickers")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume from existing rows")
    parser.add_argument("--no-resume", action="store_true", help="Recompute everything")
    parser.add_argument("--horizon", type=int, default=None, help="Override TBM horizon")
    parser.add_argument("--asset-class", type=str, default=None, help="Restrict to one asset class")
    parser.add_argument("--tbm-config", type=str, default=str(DEFAULT_TBM_CONFIG),
                        help="Path to TBM config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")

    args = parser.parse_args()
    resume = not args.no_resume
    run_fast_backfill(
        db_path=Path(args.db),
        days=args.days,
        workers=args.workers,
        batch_size=args.batch_size,
        symbols=args.symbols,
        resume=resume,
        horizon=args.horizon,
        dry_run=args.dry_run,
        asset_class=args.asset_class,
        tbm_config_path=args.tbm_config,
    )


if __name__ == "__main__":
    main()
