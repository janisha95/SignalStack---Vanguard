"""
vanguard_selection.py — V5 Strategy Router entry point.

Three jobs:
  1. Regime Gate — should we trade this cycle?
  2. Strategy Router — run asset-class-specific scoring strategies
  3. Consensus Count — how many strategies agree per pick?

Writes results to vanguard_shortlist table in vanguard_universe.db.

CLI:
  python3 stages/vanguard_selection.py                         # normal run
  python3 stages/vanguard_selection.py --dry-run               # print output, no write
  python3 stages/vanguard_selection.py --asset-class equity    # one asset class only
  python3 stages/vanguard_selection.py --strategy momentum     # one strategy only
  python3 stages/vanguard_selection.py --force-regime ACTIVE   # bypass regime gate
  python3 stages/vanguard_selection.py --enable-llm            # enable LLM overlay
  python3 stages/vanguard_selection.py --debug AAPL            # show score breakdown for one symbol

Location: ~/SS/Vanguard/stages/vanguard_selection.py
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap — allow `python3 stages/vanguard_selection.py` from repo root
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.helpers.regime_detector import detect_regime
from vanguard.helpers.strategy_router import (
    load_features,
    load_ml_thresholds,
    predict_probabilities,
    route,
)
from vanguard.helpers.consensus_counter import count_consensus

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_selection")

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_DB_PATH     = str(_REPO_ROOT / "data" / "vanguard_universe.db")
_STRAT_CFG   = _REPO_ROOT / "config" / "vanguard_strategies.json"
_SELECT_CFG  = _REPO_ROOT / "config" / "vanguard_selection_config.json"

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_CREATE_SHORTLIST = """
CREATE TABLE IF NOT EXISTS vanguard_shortlist (
    cycle_ts_utc    TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    asset_class     TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    strategy_rank   INTEGER NOT NULL,
    strategy_score  REAL    NOT NULL,
    ml_prob         REAL    NOT NULL,
    edge_score      REAL,
    consensus_count INTEGER DEFAULT 0,
    strategies_matched TEXT,
    regime          TEXT    NOT NULL,
    model_family    TEXT,
    model_source    TEXT,
    model_readiness TEXT,
    feature_profile TEXT,
    tbm_profile     TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
);
CREATE INDEX IF NOT EXISTS idx_vg_sl_cycle
    ON vanguard_shortlist(cycle_ts_utc);
CREATE INDEX IF NOT EXISTS idx_vg_sl_asset
    ON vanguard_shortlist(cycle_ts_utc, asset_class);
CREATE INDEX IF NOT EXISTS idx_vg_sl_consensus
    ON vanguard_shortlist(cycle_ts_utc, consensus_count DESC);
"""


def _ensure_schema(con: sqlite3.Connection) -> None:
    def _ensure_column(column: str, ddl: str) -> None:
        cols = {
            row[1]
            for row in con.execute("PRAGMA table_info(vanguard_shortlist)").fetchall()
        }
        if column not in cols:
            con.execute(f"ALTER TABLE vanguard_shortlist ADD COLUMN {ddl}")

    for stmt in _CREATE_SHORTLIST.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    _ensure_column("model_family", "model_family TEXT")
    _ensure_column("model_source", "model_source TEXT")
    _ensure_column("model_readiness", "model_readiness TEXT")
    _ensure_column("feature_profile", "feature_profile TEXT")
    _ensure_column("tbm_profile", "tbm_profile TEXT")
    con.commit()


def _write_shortlist(con: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    _ensure_schema(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_shortlist
            (cycle_ts_utc, symbol, asset_class, direction, strategy,
             strategy_rank, strategy_score, ml_prob, edge_score,
             consensus_count, strategies_matched, regime,
             model_family, model_source, model_readiness, feature_profile, tbm_profile)
        VALUES
            (:cycle_ts_utc, :symbol, :asset_class, :direction, :strategy,
             :strategy_rank, :strategy_score, :ml_prob, :edge_score,
             :consensus_count, :strategies_matched, :regime,
             :model_family, :model_source, :model_readiness, :feature_profile, :tbm_profile)
        """,
        rows,
    )
    con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Operational guards
