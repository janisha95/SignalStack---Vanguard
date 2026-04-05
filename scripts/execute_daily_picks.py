#!/usr/bin/env python3
"""
execute_daily_picks.py — Execute daily picks from Meridian or S1 via SignalStack.

Reads picks from source system DBs (READ ONLY), assigns each pick to one of 7
signal tiers, converts to TradeOrder objects tagged with signal_tier and
signal_metadata for independent forward validation. All writes go to
vanguard_universe.db ONLY.

7 Signal Tiers:
  S1 (from signalstack_results.db):
    tier_dual          LONG  p_tp >= 0.60 AND nn_p_tp >= 0.75
    tier_nn            LONG  nn_p_tp >= 0.90, excl. tier_dual
    tier_rf            LONG  p_tp > 0.60 AND convergence >= 0.80, excl. dual/nn
    tier_scorer_long   LONG  scorer_prob >= 0.55, excl. dual/nn/rf, top 10
    tier_s1_short      SHORT scorer_prob >= 0.55

  Meridian (from v2_universe.db):
    tier_meridian_long  LONG  tcn >= 0.70 AND factor_rank >= 0.80, top 10
    tier_meridian_short SHORT tcn < 0.30 AND factor_rank > 0.90, top 5

Usage:
  python3 scripts/execute_daily_picks.py --source all --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source s1 --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source meridian --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source tier_dual --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source tier_meridian_long --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source all --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
VANGUARD_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VANGUARD_ROOT))

from vanguard.execution.bridge import ExecutionBridge, TradeOrder
from vanguard.execution.signalstack_adapter import SignalStackAdapter
from vanguard.execution.telegram_alerts import TelegramAlerts

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("execute_daily_picks")

# ── Paths ─────────────────────────────────────────────────────────────────────
VANGUARD_DB      = VANGUARD_ROOT / "data" / "vanguard_universe.db"
MERIDIAN_DB      = Path.home() / "SS" / "Meridian" / "data" / "v2_universe.db"
S1_DB            = Path.home() / "SS" / "Advance" / "data_cache" / "signalstack_results.db"
S1_CONV_DIR      = Path.home() / "SS" / "Advance" / "evening_results"
EXEC_CONFIG      = VANGUARD_ROOT / "config" / "vanguard_execution_config.json"
ACCOUNTS_CONFIG  = VANGUARD_ROOT / "config" / "vanguard_accounts.json"

# ── Tier constants ────────────────────────────────────────────────────────────
S1_TIERS      = {"tier_dual", "tier_nn", "tier_rf", "tier_scorer_long", "tier_s1_short"}
MERIDIAN_TIERS = {"tier_meridian_long", "tier_meridian_short"}
ALL_TIERS     = S1_TIERS | MERIDIAN_TIERS
VALID_SOURCES = {"all", "s1", "meridian"} | ALL_TIERS

# ── Tier thresholds ───────────────────────────────────────────────────────────
RF_DUAL_MIN       = 0.60
NN_DUAL_MIN       = 0.75
NN_NN_MIN         = 0.90
RF_RF_MIN         = 0.60
CONV_RF_MIN       = 0.80
SCORER_LONG_MIN   = 0.55
SCORER_LONG_TOP_N = 10
SCORER_SHORT_MIN  = 0.55
TCN_LONG_MIN      = 0.70
FRANK_LONG_MIN    = 0.80
MER_LONG_TOP_N    = 10
TCN_SHORT_MAX     = 0.30
FRANK_SHORT_MIN   = 0.90
MER_SHORT_TOP_N   = 5


# ── Config loading ────────────────────────────────────────────────────────────

def _resolve_env(value: str) -> str:
    if isinstance(value, str) and value.startswith("ENV:"):
        var_name = value[4:]
        env_val = os.environ.get(var_name, "")
        if not env_val:
            logger.warning(f"Environment variable {var_name!r} is not set")
        return env_val
    return value


def load_execution_config() -> dict[str, Any]:
    with open(EXEC_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["signalstack"]["webhook_url"] = _resolve_env(cfg["signalstack"]["webhook_url"])
    cfg["telegram"]["bot_token"]      = _resolve_env(cfg["telegram"]["bot_token"])
    cfg["telegram"]["chat_id"]        = _resolve_env(cfg["telegram"]["chat_id"])
    return cfg


# ── S1 data loaders (READ ONLY) ───────────────────────────────────────────────

def _latest_s1_run_date(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        "SELECT MAX(run_date) FROM scorer_predictions"
    ).fetchone()
    return row[0] if row and row[0] else None


def load_s1_scorer(run_date: str | None = None) -> dict[tuple[str, str], dict]:
    """
    Load scorer_predictions (READ ONLY), deduped by MAX(scorer_prob)
    per (ticker, direction).

    Returns: {(ticker, direction): {p_tp, nn_p_tp, scorer_prob, price, regime, gate_source}}
    """
    if not S1_DB.exists():
        logger.error(f"S1 DB not found: {S1_DB}")
        return {}

    con = sqlite3.connect(f"file:{S1_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        date = run_date or _latest_s1_run_date(con)
        if not date:
            logger.warning("S1: no run_date found in scorer_predictions")
            return {}
        rows = con.execute(
            """
            SELECT ticker, direction,
                   MAX(scorer_prob) AS scorer_prob,
                   MAX(p_tp)        AS p_tp,
                   MAX(nn_p_tp)     AS nn_p_tp,
                   MAX(price)       AS price,
                   MAX(regime)      AS regime,
                   MAX(gate_source) AS gate_source
            FROM   scorer_predictions
            WHERE  run_date = ?
            GROUP  BY ticker, direction
            """,
            (date,),
        ).fetchall()
    finally:
        con.close()

    result: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["ticker"].upper(), r["direction"].upper())
        result[key] = {
            "p_tp":        float(r["p_tp"]        or 0),
            "nn_p_tp":     float(r["nn_p_tp"]     or 0),
            "scorer_prob": float(r["scorer_prob"]  or 0),
            "price":       float(r["price"]        or 0),
            "regime":      r["regime"]      or "",
            "gate_source": r["gate_source"] or "",
        }
    logger.info(f"S1 scorer loaded: {len(result)} rows for run_date={date}")
    return result


def load_convergence_scores() -> dict[str, float]:
    """
    Load convergence_score per ticker from the latest convergence_*.json file.
    Returns: {ticker: convergence_score}
    """
    files = sorted(S1_CONV_DIR.glob("convergence_*.json")) if S1_CONV_DIR.exists() else []
    if not files:
        logger.warning(f"No convergence JSON files found in {S1_CONV_DIR}")
        return {}

    latest = files[-1]
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.error(f"Failed to load convergence JSON {latest.name}: {exc}")
        return {}

    shortlist = data.get("shortlist") or []
    scores: dict[str, float] = {}
    for item in shortlist:
        t = (item.get("ticker") or "").upper()
        cs = item.get("convergence_score")
        if t and cs is not None:
            scores[t] = float(cs)

    logger.info(f"Convergence scores loaded: {len(scores)} tickers from {latest.name}")
    return scores


# ── Tier builder: S1 ──────────────────────────────────────────────────────────

def build_s1_tiers(
    scorer: dict[tuple[str, str], dict],
    convergence: dict[str, float],
) -> dict[str, list[dict]]:
    """
    Apply tier logic to S1 scorer data with dedup priority:
    Dual > NN > RF > Scorer (each LONG ticker in ONE tier only).

    Returns dict: {tier_name: [pick_dict, ...]}
    Each pick_dict has: ticker, direction, signal_tier, p_tp, nn_p_tp,
                        scorer_prob, convergence_score, price, regime, gate_source
    """
    assigned: set[str] = set()  # tracks tickers assigned to any LONG tier

    tier_dual: list[dict] = []
    tier_nn: list[dict] = []
    tier_rf: list[dict] = []
    tier_scorer_long: list[dict] = []
    tier_s1_short: list[dict] = []

    for (ticker, direction), r in scorer.items():
        if direction != "LONG":
            continue
        p    = r["p_tp"]
        nn   = r["nn_p_tp"]
        conv = convergence.get(ticker, 0.0)

        if p >= RF_DUAL_MIN and nn >= NN_DUAL_MIN:
            tier_dual.append({
                "ticker": ticker, "direction": "LONG", "signal_tier": "tier_dual",
                "p_tp": p, "nn_p_tp": nn, "scorer_prob": r["scorer_prob"],
                "convergence_score": conv, "price": r["price"],
                "regime": r["regime"], "gate_source": r["gate_source"],
            })
            assigned.add(ticker)

    # tier_nn: high NN, not in dual
    for (ticker, direction), r in scorer.items():
        if direction != "LONG" or ticker in assigned:
            continue
        nn = r["nn_p_tp"]
        if nn >= NN_NN_MIN:
            tier_nn.append({
                "ticker": ticker, "direction": "LONG", "signal_tier": "tier_nn",
                "p_tp": r["p_tp"], "nn_p_tp": nn, "scorer_prob": r["scorer_prob"],
                "convergence_score": convergence.get(ticker, 0.0),
                "price": r["price"], "regime": r["regime"], "gate_source": r["gate_source"],
            })
            assigned.add(ticker)

    # tier_rf: RF + convergence, not in dual/nn
    for (ticker, direction), r in scorer.items():
        if direction != "LONG" or ticker in assigned:
            continue
        p    = r["p_tp"]
        conv = convergence.get(ticker, 0.0)
        if p > RF_RF_MIN and conv >= CONV_RF_MIN:
            tier_rf.append({
                "ticker": ticker, "direction": "LONG", "signal_tier": "tier_rf",
                "p_tp": p, "nn_p_tp": r["nn_p_tp"], "scorer_prob": r["scorer_prob"],
                "convergence_score": conv, "price": r["price"],
                "regime": r["regime"], "gate_source": r["gate_source"],
            })
            assigned.add(ticker)

    # tier_scorer_long: scorer-only, not in dual/nn/rf, top 10
    candidates: list[dict] = []
    for (ticker, direction), r in scorer.items():
        if direction != "LONG" or ticker in assigned:
            continue
        if r["scorer_prob"] >= SCORER_LONG_MIN:
            candidates.append({
                "ticker": ticker, "direction": "LONG", "signal_tier": "tier_scorer_long",
                "p_tp": r["p_tp"], "nn_p_tp": r["nn_p_tp"], "scorer_prob": r["scorer_prob"],
                "convergence_score": convergence.get(ticker, 0.0),
                "price": r["price"], "regime": r["regime"], "gate_source": r["gate_source"],
            })
    candidates.sort(key=lambda x: -x["scorer_prob"])
    tier_scorer_long = candidates[:SCORER_LONG_TOP_N]

    # tier_s1_short: no dedup (different direction)
    for (ticker, direction), r in scorer.items():
        if direction != "SHORT":
            continue
        if r["scorer_prob"] >= SCORER_SHORT_MIN:
            tier_s1_short.append({
                "ticker": ticker, "direction": "SHORT", "signal_tier": "tier_s1_short",
                "p_tp": r["p_tp"], "nn_p_tp": r["nn_p_tp"], "scorer_prob": r["scorer_prob"],
                "convergence_score": convergence.get(ticker, 0.0),
                "price": r["price"], "regime": r["regime"], "gate_source": r["gate_source"],
            })
    tier_s1_short.sort(key=lambda x: -x["scorer_prob"])

    logger.info(
        f"S1 tiers: dual={len(tier_dual)} nn={len(tier_nn)} rf={len(tier_rf)} "
        f"scorer_long={len(tier_scorer_long)} s1_short={len(tier_s1_short)}"
    )
    return {
        "tier_dual":        tier_dual,
        "tier_nn":          tier_nn,
        "tier_rf":          tier_rf,
        "tier_scorer_long": tier_scorer_long,
        "tier_s1_short":    tier_s1_short,
    }


# ── Tier builder: Meridian ────────────────────────────────────────────────────

def build_meridian_tiers(trade_date: str) -> dict[str, list[dict]]:
    """
    Load Meridian shortlist and apply tier thresholds (READ ONLY).

    Table: shortlist_daily
    Columns: date, ticker, direction, tcn_score, factor_rank, final_score,
             rank, regime, sector, price
    """
    if not MERIDIAN_DB.exists():
        logger.error(f"Meridian DB not found: {MERIDIAN_DB}")
        return {"tier_meridian_long": [], "tier_meridian_short": []}

    con = sqlite3.connect(f"file:{MERIDIAN_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT date, ticker, direction, tcn_score, factor_rank,
                   final_score, rank, regime, sector, price
            FROM   shortlist_daily
            WHERE  date = ?
            ORDER  BY final_score DESC
            """,
            (trade_date,),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        logger.warning(f"Meridian: no rows for date={trade_date}")
        return {"tier_meridian_long": [], "tier_meridian_short": []}

    tier_meridian_long: list[dict] = []
    tier_meridian_short: list[dict] = []

    for r in rows:
        tcn  = float(r["tcn_score"]   or 0)
        fr   = float(r["factor_rank"] or 0)
        fs   = float(r["final_score"] or 0)
        d    = (r["direction"] or "").upper()

        if d == "LONG" and tcn >= TCN_LONG_MIN and fr >= FRANK_LONG_MIN:
            if len(tier_meridian_long) < MER_LONG_TOP_N:
                tier_meridian_long.append({
                    "ticker": r["ticker"].upper(), "direction": "LONG",
                    "signal_tier": "tier_meridian_long",
                    "tcn_score": tcn, "factor_rank": fr, "final_score": fs,
                    "price": float(r["price"] or 0),
                    "regime": r["regime"] or "",
                })

        elif d == "SHORT" and tcn < TCN_SHORT_MAX and fr > FRANK_SHORT_MIN:
            if len(tier_meridian_short) < MER_SHORT_TOP_N:
                tier_meridian_short.append({
                    "ticker": r["ticker"].upper(), "direction": "SHORT",
                    "signal_tier": "tier_meridian_short",
                    "tcn_score": tcn, "factor_rank": fr, "final_score": fs,
                    "price": float(r["price"] or 0),
                    "regime": r["regime"] or "",
                })

    logger.info(
        f"Meridian tiers: long={len(tier_meridian_long)} short={len(tier_meridian_short)}"
    )
    return {
        "tier_meridian_long":  tier_meridian_long,
        "tier_meridian_short": tier_meridian_short,
    }


