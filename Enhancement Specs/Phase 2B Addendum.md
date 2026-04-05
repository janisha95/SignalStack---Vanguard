Addendum — Crypto Spread-Aware Sizing (corrected)
Paste target: CC_PHASE_2B_JSON_POLICY.md, replaces the crypto sub-block inside sizing_by_asset_class and adds a new top-level crypto_spread_config. Delete my old spread_buffer_pct and max_spread_pct_to_enter fields.

2.2.1 Crypto spread-aware sizing (exact implementation)
Top-level config block (add to vanguard_runtime.json):
json"crypto_spread_config": {
  "enabled": true,
  "fetch_live_spread": true,
  "min_tp_spread_multiple": 3.0,
  "max_allowed_spread_pct": 0.5,
  "emergency_default_spread_pct": 0.5,
  "fallback_spreads_absolute": {
    "BTCUSD": 80.0,
    "ETHUSD": 2.50,
    "SOLUSD": 0.30,
    "BNBUSD": 1.50,
    "XRPUSD": 0.005,
    "LTCUSD": 0.20,
    "DOGEUSD": 0.001,
    "ADAUSD": 0.002,
    "AVAXUSD": 0.10,
    "DOTUSD": 0.02,
    "LINKUSD": 0.05,
    "ATOMUSD": 0.02,
    "UNIUSD": 0.03,
    "AAVEUSD": 0.50
  }
}
Notes on config values:

max_allowed_spread_pct: 0.5 (not 1.0) — at 1%, GFT crypto edge is gone after round-trip cost. Keep tight.
fallback_spreads_absolute values are GFT-observed spreads from your manual trading — refresh these monthly or move to percentage-based as coin prices drift.
min_tp_spread_multiple: 3.0 — TP distance must be ≥ 3× current spread, otherwise TP won't clear round-trip costs + slippage.

Spread resolution priority (in sizing_crypto.py):

If fetch_live_spread=true: pull live (bid, ask) from MetaApi → spread = ask - bid
If live fetch fails OR fetch_live_spread=false: look up fallback_spreads_absolute[symbol]
If symbol not in fallback dict: spread = emergency_default_spread_pct / 100 * mid_price, log warning

Corrected SL/TP math (in vanguard/risk/sizing_crypto.py):
pythondef size_crypto_spread_aware(mid_price, bid, ask, atr, side, policy_crypto, crypto_spread_config):
    spread = ask - bid
    mid = (bid + ask) / 2.0
    spread_pct = spread / mid

    # Gate 1: spread too wide
    if spread_pct > crypto_spread_config["max_allowed_spread_pct"] / 100.0:
        return REJECT("BLOCKED_SPREAD_TOO_WIDE", {"spread_pct": spread_pct, "max": crypto_spread_config["max_allowed_spread_pct"]})

    # Compute SL/TP distances from policy (ATR-based or percentage-based)
    sl_distance = max(policy_crypto["min_sl_pct"] * mid, atr * policy_crypto.get("sl_atr_multiple", 1.5))
    tp_distance = max(policy_crypto["min_tp_pct"] * mid, atr * policy_crypto.get("tp_atr_multiple", 3.0))

    # Gate 2: TP distance must clear at least N× spread
    min_tp_multiple = crypto_spread_config["min_tp_spread_multiple"]
    if tp_distance < min_tp_multiple * spread:
        return REJECT("BLOCKED_TP_UNREACHABLE", {"tp_distance": tp_distance, "spread": spread, "min_multiple": min_tp_multiple})

    # Place SL/TP assuming MetaApi/MT5 convention:
    #   LONG closes at BID  -> SL/TP are bid-level triggers
    #   SHORT closes at ASK -> SL/TP are ask-level triggers
    # No extra spread term — entry-side already absorbed it.
    if side == "LONG":
        effective_entry = ask           # what we pay on open
        sl_price = effective_entry - sl_distance   # bid level
        tp_price = effective_entry + tp_distance   # bid level
    else:  # SHORT
        effective_entry = bid           # what we receive on open
        sl_price = effective_entry + sl_distance   # ask level
        tp_price = effective_entry - tp_distance   # ask level

    # Realized P&L if triggered (for verification):
    # LONG:  loss_at_sl  = ask_entry - sl_bid = sl_distance
    #        profit_at_tp = tp_bid - ask_entry = tp_distance
    # SHORT: loss_at_sl  = sl_ask - bid_entry = sl_distance
    #        profit_at_tp = bid_entry - tp_ask = tp_distance

    qty = account_equity * policy_crypto["risk_per_trade_pct"] / sl_distance
    qty = max(qty, policy_crypto["min_qty"])

    notes = [f"spread={spread:.4f}", f"spread_pct={spread_pct*100:.3f}%", f"tp_distance={tp_distance:.4f}", f"tp_spread_ratio={tp_distance/spread:.2f}x"]

    return APPROVE(qty, sl_price, tp_price, notes)