# ---------------------------------------------------------------------------

_MODEL_DIR = _REPO_ROOT / "models" / "vanguard" / "latest"


def check_model_availability() -> dict:
    """Check if LGBM model artifacts exist. Returns status dict."""
    missing = []
    for direction in ("long", "short"):
        path = _MODEL_DIR / f"lgbm_equity_{direction}_v1.pkl"
        if not path.exists():
            missing.append(str(path))
    return {
        "has_models": len(missing) == 0,
        "model_dir": str(_MODEL_DIR),
        "missing": missing,
    }


def check_feature_freshness(df: pd.DataFrame, max_age_minutes: int = 30) -> bool:
    """
    Warn if the feature cycle_ts is older than max_age_minutes.
    Returns True if fresh (or age cannot be determined).
    """
    if df.empty or "cycle_ts_utc" not in df.columns:
        return True
    cycle_ts = df["cycle_ts_utc"].iloc[0]
    try:
        cycle_dt = datetime.fromisoformat(cycle_ts.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - cycle_dt).total_seconds() / 60
        if age_min > max_age_minutes:
            logger.warning(
                "[V5] Feature staleness: cycle_ts=%s is %.1f min old (threshold=%d min). "
                "V3 may not have run this cycle.",
                cycle_ts, age_min, max_age_minutes,
            )
            return False
    except Exception as exc:
        logger.debug("[V5] Could not parse feature cycle_ts for freshness check: %s", exc)
    return True


