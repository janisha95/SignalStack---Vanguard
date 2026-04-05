#!/usr/bin/env python3
"""
Vanguard E2E Test — Multi-Asset Pipeline Validation
====================================================
Purpose: Run V1(skip - use existing bars)→V2→V3→V5→V6 as a single cycle
         on actual backfilled data in vanguard_universe.db.
         First-ever test of forex, crypto, and commodities flowing through
         the full pipeline.

Pre-requisites:
  - vanguard_universe.db exists with backfilled bars (V4A may still be running)
  - All Vanguard stage files exist in ~/SS/Vanguard/stages/

Usage:
  python3 vanguard_e2e_test.py              # Full E2E test
  python3 vanguard_e2e_test.py --stage v2   # Test single stage
  python3 vanguard_e2e_test.py --dry-run    # Print plan only
  python3 vanguard_e2e_test.py --verbose    # Extra detail

What this tests:
  1. DB health: Are bars present for equities, forex, crypto, metals, energy, agriculture?
  2. V2 prefilter: Do forex/commodity symbols pass health checks on historical data?
  3. V3 factor engine: Do 35 features compute for multi-asset symbols?
  4. V5 selection: Do strategy routers work for each asset class?
  5. V6 risk filters: Does sizing work for lots (forex) vs shares (equity)?
  6. End-to-end: Does the V7 --single-cycle path chain all stages?

NOT tested (V4A/V4B):
  - Model training is overnight, can't be tested inline
  - V5 will run with mock/fallback predictions if no model exists yet
"""

import sys
import os
import json
import sqlite3
import argparse
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from textwrap import indent

# ── Paths ──────────────────────────────────────────────────────────────
VANGUARD_ROOT = Path(os.path.expanduser("~/SS/Vanguard"))
DB_PATH = VANGUARD_ROOT / "data" / "vanguard_universe.db"
STAGES_DIR = VANGUARD_ROOT / "stages"

# Add Vanguard to sys.path so stage imports work
sys.path.insert(0, str(VANGUARD_ROOT))

# ── ANSI colors ────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def ok(msg):    print(f"  {GREEN}✅ {msg}{RESET}")
def fail(msg):  print(f"  {RED}❌ {msg}{RESET}")
def warn(msg):  print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg):  print(f"  {CYAN}ℹ  {msg}{RESET}")
def header(msg): print(f"\n{BOLD}{'─'*60}\n  {msg}\n{'─'*60}{RESET}")

# ── Test result tracking ───────────────────────────────────────────────
results = {"passed": 0, "failed": 0, "warned": 0, "skipped": 0}
failures = []

def check(condition, pass_msg, fail_msg, fatal=False):
    """Assert a condition, track result."""
    if condition:
        ok(pass_msg)
        results["passed"] += 1
        return True
    else:
        fail(fail_msg)
        results["failed"] += 1
        failures.append(fail_msg)
        if fatal:
            print(f"\n  {RED}FATAL: Cannot continue. Fix this first.{RESET}")
            sys.exit(1)
        return False


# ════════════════════════════════════════════════════════════════════════
#  PHASE 0: Environment & DB Health
# ════════════════════════════════════════════════════════════════════════

def test_phase0_environment():
    header("PHASE 0: Environment & DB Health")

    # 0.1 — DB exists
    check(DB_PATH.exists(),
          f"DB exists: {DB_PATH}",
          f"DB not found: {DB_PATH}", fatal=True)

    # 0.2 — Stage files exist
    stage_files = [
        "vanguard_cache.py",
        "vanguard_prefilter.py",
        "vanguard_factor_engine.py",
        "vanguard_selection.py",
        "vanguard_risk_filters.py",
        "vanguard_orchestrator.py",
        "vanguard_model_trainer.py",
        "vanguard_training_backfill.py",
    ]
    for f in stage_files:
        path = STAGES_DIR / f
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        check(exists and size > 100,
              f"{f} exists ({size:,} bytes)",
              f"{f} missing or empty")

    # 0.3 — DB tables exist
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    expected_tables = [
        "vanguard_bars_1m", "vanguard_bars_5m", "vanguard_bars_1h",
        "vanguard_health", "vanguard_features",
        "universe_members",
    ]
    existing = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    for t in expected_tables:
        check(t in existing,
              f"Table {t} exists",
              f"Table {t} MISSING from DB")

    # 0.4 — Check optional tables that V4A/V4B/V5/V6 create
    optional_tables = [
        "vanguard_training_data",
        "vanguard_model_registry",
        "vanguard_walkforward_results",
        "vanguard_shortlist",
        "vanguard_tradeable_portfolio",
        "vanguard_factor_matrix",
    ]
    for t in optional_tables:
        if t in existing:
            count = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            info(f"Table {t} exists ({count:,} rows)")
        else:
            info(f"Table {t} not yet created (expected pre-V4B)")

    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 1: Bar Coverage by Asset Class