Why this math differs from Grok's:

Grok's formula adds one extra spread term to both SL and TP. That makes realized loss = sl_distance + spread when SL hits (exceeds intended stop by one spread) and realized profit = tp_distance + spread when TP hits.
The form above keeps realized P&L = sl_distance / tp_distance exactly, assuming MetaApi's bid-trigger convention for longs and ask-trigger convention for shorts.
If your broker actually triggers longs on ask / shorts on bid (some retail brokers do), revert to Grok's formulas. Verify once with a test trade before locking either convention.

Per-asset-class config stays in policy template (unchanged except buffer fields removed):
json"crypto": {
  "method": "risk_per_stop_spread_aware",
  "risk_per_trade_pct": 0.005,
  "min_qty": 0.000001,
  "min_sl_pct": 0.008,
  "min_tp_pct": 0.024,
  "sl_atr_multiple": 1.5,
  "tp_atr_multiple": 3.0
}
Note: min_tp_pct bumped from 0.016 to 0.024 so that on a 0.5% spread, tp_distance = 0.024 * mid easily clears 3 × spread = 1.5% * mid. Tune after first 10 live crypto trades.

Reject reasons (add to reject_rules.py):
python"spread_too_wide": "Current spread {spread_pct}% exceeds max allowed {max}%",
"tp_unreachable": "TP distance {tp_distance} < {min_multiple}× spread ({spread}) — round-trip cost not coverable",

Updated acceptance tests for Phase 2b §3:
Test 5 (revised) — Crypto spread-aware TP clears min multiple
Inject ETH SHORT candidate: bid=2000, ask=2002.08 (spread=2.08), min_tp_pct=0.024 → tp_distance = 48. Spread-multiple check: 48 / 2.08 = 23× (>>3× threshold). Execute.
Expect: APPROVED, tp_price = 2000 - 48 = 1952, notes include tp_spread_ratio=23.08x.
Test 5b (new) — TP rejected when too close to spread
Inject crypto candidate where tp_distance = 2 * spread (below 3× threshold).
Expect: BLOCKED_TP_UNREACHABLE, reject_reason includes multiplier detail.
Test 5c (new) — Fallback spread used on live-fetch failure
Mock MetaApi live quote failure for BTCUSD. Run sizing.
Expect: uses fallback value 80.0, log warning "live_spread_unavailable_using_fallback".
Test 5d (new) — Emergency default for unlisted symbol
Inject candidate for PEPEUSD (not in fallback_spreads). Mock live failure.
Expect: uses emergency_default_spread_pct × mid_price, warning logged.
Test 5e (new) — Realized P&L math verification (paper trade)
Place a real test SHORT on ETHUSD with known bid/ask. Wait for TP or SL hit. Compare realized P&L from broker fill to intended tp_distance / sl_distance.
Expect: realized within ± one spread of intended. If ± two or more spreads off → broker trigger convention is reverse of assumed → switch to Grok's formula.