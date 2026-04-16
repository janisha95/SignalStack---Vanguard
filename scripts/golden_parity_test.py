"""
golden_parity_test.py — Phase 4 golden-parity test.

Generates 300 synthetic evaluate_candidate() calls (100 candidates × 3 profiles),
writes deterministic JSON output, and optionally diffs two files.

Usage:
    python3 scripts/golden_parity_test.py --out /tmp/phase4_golden.json
    python3 scripts/golden_parity_test.py --diff /tmp/pre.json /tmp/post.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Synthetic test data
# ---------------------------------------------------------------------------

SEED = 42

_PROFILES = [
    {
        "id": "gft_10k",
        "policy_id": "gft_10k_v1",
        "account_size": 10000,
        "is_active": True,
        "instrument_scope": "gft_universe",
        "environment": "qa",
    },
    {
        "id": "gft_5k",
        "policy_id": "gft_standard_v1",
        "account_size": 5000,
        "is_active": True,
        "instrument_scope": "gft_universe",
        "environment": "qa",
    },
    {
        "id": "gft_standard",
        "policy_id": "gft_standard_v1",
        "account_size": 8000,
        "is_active": True,
        "instrument_scope": "gft_universe",
        "environment": "qa",
    },
]

_BASE_POLICY = {
    "side_controls": {"allow_long": True, "allow_short": True},
    "position_limits": {
        "max_open_positions": 3,
        "block_duplicate_symbols": True,
        "max_holding_minutes": 240,
        "auto_close_after_max_holding": True,
    },
    "drawdown_rules": {
        "daily_loss_pause_pct": 0.03,
        "trailing_drawdown_pause_pct": 0.05,
        "pause_until_next_day": True,
    },
    "reject_rules": {
        "enforce_drawdown_pause": True,
        "enforce_blackout": True,
        "enforce_position_limit": True,
        "enforce_duplicate_block": True,
        "enforce_heat_limit": False,
    },
    "sizing_by_asset_class": {
        "forex": {
            "method": "risk_per_stop_pips",
            "risk_per_trade_pct": 0.005,
            "min_sl_pips": 20,
            "min_tp_pips": 40,
            "pip_value_usd_per_standard_lot": {"EURUSD": 10.0, "default": 10.0},
        },
        "crypto": {
            "method": "risk_per_stop_spread_aware",
            "risk_per_trade_pct": 0.005,
            "min_qty": 1e-6,
            "min_sl_pct": 0.008,
            "min_tp_pct": 0.024,
            "sl_atr_multiple": 1.5,
            "tp_atr_multiple": 3.0,
        },
        "equity": {
            "method": "atr_position_size",
            "risk_per_trade_pct": 0.005,
            "stop_atr_multiple": 1.5,
            "tp_atr_multiple": 3.0,
        },
    },
}

_BLACKOUTS: list[dict] = []  # no active blackouts in golden set


def _make_account_state(rng: random.Random, open_count: int = 0) -> dict:
    equity = rng.uniform(5000, 15000)
    return {
        "equity": round(equity, 2),
        "starting_equity_today": round(equity, 2),
        "daily_pnl_pct": round(rng.uniform(-0.02, 0.02), 4),
        "trailing_dd_pct": round(rng.uniform(0.0, 0.03), 4),
        "open_positions": [
            {"symbol": f"SYM{i}", "side": "LONG", "qty": 0.1, "risk_dollars": 50.0}
            for i in range(open_count)
        ],
        "paused_until_utc": None,
        "pause_reason": None,
    }


def _make_candidate(rng: random.Random) -> dict:
    asset_class = rng.choice(["forex", "equity", "crypto"])
    direction   = rng.choice(["LONG", "SHORT"])
    if asset_class == "forex":
        entry = round(rng.uniform(0.8, 200.0), 5)
        atr   = round(entry * rng.uniform(0.001, 0.005), 5)
        sym   = rng.choice(["EURUSD", "USDJPY", "GBPUSD", "USDCHF", "AUDUSD"])
    elif asset_class == "equity":
        entry = round(rng.uniform(10.0, 500.0), 2)
        atr   = round(entry * rng.uniform(0.005, 0.02), 4)
        sym   = rng.choice(["AAPL", "MSFT", "SPY", "QQQ"])
    else:  # crypto
        entry = round(rng.uniform(20000.0, 70000.0), 2)
        atr   = round(entry * rng.uniform(0.005, 0.02), 2)
        sym   = rng.choice(["BTCUSD", "ETHUSD"])
        entry = 0.0 if rng.random() < 0.05 else entry  # 5% invalid price

    return {
        "symbol": sym,
        "direction": direction,
        "entry_price": entry,
        "intraday_atr": atr if rng.random() > 0.05 else 0.0,  # 5% invalid ATR
        "asset_class": asset_class,
        "bid": None,
        "ask": None,
    }


def generate_cases() -> list[dict]:
    """Generate 300 deterministic test cases (100 candidates × 3 profiles)."""
    rng = random.Random(SEED)
    cases = []
    candidates = [_make_candidate(rng) for _ in range(100)]

    for profile in _PROFILES:
        for cand in candidates:
            open_count = rng.randint(0, 4)
            account_state = _make_account_state(rng, open_count)
            cycle_ts = datetime(2026, 4, 1, 9, 30, 0, tzinfo=timezone.utc)
            cases.append({
                "candidate": cand,
                "profile": profile,
                "policy": dict(_BASE_POLICY),
                "account_state": account_state,
                "cycle_ts": cycle_ts.isoformat(),
            })
    return cases


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_cases(cases: list[dict]) -> list[dict]:
    from vanguard.risk.policy_engine import evaluate_candidate
    results = []
    for i, c in enumerate(cases):
        cycle_ts = datetime.fromisoformat(c["cycle_ts"])
        dec = evaluate_candidate(
            candidate=c["candidate"],
            profile=c["profile"],
            policy=c["policy"],
            overrides=None,
            blackouts=_BLACKOUTS,
            account_state=c["account_state"],
            cycle_ts_utc=cycle_ts,
        )
        results.append({"case": i, "decision": dec.as_dict()})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    out_p = sub.add_parser("--out", help="Run cases and write JSON output")
    out_p.add_argument("path")

    diff_p = sub.add_parser("--diff", help="Diff two golden files")
    diff_p.add_argument("pre")
    diff_p.add_argument("post")

    # Also allow positional-style: --out /path
    parser.add_argument("--out", dest="out_path", metavar="PATH")
    parser.add_argument("--diff", nargs=2, metavar=("PRE", "POST"))

    args = parser.parse_args()

    if args.out_path:
        cases = generate_cases()
        results = run_cases(cases)
        out = json.dumps(results, sort_keys=True, indent=2)
        Path(args.out_path).write_text(out)
        print(f"Written {len(results)} results to {args.out_path}")
        return 0

    if args.diff:
        pre_path, post_path = args.diff
        pre  = json.dumps(json.loads(Path(pre_path).read_text()),  sort_keys=True)
        post = json.dumps(json.loads(Path(post_path).read_text()), sort_keys=True)
        if pre == post:
            print("GOLDEN PARITY: PASS — files are byte-identical (after sort_keys)")
            return 0
        else:
            import difflib
            diff = list(difflib.unified_diff(
                pre.splitlines(keepends=True),
                post.splitlines(keepends=True),
                fromfile=pre_path,
                tofile=post_path,
                n=3,
            ))
            print("GOLDEN PARITY: FAIL — diff follows:")
            print("".join(diff[:100]))
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
