"""
prefetch_spread_baselines.py — F1: Live spread baseline prefetch via DWX.

Connects to the local MT5 DWX bridge, subscribes all GFT universe symbols to
tick data, samples bid/ask quotes every second for --minutes (default 30),
computes median/p95/p99 spread per symbol, and writes the result to
~/SS/Risky/data/spread_baselines.json.

Usage:
    cd ~/SS/Vanguard_QAenv
    source .env
    python3 scripts/prefetch_spread_baselines.py [--minutes 30] [--output PATH]

Requirements:
    - MT5 is running with DWX_Server_MT5 EA active on the GFT account.
    - Called from within the Vanguard_QAenv virtualenv (or with its deps on sys.path).

Design:
    - Zero-volume ticks (ask==bid, either==0) are discarded per sample.
    - Symbols with < 100 valid samples after filtering are omitted from output.
    - Equity CFDs and index CFDs record sampled_during_active_session=False
      outside 13:30–20:00 UTC weekdays. Risky uses this to downgrade verdict mode
      (see F4 spread_sanity handler).
    - Spread is reported in points (price * 10^digits). For forex 5-digit, 1 point
      = 0.00001. For JPY pairs and crypto, scaling is handled by per-symbol digits.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo path setup
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")
load_dotenv(_REPO_ROOT.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("prefetch_spreads")

# ---------------------------------------------------------------------------
# GFT universe — canonical list from vanguard_runtime.json
# ---------------------------------------------------------------------------
_GFT_UNIVERSE: dict[str, list[str]] = {
    "equity_cfd": [
        "AAPL", "AMZN", "BA", "COST", "JPM", "KO", "META", "MSFT",
        "NFLX", "NVDA", "ORCL", "PLTR", "TSLA", "UNH", "V",
    ],
    "forex_major": [
        "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD",
        "USDCAD", "USDCHF", "USDJPY",
    ],
    "forex_cross": [
        "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD",
        "CADCHF", "CADJPY", "CHFJPY",
        "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY", "EURNZD",
        "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD",
        "NZDCAD", "NZDCHF",
    ],
    "crypto": ["BCHUSD", "BNBUSD", "BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD"],
    "index_cfd": ["AUS200", "GER40", "JAP225", "NAS100", "SPX500", "UK100", "US30"],
    "commodity": ["BRENT", "WTI", "XAGUSD"],
}

# Flat map: symbol → asset_class
_SYMBOL_CLASS: dict[str, str] = {
    sym: cls for cls, syms in _GFT_UNIVERSE.items() for sym in syms
}

# Digit counts: number of decimal places in the price (used to express spread in points)
# 5-digit forex: 1 point = 0.00001 → multiply by 100000
# JPY pairs: 1 point = 0.001 → multiply by 1000
# Crypto: variable, but spread in USD absolute is more intuitive; we use 2 decimal places (cents)
_POINT_MULTIPLIER: dict[str, int] = {}
for sym in _GFT_UNIVERSE["forex_major"] + _GFT_UNIVERSE["forex_cross"]:
    if sym.endswith("JPY"):
        _POINT_MULTIPLIER[sym] = 1000      # JPY pair, 3 decimal places
    else:
        _POINT_MULTIPLIER[sym] = 100_000   # standard 5-digit forex
for sym in _GFT_UNIVERSE["equity_cfd"]:
    _POINT_MULTIPLIER[sym] = 100           # CFDs in USD cents
for sym in _GFT_UNIVERSE["crypto"]:
    _POINT_MULTIPLIER[sym] = 100           # spread in USD cents
for sym in _GFT_UNIVERSE["index_cfd"]:
    _POINT_MULTIPLIER[sym] = 100           # index points × 100 = cents
for sym in _GFT_UNIVERSE["commodity"]:
    _POINT_MULTIPLIER[sym] = 100


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _is_active_session(cls: str, at_utc: datetime) -> tuple[bool, str | None]:
    """
    Return (is_active, reason_if_not).

    - crypto: always active
    - forex_major / forex_cross / commodity: active Mon–Fri (UTC weekday 0–4), not weekend
    - equity_cfd / index_cfd: active Mon–Fri 13:30–20:00 UTC only
    """
    weekday = at_utc.weekday()  # 0=Mon, 5=Sat, 6=Sun
    is_weekday = weekday < 5

    if cls == "crypto":
        return True, None

    if cls in ("forex_major", "forex_cross", "commodity"):
        if not is_weekday:
            return False, f"Sampled on weekend ({at_utc.strftime('%A')}) — forex spreads are artificially wide"
        return True, None

    # equity_cfd, index_cfd: need US session
    if not is_weekday:
        return False, f"Sampled on weekend — equity/index spreads are not representative"
    us_open  = at_utc.replace(hour=13, minute=30, second=0, microsecond=0)
    us_close = at_utc.replace(hour=20, minute=0,  second=0, microsecond=0)
    if us_open <= at_utc <= us_close:
        return True, None
    return False, (
        f"Sampled outside US session ({at_utc.strftime('%H:%M')} UTC) — "
        "equity/index spreads are wide outside 13:30–20:00 UTC"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prefetch live spread baselines from DWX bid/ask sampling."
    )
    parser.add_argument(
        "--minutes", type=int, default=30,
        help="Sampling window in minutes (default: 30). 30 min → ~1800 samples → stable p99.",
    )
    parser.add_argument(
        "--output", type=str,
        default=str(Path.home() / "SS" / "Risky" / "data" / "spread_baselines.json"),
        help="Output JSON path.",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="Seconds between samples (default: 1.0).",
    )
    parser.add_argument(
        "--min-samples", type=int, default=100,
        help="Minimum valid samples required to include a symbol (default: 100).",
    )
    args = parser.parse_args()

    all_symbols = list(_SYMBOL_CLASS.keys())
    total_secs   = args.minutes * 60
    n_expected   = int(total_secs / args.interval)

    logger.info(
        "Starting spread baseline prefetch — %d symbols, %d min, ~%d samples each",
        len(all_symbols), args.minutes, n_expected,
    )

    # ── Connect DWX ────────────────────────────────────────────────────────
    from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
    from vanguard.config.runtime_config import get_runtime_config

    cfg     = get_runtime_config()
    mt5_cfg = cfg.get("data_sources", {}).get("mt5_local", {})
    dwx_path = mt5_cfg.get(
        "dwx_files_path",
        str(Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5"
            "/drive_c/Program Files/MetaTrader 5/MQL5/Files"),
    )
    db_path = cfg.get("database", {}).get(
        "shadow_db_path",
        str(_REPO_ROOT / "data" / "vanguard_universe.db"),
    )

    adapter = MT5DWXAdapter(
        dwx_files_path=dwx_path,
        db_path=db_path,
        symbol_suffix=str(".x" if mt5_cfg.get("symbol_suffix") is None else mt5_cfg.get("symbol_suffix")),
    )
    if not adapter.connect():
        logger.error("DWX connection failed — is MT5 running with DWX_Server_MT5 EA?")
        sys.exit(1)

    logger.info("DWX connected. Subscribing %d symbols to tick data...", len(all_symbols))
    adapter.subscribe_ticks(all_symbols)
    time.sleep(3)  # allow initial market_data to populate

    # ── Sampling loop ──────────────────────────────────────────────────────
    samples: dict[str, list[float]] = defaultdict(list)   # symbol → list of spread_points
    start_utc = datetime.now(timezone.utc)
    end_time  = time.monotonic() + total_secs
    ticks_this_log = 0
    next_log = time.monotonic() + 60

    logger.info(
        "Sampling started at %s UTC. Will run until %s UTC.",
        start_utc.strftime("%H:%M:%S"),
        datetime.fromtimestamp(
            start_utc.timestamp() + total_secs, tz=timezone.utc
        ).strftime("%H:%M:%S"),
    )

    while time.monotonic() < end_time:
        quotes = adapter.get_live_quotes(all_symbols)
        for sym, q in quotes.items():
            bid = q.get("bid", 0.0)
            ask = q.get("ask", 0.0)
            if bid <= 0 or ask <= 0 or ask == bid:
                continue  # zero-volume / market-closed tick — discard
            spread_raw = ask - bid
            mult = _POINT_MULTIPLIER.get(sym, 100)
            spread_points = round(spread_raw * mult, 2)
            if spread_points > 0:
                samples[sym].append(spread_points)
                ticks_this_log += 1

        now_mono = time.monotonic()
        if now_mono >= next_log:
            elapsed_m = (now_mono - (end_time - total_secs)) / 60
            remaining_m = (end_time - now_mono) / 60
            covered = sum(1 for v in samples.values() if len(v) >= 10)
            logger.info(
                "%.1f min elapsed / %.1f remaining — %d symbols active, %d total ticks",
                elapsed_m, remaining_m, covered, ticks_this_log,
            )
            ticks_this_log = 0
            next_log = now_mono + 60

        time.sleep(args.interval)

    end_utc  = datetime.now(timezone.utc)
    midpoint = datetime.fromtimestamp(
        (start_utc.timestamp() + end_utc.timestamp()) / 2, tz=timezone.utc
    )

    logger.info("Sampling complete at %s UTC.", end_utc.strftime("%H:%M:%S"))
    adapter.disconnect()

    # ── Compute statistics ─────────────────────────────────────────────────
    skipped_insufficient: list[str] = []
    output_symbols: dict[str, dict] = {}

    for sym in sorted(all_symbols):
        vals = samples[sym]
        cls  = _SYMBOL_CLASS[sym]
        is_active, note = _is_active_session(cls, midpoint)

        if len(vals) < args.min_samples:
            logger.warning(
                "SKIP %s — only %d valid samples (need %d). active_session=%s",
                sym, len(vals), args.min_samples, is_active,
            )
            skipped_insufficient.append(sym)
            continue

        vals_sorted = sorted(vals)
        n = len(vals_sorted)

        def percentile(sorted_list: list[float], pct: float) -> float:
            idx = pct / 100.0 * (len(sorted_list) - 1)
            lo, hi = int(idx), min(int(idx) + 1, len(sorted_list) - 1)
            return sorted_list[lo] + (sorted_list[hi] - sorted_list[lo]) * (idx - lo)

        output_symbols[sym] = {
            "median_spread_points": round(statistics.median(vals), 2),
            "p95_spread_points":    round(percentile(vals_sorted, 95), 2),
            "p99_spread_points":    round(percentile(vals_sorted, 99), 2),
            "sample_count":         n,
            "asset_class":          cls,
            "sampled_at_utc":       midpoint.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sampled_during_active_session": is_active,
            "notes":                note,
        }
        logger.info(
            "  %-10s  median=%.1f  p95=%.1f  p99=%.1f  n=%d  active=%s",
            sym,
            output_symbols[sym]["median_spread_points"],
            output_symbols[sym]["p95_spread_points"],
            output_symbols[sym]["p99_spread_points"],
            n,
            is_active,
        )

    # ── Write output ───────────────────────────────────────────────────────
    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "_version":                        "2026-04-08-dwx-live-sample",
        "_source":                         f"DWX get_live_quotes, {args.minutes}min sampling window",
        "_sample_start_utc":               start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_sample_end_utc":                 end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_symbols_requested":              len(all_symbols),
        "_symbols_written":                len(output_symbols),
        "_symbols_skipped_insufficient_samples": skipped_insufficient,
        "symbols":                         output_symbols,
    }

    out_path.write_text(json.dumps(result, indent=2))
    logger.info(
        "Written %d symbols to %s  (%d skipped — insufficient samples)",
        len(output_symbols), out_path, len(skipped_insufficient),
    )
    if skipped_insufficient:
        logger.warning("Skipped: %s", ", ".join(skipped_insufficient))


if __name__ == "__main__":
    main()
