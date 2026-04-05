# CLAUDE CODE — V1→V6 Single-Cycle End-to-End Run
# Goal: Produce REAL vanguard_shortlist + vanguard_tradeable_portfolio in the DB
# Date: Mar 31, 2026
# Context: V1-V6 code exists but has NEVER run end-to-end. Tables don't exist in DB.

---

## READ FIRST

```bash
# 1. What data already exists in vanguard DB
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
"

# 2. Check if shortlist/portfolio tables exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT name FROM sqlite_master WHERE name IN ('vanguard_shortlist', 'vanguard_tradeable_portfolio', 'vanguard_portfolio_state');
"

# 3. Check what V1-V4 data we have
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 'bars_5m' as tbl, COUNT(*) FROM vanguard_bars_5m
UNION ALL SELECT 'bars_1h', COUNT(*) FROM vanguard_bars_1h
UNION ALL SELECT 'health', COUNT(*) FROM vanguard_health
UNION ALL SELECT 'features', COUNT(*) FROM vanguard_features
UNION ALL SELECT 'training', COUNT(*) FROM vanguard_training_data
UNION ALL SELECT 'model_reg', COUNT(*) FROM vanguard_model_registry
"

# 4. Check V5 entry point
head -30 ~/SS/Vanguard/stages/vanguard_selection.py

# 5. Check V6 entry point
head -30 ~/SS/Vanguard/stages/vanguard_risk_filters.py

# 6. Check what account profiles exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, account_size, daily_loss_limit, max_positions FROM account_profiles;
"

# 7. Check the features table schema and latest data
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
PRAGMA table_info(vanguard_features);
"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT cycle_ts_utc, COUNT(*) as symbols FROM vanguard_features GROUP BY cycle_ts_utc ORDER BY cycle_ts_utc DESC LIMIT 3;
"
```

Report ALL output before writing code.

---

## WHAT TO BUILD

A single script that runs V1→V6 sequentially using EXISTING data and code.
This is NOT the V7 orchestrator. It's a minimal test harness that proves
the pipeline works end-to-end.

### File: ~/SS/Vanguard/scripts/run_single_cycle.py