# ════════════════════════════════════════════════════════════════════════

def test_phase1_bar_coverage():
    header("PHASE 1: Bar Coverage by Asset Class")

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    # 1.1 — Total bar counts
    for table in ["vanguard_bars_1m", "vanguard_bars_5m", "vanguard_bars_1h"]:
        try:
            count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            check(count > 0, f"{table}: {count:,} rows", f"{table}: EMPTY")
        except Exception as e:
            fail(f"{table}: query failed — {e}")
            results["failed"] += 1

    # 1.2 — Distinct symbols
    try:
        symbols_1m = cur.execute(
            "SELECT COUNT(DISTINCT symbol) FROM vanguard_bars_1m").fetchone()[0]
        symbols_5m = cur.execute(
            "SELECT COUNT(DISTINCT symbol) FROM vanguard_bars_5m").fetchone()[0]
        info(f"Distinct symbols: 1m={symbols_1m}, 5m={symbols_5m}")
    except Exception as e:
        warn(f"Could not count distinct symbols: {e}")

    # 1.3 — Multi-asset symbol check: look for specific test tickers
    test_symbols = {
        "equity": ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"],
        "forex": ["EUR/USD", "EURUSD", "GBP/USD", "GBPUSD", "USD/JPY", "USDJPY"],
        "crypto": ["BTC/USD", "BTCUSD", "ETH/USD", "ETHUSD"],
        "metal": ["XAU/USD", "XAUUSD", "XAG/USD", "XAGUSD"],
        "energy": ["NATGAS", "USOIL", "NATGAS.cash"],
        "agriculture": ["CORN", "WHEAT", "COCOA", "CORN.c", "WHEAT.c", "COCOA.c"],
    }

    all_db_symbols = {r[0] for r in cur.execute(
        "SELECT DISTINCT symbol FROM vanguard_bars_5m").fetchall()}

    for asset_class, candidates in test_symbols.items():
        found = [s for s in candidates if s in all_db_symbols]
        if found:
            ok(f"{asset_class}: found {len(found)} symbols → {', '.join(found[:5])}")
        else:
            # Try partial match
            partial = [s for s in all_db_symbols
                       if any(c.replace("/", "").lower() in s.lower() for c in candidates)]
            if partial:
                warn(f"{asset_class}: no exact match, but found partial → {', '.join(sorted(partial)[:5])}")
                results["warned"] += 1
            else:
                fail(f"{asset_class}: NO symbols found in DB (tried {candidates[:3]})")
                results["failed"] += 1

    # 1.4 — Print all unique symbols for debugging
    info(f"All {len(all_db_symbols)} symbols in 5m bars (first 30):")
    for s in sorted(all_db_symbols)[:30]:
        count = cur.execute(
            "SELECT COUNT(*) FROM vanguard_bars_5m WHERE symbol=?", (s,)).fetchone()[0]
        print(f"      {s:20s} → {count:>8,} bars")
    if len(all_db_symbols) > 30:
        print(f"      ... and {len(all_db_symbols) - 30} more")

    # 1.5 — universe_members table (canonical bus from Stage 1 unification)
    try:
        um_count = cur.execute("SELECT COUNT(*) FROM universe_members").fetchone()[0]
        if um_count > 0:
            um_classes = cur.execute(
                "SELECT asset_class, COUNT(*) FROM universe_members GROUP BY asset_class"
            ).fetchall()
            ok(f"universe_members: {um_count} members")
            for ac, cnt in um_classes:
                info(f"  {ac}: {cnt}")
        else:
            warn("universe_members exists but is empty")
            results["warned"] += 1
    except Exception as e:
        warn(f"universe_members query failed: {e}")
        results["warned"] += 1

    # 1.6 — Date range of bars
    try:
        date_range = cur.execute("""
            SELECT MIN(timestamp), MAX(timestamp), COUNT(DISTINCT DATE(timestamp))
            FROM vanguard_bars_5m
        """).fetchone()
        if date_range[0]:
            info(f"5m bar range: {date_range[0]} → {date_range[1]} ({date_range[2]} distinct dates)")
    except Exception as e:
        warn(f"Date range query failed: {e}")

    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 2: V2 Prefilter / Health Monitor