# ── Pick → TradeOrder ─────────────────────────────────────────────────────────

def pick_to_order(
    pick: dict,
    shares: int,
    account_id: str,
    execution_mode: str,
) -> TradeOrder:
    """Convert a tier pick dict to a TradeOrder with signal_tier + signal_metadata."""
    tier = pick["signal_tier"]
    source = "meridian" if tier.startswith("tier_meridian") else "s1"

    # Build signal_metadata from tier-relevant scores
    meta: dict[str, Any] = {}
    for key in ("p_tp", "nn_p_tp", "scorer_prob", "convergence_score",
                "tcn_score", "factor_rank", "final_score", "price", "regime", "gate_source"):
        val = pick.get(key)
        if val is not None and val != "":
            meta[key] = val

    return TradeOrder(
        symbol=pick["ticker"].upper(),
        direction=pick["direction"].upper(),
        shares=shares,
        order_type="market",
        source_system=source,
        account_id=account_id,
        execution_mode=execution_mode,
        signal_tier=tier,
        signal_metadata=meta,
    )


# ── Bridge factory ────────────────────────────────────────────────────────────

def build_bridge(cfg: dict, dry_run: bool) -> ExecutionBridge:
    ss_cfg = cfg["signalstack"]
    tg_cfg = cfg["telegram"]

    ss_adapter = None
    tg_adapter = None

    webhook_url = ss_cfg["webhook_url"]
    if webhook_url and not dry_run:
        ss_adapter = SignalStackAdapter(
            webhook_url=webhook_url,
            timeout=int(ss_cfg.get("timeout_seconds", 5)),
            max_retries=int(ss_cfg.get("max_retries", 2)),
            retry_delay=float(ss_cfg.get("retry_delay_seconds", 1.0)),
            log_payloads=bool(ss_cfg.get("log_payloads", True)),
        )
    elif not webhook_url:
        logger.warning("SIGNALSTACK_WEBHOOK_URL not set — orders will be forward-tracked")

    tg_enabled = bool(tg_cfg.get("enabled", True)) and not dry_run
    if tg_cfg.get("bot_token") and tg_cfg.get("chat_id"):
        tg_adapter = TelegramAlerts(
            bot_token=tg_cfg["bot_token"],
            chat_id=tg_cfg["chat_id"],
            enabled=tg_enabled,
        )

    VANGUARD_DB.parent.mkdir(parents=True, exist_ok=True)
    return ExecutionBridge(
        db_path=str(VANGUARD_DB),
        signalstack=ss_adapter,
        telegram=tg_adapter,
    )