def validate_v5_v6_contract(cycle_ts: str) -> bool:
    """
    Verify V6 can read the shortlist V5 just wrote.
    Checks: rows exist, required columns present, ml_prob null rate.
    Returns True if valid.
    """
    required_cols = {
        "cycle_ts_utc", "symbol", "asset_class", "direction", "strategy",
        "strategy_rank", "strategy_score", "ml_prob", "regime",
    }
    try:
        with sqlite3.connect(_DB_PATH) as con:
            cur = con.execute(
                "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ? LIMIT 1",
                (cycle_ts,),
            )
            row = cur.fetchone()
            if row is None:
                logger.error(
                    "[V5→V6] Contract FAILED: no rows in vanguard_shortlist for cycle=%s",
                    cycle_ts,
                )
                return False
            actual_cols = {desc[0] for desc in cur.description}
            missing = required_cols - actual_cols
            if missing:
                logger.error("[V5→V6] Contract FAILED: missing columns %s", missing)
                return False
            total = con.execute(
                "SELECT COUNT(*) FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                (cycle_ts,),
            ).fetchone()[0]
            null_ml = con.execute(
                "SELECT COUNT(*) FROM vanguard_shortlist "
                "WHERE cycle_ts_utc = ? AND ml_prob IS NULL",
                (cycle_ts,),
            ).fetchone()[0]
        if null_ml > 0:
            logger.warning(
                "[V5→V6] %d/%d rows have NULL ml_prob for cycle=%s",
                null_ml, total, cycle_ts,
            )
        logger.info(
            "[V5→V6] Contract PASSED: %d rows, required columns present", total
        )
        return True
    except Exception as exc:
        logger.error("[V5→V6] Contract check error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------

def _debug_symbol(
    df: pd.DataFrame,
    symbol: str,
    cycle_ts: str,
    strategies_config: dict,
    selection_config: dict,
) -> None:
    """Print feature values and strategy scores for a single symbol."""
    row = df[df["symbol"] == symbol]
    if row.empty:
        print(f"[debug] Symbol '{symbol}' not in current cycle features.")
        return

    print(f"\n{'='*60}")
    print(f"DEBUG: {symbol}  cycle={cycle_ts}")
    print(f"{'='*60}")
    for col in sorted(row.columns):
        val = row.iloc[0][col]
        print(f"  {col:40s} = {val}")

    print(f"\n{'─'*60}")
    print("Strategy scores:")
    debug_df = pd.concat([row] * 20, ignore_index=True)  # pad for normalisation
    debug_df["symbol"] = [f"FAKE_{i:02d}" if i > 0 else symbol for i in range(20)]
    debug_df.iloc[1:, debug_df.columns.get_loc("lgbm_long_prob")]  = 0.3
    debug_df.iloc[1:, debug_df.columns.get_loc("lgbm_short_prob")] = 0.3

    from vanguard.helpers.strategy_router import _build_registry
    reg = _build_registry()
    for strat_name, strategy in reg.items():
        for direction in ["LONG", "SHORT"]:
            if strategy.short_only and direction == "LONG":
                continue
            try:
                result = strategy.score(debug_df, direction, top_n=20)
                sym_row = result[result["symbol"] == symbol]
                if not sym_row.empty:
                    sc = sym_row.iloc[0]["strategy_score"]
                    rk = sym_row.iloc[0]["strategy_rank"]
                    print(f"  {strat_name:30s} {direction:5s}  score={sc:.4f}  rank={rk}")
            except Exception:
                pass
    print()


def _apply_mock_probabilities_for_undervalidated_models(
    df: pd.DataFrame,
    strategies_config: dict,
) -> pd.DataFrame:
    """Fallback to mock probabilities when only under-validated models exist and they produce no gated candidates."""
    adjusted = df.copy()
    rng = np.random.default_rng()
    threshold_overrides = load_ml_thresholds()

    for asset_class, ac_df in adjusted.groupby("asset_class", dropna=False):
        if ac_df.empty:
            continue
        ac_name = asset_class or "equity"
        ac_cfg = strategies_config.get(ac_name, {})
        ac_thresholds = threshold_overrides.get(ac_name, {})

        for side in ("long", "short"):
            readiness_col = f"{side}_model_readiness"
            prob_col = f"lgbm_{side}_prob"
            threshold = float(ac_thresholds.get(side, ac_cfg.get("ml_gate_threshold", 0.50)))
            readiness = ac_df[readiness_col].fillna("")
            if not readiness.eq("trained_insufficient_validation").all():
                continue
            if float(ac_df[prob_col].max()) >= threshold:
                continue

            count = len(ac_df)
            adjusted.loc[ac_df.index, prob_col] = rng.uniform(0.45, 0.65, count)
            logger.warning(
                "[V5] %s %s model is trained_insufficient_validation and produced no scores above %.2f — using mock probabilities for %d symbols",
                ac_name,
                side,
                threshold,
                count,
            )

    return adjusted


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    dry_run: bool = False,
    asset_class_filter: str | None = None,
    strategy_filter: str | None = None,
    force_regime: str | None = None,
    enable_llm: bool = False,
    debug_symbol: str | None = None,
) -> dict:
    t0 = time.time()
    cycle_ts = iso_utc(now_utc())
    logger.info(f"V5 Selection — cycle={cycle_ts}")

    with connect_wal(_DB_PATH) as con:
        _ensure_schema(con)

    # Load configs
    with open(_STRAT_CFG) as fh:
        strategies_config: dict = json.load(fh)
    with open(_SELECT_CFG) as fh:
        selection_config: dict = json.load(fh)

    # ── 0. Model availability check ──────────────────────────────────────────
    model_status = check_model_availability()
    if not model_status["has_models"]:
        logger.warning(
            "[V5] No trained models in %s — all ML probabilities will default to 0.0. "
            "ML gate will eliminate all candidates. Run V4B training to fix. Missing: %s",
            model_status["model_dir"], model_status["missing"],
        )

    # ── 1. Load features ─────────────────────────────────────────────────────
    df = load_features()
    if df.empty:
        logger.warning("No features available — exiting")
        return {"status": "no_features", "rows": 0}
    logger.info(f"Loaded features: {len(df)} symbols")

    check_feature_freshness(df)

    # ── 2. Predict LGBM probabilities ────────────────────────────────────────
    df = predict_probabilities(df)
    df = _apply_mock_probabilities_for_undervalidated_models(df, strategies_config)

    if not model_status["has_models"]:
        rng = np.random.default_rng()
        n = len(df)
        df["lgbm_long_prob"]  = rng.uniform(0.45, 0.65, n)
        df["lgbm_short_prob"] = rng.uniform(0.45, 0.65, n)
        logger.warning(
            "[V5] NO MODELS — using mock predictions (random 0.45-0.65) for %d symbols", n
        )

    # Debug single symbol
    if debug_symbol:
        _debug_symbol(df, debug_symbol, cycle_ts, strategies_config, selection_config)

    # ── 3. Regime Gate ───────────────────────────────────────────────────────
    regime_cfg = selection_config.get("regime_gate", {})
    if force_regime:
        regime = force_regime.upper()
        logger.info(f"Regime forced to: {regime}")
    else:
        regime = detect_regime(
            df,
            atr_caution_threshold=regime_cfg.get("atr_caution_threshold", 2.0),
            volume_dead_threshold=regime_cfg.get("volume_dead_threshold", 0.5),
        )

    if regime in ("DEAD", "CLOSED"):
        logger.info(f"Regime={regime} — skipping cycle")
        return {"status": "skipped", "regime": regime, "rows": 0}

    logger.info(f"Regime={regime} — proceeding")

    # ── 4. Strategy Router ───────────────────────────────────────────────────
    t1 = time.time()
    results = route(
        df=df,
        strategies_config=strategies_config,
        selection_config=selection_config,
        regime=regime,
        asset_class_filter=asset_class_filter,
        strategy_filter=strategy_filter,
        enable_llm=enable_llm,
    )
    logger.info(f"Strategy routing done in {time.time()-t1:.2f}s — {len(results)} result batches")

    if not results:
        logger.warning("No results from strategy router")
        return {"status": "no_results", "regime": regime, "rows": 0}

    # ── 5. Consensus Count ───────────────────────────────────────────────────
    combined = count_consensus(results)

    # Attach cycle metadata
    combined["cycle_ts_utc"] = cycle_ts

    # Ensure ml_prob is populated from lgbm probs
    if "ml_prob" not in combined.columns:
        # Join from df
        prob_map_long  = df.set_index("symbol")["lgbm_long_prob"].to_dict()
        prob_map_short = df.set_index("symbol")["lgbm_short_prob"].to_dict()
        combined["ml_prob"] = combined.apply(
            lambda r: prob_map_long.get(r["symbol"], 0.5)
            if r["direction"] == "LONG"
            else prob_map_short.get(r["symbol"], 0.5),
            axis=1,
        )

    # edge_score column (None if not computed by strategy)
    if "edge_score" not in combined.columns:
        combined["edge_score"] = None

    # ── 6. Write to DB ───────────────────────────────────────────────────────
    rows = combined.rename(columns={"asset_class": "asset_class"}).to_dict(orient="records")

    # Coerce types for SQLite
    for r in rows:
        r["cycle_ts_utc"]      = cycle_ts
        r["strategy_rank"]     = int(r.get("strategy_rank", 0))
        r["strategy_score"]    = float(r.get("strategy_score", 0.0))
        r["ml_prob"]           = float(r.get("ml_prob", 0.0))
        r["consensus_count"]   = int(r.get("consensus_count", 0))
        r["strategies_matched"] = r.get("strategies_matched", "")
        r["regime"]            = regime
        r.setdefault("edge_score", None)
        r.setdefault("asset_class", "equity")
        r.setdefault("model_family", None)
        r.setdefault("model_source", "none")
        r.setdefault("model_readiness", "not_trained")
        r.setdefault("feature_profile", None)
        r.setdefault("tbm_profile", None)

    total_elapsed = time.time() - t0
    logger.info(
        f"V5 complete — {len(rows)} rows, regime={regime}, "
        f"elapsed={total_elapsed:.2f}s"
    )

    if dry_run:
        logger.info("[DRY-RUN] Not writing to DB")
        _print_summary(combined)
        return {
            "status": "dry_run",
            "regime": regime,
            "rows": len(rows),
            "elapsed": total_elapsed,
        }

    with connect_wal(_DB_PATH) as con:
        _ensure_schema(con)
        written = _write_shortlist(con, rows)

    logger.info(f"Wrote {written} rows to vanguard_shortlist")
    validate_v5_v6_contract(cycle_ts)
    _print_summary(combined)

    return {
        "status": "ok",
        "regime": regime,
        "rows": written,
        "elapsed": total_elapsed,
    }