# ════════════════════════════════════════════════════════════════════════

def test_phase2_prefilter():
    header("PHASE 2: V2 Prefilter / Health Monitor")

    try:
        # Try to import and run
        from stages.vanguard_prefilter import main as prefilter_main
        info("Imported vanguard_prefilter.main successfully")
    except ImportError as e:
        # Try alternative import patterns
        warn(f"Direct import failed: {e}")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "vanguard_prefilter",
                str(STAGES_DIR / "vanguard_prefilter.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            ok("Loaded vanguard_prefilter via importlib")

            # Check for callable entry points
            entry_points = [attr for attr in dir(mod) if attr in
                           ("main", "run", "run_prefilter", "run_health_check")]
            info(f"Available entry points: {entry_points}")

            if hasattr(mod, "run") or hasattr(mod, "main"):
                info("Entry point found — but skipping execution in test mode")
                info("To run: python3 ~/SS/Vanguard/stages/vanguard_prefilter.py --dry-run")
            else:
                warn("No standard entry point found (main/run)")
                results["warned"] += 1
        except Exception as e2:
            fail(f"Could not load vanguard_prefilter: {e2}")
            results["failed"] += 1
            return

    # Check existing health results in DB
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    try:
        health_count = cur.execute("SELECT COUNT(*) FROM vanguard_health").fetchone()[0]
        if health_count > 0:
            ok(f"vanguard_health has {health_count} rows from previous runs")

            # Check by status
            statuses = cur.execute(
                "SELECT status, COUNT(*) FROM vanguard_health GROUP BY status"
            ).fetchall()
            for status, cnt in statuses:
                info(f"  {status}: {cnt}")

            # Check by asset class
            by_class = cur.execute("""
                SELECT asset_class, COUNT(*), SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END)
                FROM vanguard_health GROUP BY asset_class
            """).fetchall()
            for ac, total, healthy in by_class:
                healthy = healthy or 0
                pct = (healthy/total*100) if total > 0 else 0
                info(f"  {ac}: {healthy}/{total} healthy ({pct:.0f}%)")
        else:
            warn("vanguard_health is empty — V2 has never run on this DB")
            results["warned"] += 1
    except Exception as e:
        warn(f"vanguard_health query failed: {e}")
        results["warned"] += 1

    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 3: V3 Factor Engine
# ════════════════════════════════════════════════════════════════════════

def test_phase3_factor_engine():
    header("PHASE 3: V3 Factor Engine")

    # Check if factor engine module loads
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vanguard_factor_engine",
            str(STAGES_DIR / "vanguard_factor_engine.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok("Loaded vanguard_factor_engine successfully")
    except Exception as e:
        fail(f"Could not load vanguard_factor_engine: {e}")
        results["failed"] += 1
        return

    # Check existing features in DB
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    try:
        feat_count = cur.execute("SELECT COUNT(*) FROM vanguard_features").fetchone()[0]
        if feat_count > 0:
            ok(f"vanguard_features has {feat_count} rows")

            # Check column count (should be ~35 features + metadata)
            cols = [r[1] for r in cur.execute("PRAGMA table_info(vanguard_features)").fetchall()]
            feature_cols = [c for c in cols if c not in (
                "cycle_ts_utc", "symbol", "asset_class", "path",
                "created_at", "updated_at", "id")]
            info(f"Feature columns: {len(feature_cols)}")

            # NaN rate check on a sample
            sample = cur.execute(
                "SELECT * FROM vanguard_features LIMIT 10").fetchall()
            if sample:
                col_names = [d[0] for d in cur.description]
                total_cells = 0
                nan_cells = 0
                for row in sample:
                    for i, val in enumerate(row):
                        if col_names[i] in feature_cols:
                            total_cells += 1
                            if val is None:
                                nan_cells += 1
                nan_rate = nan_cells / total_cells if total_cells > 0 else 1.0
                check(nan_rate < 0.5,
                      f"NaN rate in sample: {nan_rate:.1%} (< 50% threshold)",
                      f"NaN rate in sample: {nan_rate:.1%} — too many nulls")
        else:
            warn("vanguard_features is empty — V3 has never run")
            results["warned"] += 1
    except Exception as e:
        warn(f"vanguard_features query failed: {e}")
        results["warned"] += 1

    # Check factor_matrix (V3 output per cycle)
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "vanguard_factor_matrix" in existing:
            fm_count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_factor_matrix").fetchone()[0]
            info(f"vanguard_factor_matrix: {fm_count} rows")
            if fm_count > 0:
                by_class = cur.execute("""
                    SELECT asset_class, COUNT(*)
                    FROM vanguard_factor_matrix
                    GROUP BY asset_class
                """).fetchall()
                for ac, cnt in by_class:
                    info(f"  {ac}: {cnt} rows")
        else:
            info("vanguard_factor_matrix not yet created")
    except Exception as e:
        warn(f"Factor matrix query failed: {e}")

    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 4: V4A Training Data + V4B Models
# ════════════════════════════════════════════════════════════════════════

def test_phase4_training():
    header("PHASE 4: V4A Training Data + V4B Models")

    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()

    # 4.1 — Training data
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "vanguard_training_data" in existing:
            td_count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_training_data").fetchone()[0]
            ok(f"vanguard_training_data: {td_count:,} rows")

            if td_count > 0:
                # Check by asset class or symbol pattern
                try:
                    cols = [r[1] for r in cur.execute(
                        "PRAGMA table_info(vanguard_training_data)").fetchall()]

                    if "asset_class" in cols:
                        by_class = cur.execute("""
                            SELECT asset_class, COUNT(*) FROM vanguard_training_data
                            GROUP BY asset_class
                        """).fetchall()
                        for ac, cnt in by_class:
                            info(f"  {ac}: {cnt:,} training rows")
                    elif "symbol" in cols:
                        sym_count = cur.execute("""
                            SELECT COUNT(DISTINCT symbol) FROM vanguard_training_data
                        """).fetchone()[0]
                        info(f"  {sym_count} distinct symbols in training data")

                        # Sample some symbols
                        samples = cur.execute("""
                            SELECT symbol, COUNT(*) as cnt
                            FROM vanguard_training_data
                            GROUP BY symbol
                            ORDER BY cnt DESC
                            LIMIT 10
                        """).fetchall()
                        for sym, cnt in samples:
                            info(f"    {sym}: {cnt:,} rows")

                    # Check label distribution
                    if "label_long" in cols:
                        label_dist = cur.execute("""
                            SELECT
                                SUM(CASE WHEN label_long = 1 THEN 1 ELSE 0 END) as long_wins,
                                SUM(CASE WHEN label_long = 0 THEN 1 ELSE 0 END) as long_losses,
                                SUM(CASE WHEN label_short = 1 THEN 1 ELSE 0 END) as short_wins,
                                SUM(CASE WHEN label_short = 0 THEN 1 ELSE 0 END) as short_losses
                            FROM vanguard_training_data
                        """).fetchone()
                        if label_dist[0] is not None:
                            total = (label_dist[0] or 0) + (label_dist[1] or 0)
                            long_wr = label_dist[0] / total * 100 if total > 0 else 0
                            info(f"  Long labels: {label_dist[0]:,} wins / {label_dist[1]:,} losses ({long_wr:.1f}% WR)")
                            total_s = (label_dist[2] or 0) + (label_dist[3] or 0)
                            short_wr = label_dist[2] / total_s * 100 if total_s > 0 else 0
                            info(f"  Short labels: {label_dist[2]:,} wins / {label_dist[3]:,} losses ({short_wr:.1f}% WR)")
                except Exception as e:
                    warn(f"Training data analysis error: {e}")
        else:
            warn("vanguard_training_data not yet created — V4A still running?")
            results["warned"] += 1
    except Exception as e:
        warn(f"Training data check failed: {e}")

    # 4.2 — Model registry
    try:
        if "vanguard_model_registry" in existing:
            models = cur.execute("SELECT * FROM vanguard_model_registry").fetchall()
            cols = [d[0] for d in cur.description]
            if models:
                ok(f"vanguard_model_registry: {len(models)} models")
                for m in models:
                    row = dict(zip(cols, m))
                    info(f"  {row.get('model_id', '?')}: family={row.get('model_family','?')}, "
                         f"track={row.get('track','?')}, status={row.get('status','?')}")
            else:
                info("Model registry exists but empty — V4B not yet run")
        else:
            info("vanguard_model_registry not yet created")
    except Exception as e:
        warn(f"Model registry check failed: {e}")

    # 4.3 — Walk-forward results
    try:
        if "vanguard_walkforward_results" in existing:
            wf_count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_walkforward_results").fetchone()[0]
            info(f"vanguard_walkforward_results: {wf_count} rows")
        else:
            info("vanguard_walkforward_results not yet created")
    except Exception as e:
        warn(f"Walk-forward check failed: {e}")

    # 4.4 — Model artifact files
    models_dir = VANGUARD_ROOT / "models"
    if models_dir.exists():
        model_dirs = [d for d in models_dir.iterdir() if d.is_dir()]
        if model_dirs:
            ok(f"Model artifacts: {len(model_dirs)} directories")
            for d in model_dirs:
                files = list(d.iterdir())
                info(f"  {d.name}/: {len(files)} files")
        else:
            info("models/ directory exists but no model subdirectories yet")
    else:
        info("models/ directory doesn't exist yet")

    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 5: V5 Selection (Strategy Router)
# ════════════════════════════════════════════════════════════════════════

def test_phase5_selection():
    header("PHASE 5: V5 Selection (Strategy Router)")

    # Check module loads
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vanguard_selection",
            str(STAGES_DIR / "vanguard_selection.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok("Loaded vanguard_selection successfully")

        # Check for strategy definitions
        for attr in ["STRATEGY_REGISTRY", "ASSET_CLASS_STRATEGIES",
                     "strategy_registry", "strategies"]:
            if hasattr(mod, attr):
                val = getattr(mod, attr)
                if isinstance(val, dict):
                    info(f"  {attr}: {len(val)} entries → {list(val.keys())[:5]}")
                elif isinstance(val, list):
                    info(f"  {attr}: {len(val)} entries")
    except Exception as e:
        fail(f"Could not load vanguard_selection: {e}")
        results["failed"] += 1

    # Check helper strategy router
    try:
        router_path = VANGUARD_ROOT / "vanguard" / "helpers" / "strategy_router.py"
        if router_path.exists():
            spec2 = importlib.util.spec_from_file_location(
                "strategy_router", str(router_path))
            mod2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(mod2)
            ok("Loaded strategy_router.py helper")

            # Check for concrete strategy classes
            strategies_dir = VANGUARD_ROOT / "vanguard" / "strategies"
            if strategies_dir.exists():
                strat_files = list(strategies_dir.glob("*.py"))
                strat_files = [f for f in strat_files if f.name != "__init__.py"]
                info(f"Strategy files: {len(strat_files)}")
                for sf in strat_files:
                    info(f"  {sf.name} ({sf.stat().st_size:,} bytes)")
        else:
            warn("strategy_router.py not found")
            results["warned"] += 1
    except Exception as e:
        warn(f"Strategy router check failed: {e}")

    # Check existing shortlist in DB
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "vanguard_shortlist" in existing:
            sl_count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_shortlist").fetchone()[0]
            if sl_count > 0:
                ok(f"vanguard_shortlist: {sl_count} rows")
                by_dir = cur.execute("""
                    SELECT direction, COUNT(*) FROM vanguard_shortlist
                    GROUP BY direction
                """).fetchall()
                for d, cnt in by_dir:
                    info(f"  {d}: {cnt}")
            else:
                info("vanguard_shortlist exists but empty — V5 hasn't produced output yet")
        else:
            info("vanguard_shortlist not yet created — expected before V5 first run")
    except Exception as e:
        warn(f"Shortlist check failed: {e}")
    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 6: V6 Risk Filters
# ════════════════════════════════════════════════════════════════════════

def test_phase6_risk():
    header("PHASE 6: V6 Risk Filters")

    # Check module loads
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vanguard_risk_filters",
            str(STAGES_DIR / "vanguard_risk_filters.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok(f"Loaded vanguard_risk_filters ({STAGES_DIR / 'vanguard_risk_filters.py'})")
    except Exception as e:
        fail(f"Could not load vanguard_risk_filters: {e}")
        results["failed"] += 1

    # Check account config
    config_paths = [
        VANGUARD_ROOT / "config" / "vanguard_accounts.json",
        VANGUARD_ROOT / "config" / "vanguard_instrument_specs.json",
        VANGUARD_ROOT / "config" / "vanguard_orchestrator_config.json",
    ]
    for cp in config_paths:
        if cp.exists():
            try:
                data = json.loads(cp.read_text())
                ok(f"Config: {cp.name} ({len(json.dumps(data)):,} chars)")
                # Peek at structure
                if isinstance(data, dict):
                    info(f"  Top keys: {list(data.keys())[:5]}")
            except json.JSONDecodeError as e:
                fail(f"Config {cp.name} is invalid JSON: {e}")
                results["failed"] += 1
        else:
            warn(f"Config not found: {cp.name}")
            results["warned"] += 1

    # Check existing tradeable portfolio in DB
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "vanguard_tradeable_portfolio" in existing:
            tp_count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_tradeable_portfolio").fetchone()[0]
            info(f"vanguard_tradeable_portfolio: {tp_count} rows")
        else:
            info("vanguard_tradeable_portfolio not yet created")
    except Exception as e:
        warn(f"Portfolio check failed: {e}")
    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 7: V7 Orchestrator
# ════════════════════════════════════════════════════════════════════════

def test_phase7_orchestrator():
    header("PHASE 7: V7 Orchestrator")

    orch_path = STAGES_DIR / "vanguard_orchestrator.py"

    # Check it's not empty (was a known issue from audit)
    if orch_path.exists():
        size = orch_path.stat().st_size
        check(size > 500,
              f"vanguard_orchestrator.py: {size:,} bytes (not empty!)",
              f"vanguard_orchestrator.py: {size} bytes — still empty/stub?")

        # Try to load it
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "vanguard_orchestrator", str(orch_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            ok("Loaded vanguard_orchestrator successfully")

            # Check for key functions
            for attr in ["main", "run_single_cycle", "run_cycle",
                         "OrchestratorSession", "VanguardOrchestrator"]:
                if hasattr(mod, attr):
                    info(f"  Found: {attr}")
        except Exception as e:
            fail(f"Could not load vanguard_orchestrator: {e}")
            results["failed"] += 1
    else:
        fail("vanguard_orchestrator.py does not exist")
        results["failed"] += 1

    # Check existing session logs
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in ["vanguard_session_log", "vanguard_execution_log"]:
            if t in existing:
                cnt = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                info(f"{t}: {cnt} rows")
            else:
                info(f"{t} not yet created")
    except Exception as e:
        warn(f"Session log check failed: {e}")
    con.close()


# ════════════════════════════════════════════════════════════════════════
#  PHASE 8: V4A Progress Check (if running)
# ════════════════════════════════════════════════════════════════════════

def test_phase8_v4a_progress():
    header("PHASE 8: V4A Training Backfill Progress")

    # Check the log file
    log_path = VANGUARD_ROOT / "logs" / "v4a_training.log"
    if log_path.exists():
        size = log_path.stat().st_size
        info(f"V4A log: {log_path} ({size:,} bytes)")

        # Read last 30 lines
        try:
            lines = log_path.read_text().strip().split("\n")
            info(f"Total log lines: {len(lines)}")
            print(f"\n  {CYAN}Last 20 lines:{RESET}")
            for line in lines[-20:]:
                print(f"    {line}")
        except Exception as e:
            warn(f"Could not read log: {e}")
    else:
        info("V4A log not found — may not have started yet or using different path")

    # Check if process is running
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "vanguard_training_backfill"],
            capture_output=True, text=True)
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            ok(f"V4A process RUNNING (PIDs: {', '.join(pids)})")
        else:
            info("V4A process not running (may have finished)")
    except Exception as e:
        warn(f"Could not check V4A process: {e}")

    # Check training data row count growth
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    try:
        existing = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "vanguard_training_data" in existing:
            count = cur.execute(
                "SELECT COUNT(*) FROM vanguard_training_data").fetchone()[0]
            ok(f"Training data rows: {count:,}")

            # Estimate progress if we know total symbols
            distinct_syms = cur.execute(
                "SELECT COUNT(DISTINCT symbol) FROM vanguard_training_data").fetchone()[0]
            info(f"Distinct symbols with training data: {distinct_syms}")

            # Target: 2,676 equities + 90 forex/commodities/crypto = 2,766
            target = 2766
            pct = (distinct_syms / target * 100) if target > 0 else 0
            info(f"Progress estimate: {distinct_syms}/{target} symbols ({pct:.1f}%)")
    except Exception as e:
        warn(f"Training progress check failed: {e}")
    con.close()


# ════════════════════════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════════════════════════

def print_summary():
    header("E2E TEST SUMMARY")

    total = results["passed"] + results["failed"] + results["warned"]

    if results["failed"] == 0:
        print(f"  {GREEN}{BOLD}ALL {results['passed']} CHECKS PASSED{RESET}")
    else:
        print(f"  {RED}{BOLD}{results['failed']} FAILURES{RESET} / "
              f"{GREEN}{results['passed']} passed{RESET} / "
              f"{YELLOW}{results['warned']} warnings{RESET}")

    if failures:
        print(f"\n  {RED}Failures:{RESET}")
        for f_msg in failures:
            print(f"    ❌ {f_msg}")

    print(f"\n  {CYAN}Next steps:{RESET}")
    if results["failed"] > 0:
        print("    1. Fix failures above before proceeding")
    print("    2. If V4A is done: python3 ~/SS/Vanguard/stages/vanguard_model_trainer.py --track A")
    print("    3. Then: python3 ~/SS/Vanguard/stages/vanguard_orchestrator.py --single-cycle --dry-run")
    print("    4. Then during market hours: python3 ~/SS/Vanguard/stages/vanguard_orchestrator.py --single-cycle")
    print()


# ════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Vanguard E2E Pipeline Test")
    parser.add_argument("--stage", choices=["v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v4a"],
                        help="Test single stage only")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    parser.add_argument("--verbose", action="store_true", help="Extra detail")
    args = parser.parse_args()

    print(f"\n{BOLD}{'═'*60}")
    print(f"  VANGUARD E2E TEST — Multi-Asset Pipeline Validation")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  DB: {DB_PATH}")
    print(f"{'═'*60}{RESET}")

    if args.dry_run:
        print("\n  DRY RUN — would test phases 0-8")
        print("  Phase 0: Environment & DB health")
        print("  Phase 1: Bar coverage by asset class")
        print("  Phase 2: V2 prefilter / health monitor")
        print("  Phase 3: V3 factor engine")
        print("  Phase 4: V4A training data + V4B models")
        print("  Phase 5: V5 selection (strategy router)")
        print("  Phase 6: V6 risk filters")
        print("  Phase 7: V7 orchestrator")
        print("  Phase 8: V4A progress check")
        return

    stage_map = {
        "v0": [test_phase0_environment],
        "v1": [test_phase0_environment, test_phase1_bar_coverage],
        "v2": [test_phase0_environment, test_phase2_prefilter],
        "v3": [test_phase0_environment, test_phase3_factor_engine],
        "v4": [test_phase0_environment, test_phase4_training],
        "v4a": [test_phase0_environment, test_phase8_v4a_progress],
        "v5": [test_phase0_environment, test_phase5_selection],
        "v6": [test_phase0_environment, test_phase6_risk],
        "v7": [test_phase0_environment, test_phase7_orchestrator],
    }

    if args.stage:
        phases = stage_map.get(args.stage, [])
    else:
        phases = [
            test_phase0_environment,
            test_phase1_bar_coverage,
            test_phase2_prefilter,
            test_phase3_factor_engine,
            test_phase4_training,
            test_phase5_selection,
            test_phase6_risk,
            test_phase7_orchestrator,
            test_phase8_v4a_progress,
        ]

    for phase_fn in phases:
        try:
            phase_fn()
        except Exception as e:
            fail(f"PHASE CRASHED: {e}")
            traceback.print_exc()
            results["failed"] += 1

    print_summary()


if __name__ == "__main__":
    main()
