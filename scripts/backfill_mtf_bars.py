#!/usr/bin/env python3
"""
backfill_mtf_bars.py — Backfill 10m, 15m, 30m bars from existing 5m data.

This is a thin wrapper around aggregate_bars_mtf.py that runs the full
backfill across all symbols and all three timeframes.

Usage:
  python3 scripts/backfill_mtf_bars.py
  python3 scripts/backfill_mtf_bars.py --asset-class forex
  python3 scripts/backfill_mtf_bars.py --dry-run

Estimated runtime: 2–4 hours depending on bar count.
Reads:  vanguard_bars_5m
Writes: vanguard_bars_10m, vanguard_bars_15m, vanguard_bars_30m
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reuse the production aggregation script — it already does the heavy lifting.
import scripts.aggregate_bars_mtf as _agg

if __name__ == "__main__":
    _agg.main()