# ── Tier selector ─────────────────────────────────────────────────────────────

def select_tiers(source: str) -> set[str]:
    """Return the set of tier names that should run for a given --source value."""
    if source == "all":
        return set(ALL_TIERS)
    if source == "s1":
        return set(S1_TIERS)
    if source == "meridian":
        return set(MERIDIAN_TIERS)
    if source in ALL_TIERS:
        return {source}
    raise ValueError(
        f"Unknown source {source!r}. Valid: all, s1, meridian, "
        + ", ".join(sorted(ALL_TIERS))
    )


# ── Main run ──────────────────────────────────────────────────────────────────

def run(
    source: str,
    account_id: str,
    trade_date: str,
    shares_per_trade: int,
    execution_mode: str,
    dry_run: bool,
) -> int:
    logger.info(
        f"execute_daily_picks: source={source} account={account_id} "
        f"date={trade_date} mode={execution_mode} dry_run={dry_run}"
    )

    cfg = load_execution_config()
    if execution_mode:
        cfg["defaults"]["execution_mode"] = execution_mode
    effective_mode = cfg["defaults"]["execution_mode"]
    if dry_run:
        effective_mode = "off"
        logger.info("[dry-run] execution_mode forced to OFF")

    active_tiers = select_tiers(source)

    # ── Load data ─────────────────────────────────────────────────────────────
    all_tiers: dict[str, list[dict]] = {}

    needs_s1      = bool(active_tiers & S1_TIERS)
    needs_meridian = bool(active_tiers & MERIDIAN_TIERS)

    if needs_s1:
        scorer      = load_s1_scorer()
        convergence = load_convergence_scores()
        s1_tiers    = build_s1_tiers(scorer, convergence)
        all_tiers.update(s1_tiers)

    if needs_meridian:
        mer_tiers = build_meridian_tiers(trade_date)
        all_tiers.update(mer_tiers)

    # ── Filter to requested tiers only ───────────────────────────────────────
    active_tier_data: dict[str, list[dict]] = {
        t: all_tiers.get(t, []) for t in sorted(active_tiers)
    }

    # ── Convert to TradeOrder objects ─────────────────────────────────────────
    tier_orders: dict[str, list[TradeOrder]] = {}
    for tier_name, picks in active_tier_data.items():
        orders = [
            pick_to_order(p, shares_per_trade, account_id, effective_mode)
            for p in picks
        ]
        tier_orders[tier_name] = orders

    total = sum(len(v) for v in tier_orders.values())
    logger.info(f"Total orders across all tiers: {total}")
    for tier_name, orders in sorted(tier_orders.items()):
        if orders:
            syms = ", ".join(o.symbol for o in orders)
            logger.info(f"  {tier_name} ({len(orders)}): {syms}")

    if total == 0:
        logger.warning("No picks found — nothing to execute")
        return 0

    # ── Dry-run: print and exit ───────────────────────────────────────────────
    if dry_run:
        print("\n=== DRY-RUN: trade orders that would be submitted ===")
        idx = 1
        for tier_name, orders in sorted(tier_orders.items()):
            for o in orders:
                meta_str = " ".join(
                    f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in o.signal_metadata.items()
                    if k in ("p_tp", "nn_p_tp", "scorer_prob", "convergence_score",
                             "tcn_score", "factor_rank")
                )
                print(
                    f"  [{idx:02d}] {o.symbol:6s} {o.direction:5s} x{o.shares} "
                    f"tier={o.signal_tier}  {meta_str}"
                )
                idx += 1
        return 0

    # ── Execute ───────────────────────────────────────────────────────────────
    bridge = build_bridge(cfg, dry_run=False)

    all_orders: list[TradeOrder] = [
        o for orders in tier_orders.values() for o in orders
    ]
    results = bridge.execute_batch(all_orders)

    filled = sum(1 for r in results if r.get("success") and r.get("status_code", 0) != 0)
    ft     = sum(1 for r in results if r.get("success") and r.get("status_code", 0) == 0)
    failed = sum(1 for r in results if not r.get("success"))
    if effective_mode == "off":
        ft = sum(1 for r in results if r.get("success"))
        filled = 0

    logger.info(
        f"Results: submitted={len(all_orders)} filled={filled} "
        f"forward_tracked={ft} failed={failed}"
    )

    # ── Telegram tier summary ─────────────────────────────────────────────────
    tg = bridge.telegram
    if tg:
        # Preserve canonical tier order for the report
        ordered_tier_orders: dict[str, list[TradeOrder]] = {}
        for t in ("tier_dual", "tier_nn", "tier_rf", "tier_scorer_long",
                  "tier_s1_short", "tier_meridian_long", "tier_meridian_short"):
            if t in tier_orders and tier_orders[t]:
                ordered_tier_orders[t] = tier_orders[t]
        tg.alert_tier_summary(ordered_tier_orders, account_id, effective_mode)

    return 1 if failed > 0 else 0


def main() -> int:
    all_source_choices = sorted(VALID_SOURCES)
    parser = argparse.ArgumentParser(
        description="Execute daily picks from Meridian or S1 via SignalStack (7-tier)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        choices=all_source_choices,
        default="all",
        metavar="SOURCE",
        help=(
            "Pick source. One of: all, s1, meridian, or a specific tier "
            f"({', '.join(sorted(ALL_TIERS))}). Default: all"
        ),
    )
    parser.add_argument(
        "--account",
        default="ttp_demo_1m",
        help="Account ID from vanguard_accounts.json (default: ttp_demo_1m)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trade date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--shares-per-trade",
        type=int,
        default=100,
        help="Fixed shares per trade (default: 100)",
    )
    parser.add_argument(
        "--execution-mode",
        choices=["off", "paper", "live"],
        default=None,
        help="Override execution mode (default: from config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print trades but do not execute; forces execution_mode=off",
    )

    args = parser.parse_args()
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")

    return run(
        source=args.source,
        account_id=args.account,
        trade_date=trade_date,
        shares_per_trade=args.shares_per_trade,
        execution_mode=args.execution_mode or "",
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