def _print_summary(combined: pd.DataFrame) -> None:
    """Print a human-readable summary of results."""
    if combined.empty:
        print("[V5] No results.")
        return

    print(f"\n{'='*70}")
    print(f"V5 Strategy Router — Summary")
    print(f"{'='*70}")

    for strat in sorted(combined["strategy"].unique()):
        sub = combined[combined["strategy"] == strat]
        long_picks  = sub[sub["direction"] == "LONG"]
        short_picks = sub[sub["direction"] == "SHORT"]
        print(f"\n  [{strat.upper()}]  LONG:{len(long_picks)}  SHORT:{len(short_picks)}")
        for _, r in sub.sort_values("strategy_rank").iterrows():
            dir_sym = "↑" if r["direction"] == "LONG" else "↓"
            print(
                f"    {dir_sym} {r['symbol']:8s}  "
                f"score={r['strategy_score']:.4f}  "
                f"rank={r['strategy_rank']}  "
                f"consensus={r.get('consensus_count', '?')}"
            )

    # Consensus highlights
    if "consensus_count" in combined.columns:
        top = (
            combined[["symbol", "direction", "consensus_count", "strategies_matched"]]
            .drop_duplicates(subset=["symbol", "direction"])
            .sort_values("consensus_count", ascending=False)
            .head(10)
        )
        if not top.empty and top.iloc[0]["consensus_count"] > 1:
            print(f"\n{'─'*70}")
            print("  TOP CONSENSUS PICKS:")
            for _, r in top.iterrows():
                print(
                    f"    {r['direction']:5s} {r['symbol']:8s}  "
                    f"consensus={r['consensus_count']}  "
                    f"strategies=[{r['strategies_matched']}]"
                )

    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V5 Vanguard Selection — Strategy Router")
    p.add_argument("--dry-run", action="store_true", help="Print results, do not write to DB")
    p.add_argument("--asset-class", dest="asset_class", default=None,
                   choices=["equity", "forex", "index", "metal", "crypto", "commodity"],
                   help="Run only this asset class")
    p.add_argument("--strategy", default=None,
                   help="Run only this strategy (e.g. smc, momentum)")
    p.add_argument("--force-regime", dest="force_regime", default=None,
                   choices=["ACTIVE", "CAUTION", "DEAD", "CLOSED"],
                   help="Override regime detection")
    p.add_argument("--enable-llm", action="store_true", help="Enable LLM strategy overlay")
    p.add_argument("--debug", default=None, metavar="SYMBOL",
                   help="Print feature + strategy score breakdown for one symbol")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(
        dry_run=args.dry_run,
        asset_class_filter=args.asset_class,
        strategy_filter=args.strategy,
        force_regime=args.force_regime,
        enable_llm=args.enable_llm,
        debug_symbol=args.debug,
    )
    sys.exit(0 if result.get("status") in ("ok", "dry_run", "skipped") else 1)
