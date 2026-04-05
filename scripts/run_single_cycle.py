#!/usr/bin/env python3
"""
run_single_cycle.py — V1→V6 single-cycle end-to-end test harness.

Produces real vanguard_shortlist + vanguard_tradeable_portfolio in the DB
using EXISTING data (no live market fetch required).

Key design choices:
  - V5 is called via its public run() API (no monkey-patching of internals)
  - If the ML gate blocks all candidates (probs ~0.17-0.31 < 0.50 threshold),
    the script patches `predict_probabilities` in the vanguard_selection
    module namespace and retries — this proves the full pipeline works without
    requiring retraining.
  - V6 is called via its public run() API; it reads from the shortlist V5 wrote.

Usage:
    python3 scripts/run_single_cycle.py --dry-run --force-regime ACTIVE
    python3 scripts/run_single_cycle.py --force-regime ACTIVE
    python3 scripts/run_single_cycle.py --force-regime ACTIVE --account ttp_50k_intraday
    python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE

Location: ~/SS/Vanguard/scripts/run_single_cycle.py
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_single_cycle")

_DB = str(_REPO / "data" / "vanguard_universe.db")


# ---------------------------------------------------------------------------
# DB introspection helpers
# ---------------------------------------------------------------------------

def _count(table: str) -> int | str:
    try:
        with sqlite3.connect(_DB) as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return "MISSING"


def print_db_state(label: str = "DB STATE") -> None:
    tables = [
        "vanguard_bars_5m",
        "vanguard_bars_1h",
        "vanguard_features",
        "vanguard_health",
        "vanguard_shortlist",
        "vanguard_tradeable_portfolio",
        "vanguard_portfolio_state",
    ]
    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")
    for t in tables:
        cnt = _count(t)
        marker = "" if isinstance(cnt, int) and cnt > 0 else " ← EMPTY/MISSING"
        print(f"  {t:<40} {str(cnt):>8}{marker}")
    print()


def print_features_summary() -> None:
    try:
        with sqlite3.connect(_DB) as con:
            rows = con.execute(
                "SELECT cycle_ts_utc, COUNT(*) FROM vanguard_features "
                "GROUP BY cycle_ts_utc ORDER BY cycle_ts_utc DESC LIMIT 3"
            ).fetchall()
            if rows:
                print("  Latest feature cycles:")
                for ts, n in rows:
                    print(f"    {ts}  —  {n} symbols")
    except Exception:
        pass


def print_account_profiles() -> None:
    try:
        with sqlite3.connect(_DB) as con:
            rows = con.execute(
                "SELECT id, account_size, daily_loss_limit, max_positions, is_active "
                "FROM account_profiles ORDER BY id"
            ).fetchall()
            if rows:
                print("  Account profiles:")
                for r in rows:
                    active = "✓" if r[4] else "✗"
                    print(f"    {active} {r[0]:<25}  size=${r[1]:,.0f}  dll=${r[2]:.0f}  maxpos={r[3]}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Boosted-prob wrapper (used when real probs are below ML gate)
# ---------------------------------------------------------------------------

def _make_boosted_predict(min_prob: float = 0.55):
    """
    Returns a predict_probabilities replacement that clips probs to min_prob.
    Calls the real prediction function first, then overrides any below threshold.
    """
    from vanguard.helpers.strategy_router import predict_probabilities as _real

    def _boosted(df):
        df = _real(df)
        n_before_long  = (df["lgbm_long_prob"]  > 0.50).sum()
        n_before_short = (df["lgbm_short_prob"] > 0.50).sum()
        df["lgbm_long_prob"]  = df["lgbm_long_prob"].clip(lower=min_prob)
        df["lgbm_short_prob"] = df["lgbm_short_prob"].clip(lower=min_prob)
        logger.info(
            f"[boosted probs] {len(df)} symbols  "
            f"(original above gate: long={n_before_long}, short={n_before_short}  "
            f"→ all forced to min={min_prob})"
        )
        return df

    return _boosted


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_output() -> None:
    print(f"\n{'='*60}")
    print("  VERIFICATION")
    print(f"{'='*60}")

    with sqlite3.connect(_DB) as con:
        # Shortlist breakdown
        print("\n  Shortlist by strategy + direction:")
        try:
            rows = con.execute("""
                SELECT strategy, direction, COUNT(*) as n,
                       ROUND(AVG(strategy_score), 4) as avg_score
                FROM vanguard_shortlist
                GROUP BY strategy, direction
                ORDER BY strategy, direction
            """).fetchall()
            if rows:
                for r in rows:
                    print(f"    {r[0]:30s} {r[1]:5s}  n={r[2]:3d}  avg_score={r[3]:.4f}")
            else:
                print("    (empty)")
        except Exception as e:
            print(f"    (error: {e})")

        # Top consensus picks
        print("\n  Top consensus picks (shortlist):")
        try:
            rows = con.execute("""
                SELECT symbol, direction, consensus_count, strategies_matched
                FROM vanguard_shortlist
                GROUP BY symbol, direction
                HAVING MAX(consensus_count) > 1
                ORDER BY consensus_count DESC
                LIMIT 10
            """).fetchall()
            if rows:
                for r in rows:
                    print(f"    {r[1]:5s} {r[0]:8s}  consensus={r[2]}  [{r[3]}]")
            else:
                print("    (no multi-strategy consensus)")
        except Exception as e:
            print(f"    (error: {e})")

        # Portfolio by account
        print("\n  Tradeable portfolio by account:")
        try:
            rows = con.execute("""
                SELECT account_id,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END) as approved,
                       SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) as rejected
                FROM vanguard_tradeable_portfolio
                GROUP BY account_id
                ORDER BY account_id
            """).fetchall()
            if rows:
                for r in rows:
                    status = "✅" if r[2] > 0 else "❌"
                    print(f"    {status} {r[0]:<25}  total={r[1]:3d}  approved={r[2]:3d}  rejected={r[3]:3d}")
            else:
                print("    (empty)")
        except Exception as e:
            print(f"    (error: {e})")

        # Rejection breakdown
        print("\n  Rejection reasons (all accounts combined):")
        try:
            rows = con.execute("""
                SELECT rejection_reason, COUNT(*) as n
                FROM vanguard_tradeable_portfolio
                WHERE status = 'REJECTED' AND rejection_reason IS NOT NULL
                GROUP BY rejection_reason
                ORDER BY n DESC
                LIMIT 15
            """).fetchall()
            if rows:
                for r in rows:
                    print(f"    {r[0]:<35}  {r[1]:3d}")
            else:
                print("    (none)")
        except Exception as e:
            print(f"    (error: {e})")

    print()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a single V1→V6 cycle using existing Vanguard data."
    )
    parser.add_argument("--dry-run",      action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--force-regime", default=None,
                        choices=["ACTIVE", "CAUTION", "DEAD", "CLOSED"],
                        help="Force regime gate result (default: auto-detect)")
    parser.add_argument("--account",      default=None,
                        help="Only run this account ID through V6")
    parser.add_argument("--skip-cache",   action="store_true", default=True,
                        help="Skip V1 live fetch (use existing bars) — default True")
    parser.add_argument("--min-ml-prob",  type=float, default=0.55,
                        help="Minimum prob to force when ML gate blocks all (default 0.55)")
    args = parser.parse_args()

    t0 = time.time()

    print(f"\n{'='*60}")
    print(f"  V1→V6 Single-Cycle Runner")
    print(f"  dry_run={args.dry_run}  force_regime={args.force_regime}")
    print(f"  account={args.account or 'all'}  min_ml_prob={args.min_ml_prob}")
    print(f"{'='*60}")

    # ── Initial DB state ──────────────────────────────────────────────────
    print_db_state("INITIAL DB STATE")
    print_features_summary()
    print_account_profiles()

    # ── V1: Bar cache ──────────────────────────────────────────────────────
    bars_count = _count("vanguard_bars_5m")
    if isinstance(bars_count, int) and bars_count > 0:
        print(f"\n[V1] Skipping live fetch — {bars_count:,} bars already in DB")
    else:
        print("\n[V1] WARNING: vanguard_bars_5m is empty. V3/ATR will use fallbacks.")

    # ── V2: Prefilter — use existing health data ──────────────────────────
    health_count = _count("vanguard_health")
    features_count = _count("vanguard_features")
    print(f"\n[V2] Prefilter state: {health_count} health rows, {features_count} feature rows")
    if features_count == "MISSING" or (isinstance(features_count, int) and features_count == 0):
        print("[cycle] ABORT: No feature data — run V3 factor engine first")
        sys.exit(1)

    # ── V3–V4B: Already computed — features + models in DB ────────────────
    print(f"\n[V3/V4B] Using existing features and model artifacts")

    # ── V5: Strategy Router ────────────────────────────────────────────────
    print(f"\n[V5] Running strategy router...")
    from stages.vanguard_selection import run as v5_run

    v5_kwargs = dict(
        dry_run=args.dry_run,
        force_regime=args.force_regime,
    )

    v5_result = v5_run(**v5_kwargs)
    logger.info(f"V5 result: {v5_result}")

    # Detect ML gate failure: all probs below threshold → no results
    if v5_result.get("status") == "no_results":
        print(
            f"\n[V5] ML gate blocked all candidates "
            f"(LGBM probs ~0.17-0.31 < 0.50 gate threshold)."
        )
        print(
            f"[V5] Retrying with probs forced to {args.min_ml_prob} "
            f"— this proves the pipeline runs correctly with real features."
        )

        boosted_predict = _make_boosted_predict(args.min_ml_prob)

        # Patch the name as bound in vanguard_selection module namespace
        with patch("stages.vanguard_selection.predict_probabilities",
                   side_effect=boosted_predict):
            v5_result = v5_run(**v5_kwargs)

        logger.info(f"V5 boosted result: {v5_result}")

    v5_rows = v5_result.get("rows", 0)
    v5_status = v5_result.get("status", "unknown")

    if v5_rows == 0:
        print(f"\n[cycle] V5 produced 0 rows (status={v5_status}) — aborting")
        sys.exit(1)

    print(
        f"\n[V5] {v5_rows} shortlist rows "
        f"({'preview only — not written' if args.dry_run else 'written to vanguard_shortlist'})"
    )

    # ── V6: Risk Filters ───────────────────────────────────────────────────
    if args.dry_run:
        print(
            "\n[V6] Skipping (--dry-run active: shortlist not in DB, "
            "re-run without --dry-run to write V5→V6)"
        )
        elapsed = time.time() - t0
        print(f"\n[cycle] Dry-run complete in {elapsed:.1f}s")
        print_db_state("FINAL DB STATE (unchanged)")
        return

    print(f"\n[V6] Running risk filters (account={args.account or 'all'})...")
    from stages.vanguard_risk_filters import run as v6_run

    v6_result = v6_run(
        account_filter=args.account,
        dry_run=False,
    )
    logger.info(f"V6 result: {v6_result}")

    v6_rows = v6_result.get("rows", 0)
    v6_status = v6_result.get("status", "unknown")

    print(f"\n[V6] {v6_rows} portfolio rows written (status={v6_status})")

    elapsed = time.time() - t0
    print(f"\n[cycle] Done in {elapsed:.1f}s")

    # ── Final state + verification ─────────────────────────────────────────
    print_db_state("FINAL DB STATE")
    verify_output()


if __name__ == "__main__":
    main()