```python
#!/usr/bin/env python3
"""
Run a single V1→V6 cycle using existing Vanguard data.
Produces real vanguard_shortlist + vanguard_tradeable_portfolio in the DB.

Usage:
    python3 scripts/run_single_cycle.py
    python3 scripts/run_single_cycle.py --dry-run
    python3 scripts/run_single_cycle.py --force-regime ACTIVE
    python3 scripts/run_single_cycle.py --account ttp_50k_intraday
"""

import sys
import os
import time
import argparse
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.expanduser("~/SS/Vanguard"))

def main():
    parser = argparse.ArgumentParser(description="Run single V1→V6 cycle")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, no DB writes")
    parser.add_argument("--force-regime", default=None, help="Force regime: ACTIVE, CAUTION, DEAD")
    parser.add_argument("--account", default=None, help="Specific account profile ID")
    parser.add_argument("--skip-cache", action="store_true", help="Skip V1 cache (use existing bars)")
    args = parser.parse_args()

    cycle_ts = datetime.now(timezone.utc).isoformat()
    print(f"[cycle] Starting single cycle at {cycle_ts}", flush=True)
    print(f"[cycle] dry_run={args.dry_run}, force_regime={args.force_regime}", flush=True)

    t0 = time.time()

    # ── V1: Cache (skip if --skip-cache, use existing bars) ──
    if not args.skip_cache:
        print(f"\n[V1] Cache warm...", flush=True)
        try:
            from stages.vanguard_cache import main as v1_main
            # V1 may need market hours to fetch new data
            # For this test, we can skip if bars already exist
            print(f"[V1] Skipping live fetch — using existing {count_bars()} bars", flush=True)
        except Exception as e:
            print(f"[V1] WARNING: {e} — continuing with existing data", flush=True)
    else:
        print(f"\n[V1] Skipped (--skip-cache)", flush=True)

    # ── V2: Prefilter ──
    print(f"\n[V2] Prefilter...", flush=True)
    try:
        from stages.vanguard_prefilter import run_prefilter
        # Get survivors from existing prefilter results
        survivors = get_latest_survivors()
        print(f"[V2] {len(survivors)} survivors from latest prefilter", flush=True)
    except Exception as e:
        print(f"[V2] WARNING: {e}", flush=True)
        # Fallback: get all symbols with features
        survivors = get_symbols_with_features()
        print(f"[V2] Fallback: {len(survivors)} symbols with features", flush=True)

    if not survivors:
        print(f"[cycle] ABORT: No survivors after V2", flush=True)
        return

    # ── V3: Factor Engine (use existing features) ──
    print(f"\n[V3] Factor engine...", flush=True)
    features_df = get_latest_features(survivors)
    if features_df is None or features_df.empty:
        print(f"[V3] ABORT: No features available", flush=True)
        return
    print(f"[V3] Loaded {len(features_df)} rows × {len(features_df.columns)} features", flush=True)

    # ── V4B: Model scoring ──
    print(f"\n[V4B] Model scoring...", flush=True)
    try:
        predictions = run_model_scoring(features_df)
        print(f"[V4B] Scored {len(predictions)} symbols", flush=True)
    except Exception as e:
        print(f"[V4B] WARNING: {e} — using dummy probs", flush=True)
        predictions = make_dummy_predictions(features_df)

    # ── V5: Strategy Router ──
    print(f"\n[V5] Strategy router...", flush=True)
    try:
        from stages.vanguard_selection import run_selection
        # V5 needs features + predictions merged
        merged = merge_features_predictions(features_df, predictions)
        shortlist = run_selection(
            merged,
            force_regime=args.force_regime or "ACTIVE",
            dry_run=args.dry_run,
            cycle_ts=cycle_ts,
        )
        print(f"[V5] Shortlist: {len(shortlist)} candidates across strategies", flush=True)
    except Exception as e:
        print(f"[V5] ERROR: {e}", flush=True)
        import traceback; traceback.print_exc()
        return

    if shortlist.empty if hasattr(shortlist, 'empty') else not shortlist:
        print(f"[cycle] V5 produced 0 candidates — likely all below ML gate", flush=True)
        print(f"[cycle] Try: --force-regime ACTIVE or check ML probs", flush=True)
        return

    # ── V6: Risk Filters ──
    print(f"\n[V6] Risk filters...", flush=True)
    try:
        from stages.vanguard_risk_filters import run_risk_filters
        portfolios = run_risk_filters(
            shortlist,
            account_id=args.account,
            dry_run=args.dry_run,
            cycle_ts=cycle_ts,
        )
        for acct, portfolio in portfolios.items():
            approved = len([p for p in portfolio if p.get("status") == "APPROVED"])
            rejected = len([p for p in portfolio if p.get("status") != "APPROVED"])
            print(f"[V6] Account {acct}: {approved} approved, {rejected} rejected", flush=True)
    except Exception as e:
        print(f"[V6] ERROR: {e}", flush=True)
        import traceback; traceback.print_exc()
        return

    elapsed = time.time() - t0
    print(f"\n[cycle] DONE in {elapsed:.1f}s", flush=True)

    # ── Verify output ──
    if not args.dry_run:
        verify_output()


def count_bars():
    """Count existing 5m bars."""
    import sqlite3
    con = sqlite3.connect(os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db"))
    count = con.execute("SELECT COUNT(*) FROM vanguard_bars_5m").fetchone()[0]
    con.close()
    return count


def get_latest_survivors():
    """Get symbols from latest prefilter health check."""
    import sqlite3
    con = sqlite3.connect(os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db"))
    try:
        rows = con.execute("""
            SELECT DISTINCT symbol FROM vanguard_health
            WHERE status = 'ACTIVE'
            ORDER BY symbol
        """).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def get_symbols_with_features():
    """Fallback: get all symbols that have feature data."""
    import sqlite3
    con = sqlite3.connect(os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db"))
    try:
        rows = con.execute("""
            SELECT DISTINCT symbol FROM vanguard_features
            ORDER BY symbol
        """).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def get_latest_features(survivors):
    """Load latest feature matrix for survivors."""
    import sqlite3
    import pandas as pd
    con = sqlite3.connect(os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db"))
    try:
        # Get latest cycle timestamp
        latest = con.execute(
            "SELECT MAX(cycle_ts_utc) FROM vanguard_features"
        ).fetchone()[0]
        if not latest:
            return None

        placeholders = ",".join("?" * len(survivors))
        df = pd.read_sql(
            f"SELECT * FROM vanguard_features WHERE cycle_ts_utc = ? AND symbol IN ({placeholders})",
            con, params=[latest] + survivors
        )
        return df
    except Exception as e:
        print(f"[V3] Error loading features: {e}", flush=True)
        return None
    finally:
        con.close()


def run_model_scoring(features_df):
    """Run LGBM models on feature matrix."""
    try:
        from vanguard.models.model_loader import load_model, predict
        lgbm_long = load_model("lgbm_long")
        lgbm_short = load_model("lgbm_short")

        # Get feature columns (exclude metadata)
        meta_cols = {"symbol", "cycle_ts_utc", "asset_class"}
        feature_cols = [c for c in features_df.columns if c not in meta_cols]

        X = features_df[feature_cols].fillna(0)
        features_df["lgbm_long_prob"] = lgbm_long.predict_proba(X)[:, 1] if hasattr(lgbm_long, "predict_proba") else lgbm_long.predict(X)
        features_df["lgbm_short_prob"] = lgbm_short.predict_proba(X)[:, 1] if hasattr(lgbm_short, "predict_proba") else lgbm_short.predict(X)

        return features_df[["symbol", "lgbm_long_prob", "lgbm_short_prob"]]
    except Exception as e:
        raise RuntimeError(f"Model scoring failed: {e}")


def make_dummy_predictions(features_df):
    """Fallback: create dummy predictions above ML gate for testing."""
    import pandas as pd
    return pd.DataFrame({
        "symbol": features_df["symbol"],
        "lgbm_long_prob": 0.55,  # Just above ML gate
        "lgbm_short_prob": 0.55,
    })


def merge_features_predictions(features_df, predictions):
    """Merge features with model predictions."""
    if "lgbm_long_prob" in features_df.columns:
        return features_df  # Already merged from run_model_scoring
    return features_df.merge(predictions, on="symbol", how="left")


def verify_output():
    """Check that V5/V6 wrote to the DB."""
    import sqlite3
    con = sqlite3.connect(os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db"))
    try:
        tables = {
            "vanguard_shortlist": "SELECT COUNT(*) FROM vanguard_shortlist",
            "vanguard_tradeable_portfolio": "SELECT COUNT(*) FROM vanguard_tradeable_portfolio",
            "vanguard_portfolio_state": "SELECT COUNT(*) FROM vanguard_portfolio_state",
        }
        print(f"\n[verify] Output tables:", flush=True)
        for name, sql in tables.items():
            try:
                count = con.execute(sql).fetchone()[0]
                status = "✅" if count > 0 else "❌ EMPTY"
                print(f"  {name}: {count} rows {status}", flush=True)
            except Exception:
                print(f"  {name}: ❌ TABLE MISSING", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()
```

---

## IMPORTANT: Adapt to actual V5/V6 function signatures

The script above calls `run_selection()` and `run_risk_filters()` — but the
actual function signatures in vanguard_selection.py and vanguard_risk_filters.py
may be different. Read those files FIRST and adapt the calls.

```bash
# Check V5 entry point signature
grep -n "def run\|def main\|def select\|if __name__" ~/SS/Vanguard/stages/vanguard_selection.py

# Check V6 entry point signature
grep -n "def run\|def main\|def filter\|if __name__" ~/SS/Vanguard/stages/vanguard_risk_filters.py

# Check what V5 expects as input
grep -n "import\|from.*import" ~/SS/Vanguard/stages/vanguard_selection.py | head -20

# Check what V6 expects as input
grep -n "import\|from.*import" ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -20
```

Adapt `run_single_cycle.py` to match the ACTUAL function signatures.

---

## RUN IT

```bash
cd ~/SS/Vanguard

# Dry run first
python3 scripts/run_single_cycle.py --dry-run --skip-cache --force-regime ACTIVE

# If dry run passes, real run
python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE

# Verify output
sqlite3 data/vanguard_universe.db "SELECT strategy, direction, COUNT(*) FROM vanguard_shortlist GROUP BY strategy, direction"
sqlite3 data/vanguard_universe.db "SELECT account_id, COUNT(*), SUM(CASE WHEN status='APPROVED' THEN 1 ELSE 0 END) as approved FROM vanguard_tradeable_portfolio GROUP BY account_id"
```

---

## EXPECTED ISSUES AND HOW TO HANDLE

1. **ML probs below 0.50 gate**: All current LGBM probs are ~0.17-0.31 (below gate).
   Fix: Use `--force-regime ACTIVE` and temporarily lower ML gate to 0.30 for testing,
   OR use `make_dummy_predictions()` with probs=0.55.

2. **V5 expects features V3 didn't compute**: Strategy scoring functions reference
   features that may not exist in the DB. Check which features are available:
   ```bash
   sqlite3 data/vanguard_universe.db "PRAGMA table_info(vanguard_features)"
   ```
   Map strategy feature names to actual column names.

3. **V6 expects account_profiles columns that don't exist**: The V6 pre-execution
   gate added new columns. Check:
   ```bash
   sqlite3 data/vanguard_universe.db "PRAGMA table_info(account_profiles)"
   ```

4. **Table creation**: V5/V6 should CREATE TABLE IF NOT EXISTS. If they don't,
   the tables need to be created first.

---

## GIT

```bash
cd ~/SS/Vanguard
git init 2>/dev/null  # Only if not already initialized
git add -A && git commit -m "feat: single-cycle V1-V6 end-to-end runner"
```
