# Precision Sniper v1 — High-Conviction Bidirectional Trading Strategy

**Version:** 1.0  
**Date:** 2026-04-05  
**Target:** Immediate implementation in Phase 2b (QAenv)  
**Goal:** Turn the current system into a low-frequency, high-RRR “bill printing machine” that passes GFT prop firm evaluations easily and scales profitably.
Addition: Think of state when data is stale, so the streak breaks because of data being stale and not the Instrument in action having inconsistent presisted signal.
This persistance gate or filter needs to think of stale session state. 
---

## 1. Core Philosophy

We do **not** trade every signal.  
We only trade the **highest-conviction setups** where:
- The model shows strong persistence (4/5 or 5/5 streak)
- Spread math is favorable (survives 3× spread buffer)
- Daily conviction (when available) aligns

Direction does **not** matter — LONG or SHORT is fine as long as the setup quality is elite.  
This is a true **Precision Sniper** strategy: rare, high-quality shots only.

---

## 2. Exact Trading Rules (Enforced in JSON Policy + Policy Engine)

### 2.1 Allowed Pairs (Low-Spread Whitelist)
Only these symbols are tradable:

**Crypto:**
- SOLUSD, LTCUSD, XRPUSD, DOGEUSD, ADAUSD, AVAXUSD

**Forex (when session open):**
- EURUSD, GBPUSD, USDJPY, AUDUSD

All other symbols are auto-rejected with reason `BLOCKED_SYMBOL_NOT_IN_SNIPER_WHITELIST`.

### 2.2 Entry Filter (New Reject Rule)
Only approve if:
- Streak in requested direction is **4/5 or 5/5** (use existing streak indicator)
- Current spread ≤ 0.5% of mid price (crypto_spread_config.max_allowed_spread_pct = 0.005)
- TP distance after spread buffer ≥ 3× spread

### 2.3 Risk & Position Rules
- Risk per trade: **0.5%** of account equity
- Max open positions: **2**
- Block duplicate symbols: true
- Max holding time: **240 minutes** (4 hours) with auto-close
- Daily loss pause: -3%
- Trailing drawdown pause: -5%

### 2.4 Spread-Aware Sizing (Already in Phase 2b)
Use the exact spread-aware math already specified in Phase 2b (effective entry = Bid for SHORT, Ask for LONG; TP must clear the opposite side).

---

## 3. JSON Policy Template (gft_precision_sniper_v1)

Add this exact template to `policy_templates` in `vanguard_runtime.json`:

```json
"gft_precision_sniper_v1": {
  "side_controls": {
    "allow_long": true,
    "allow_short": true
  },
  "position_limits": {
    "max_open_positions": 2,
    "block_duplicate_symbols": true,
    "max_holding_minutes": 240,
    "auto_close_after_max_holding": true
  },
  "sizing_by_asset_class": {
    "crypto": {
      "method": "risk_per_stop_spread_aware",
      "risk_per_trade_pct": 0.005,
      "min_qty": 0.000001,
      "min_sl_pct": 0.008,
      "min_tp_pct": 0.024,
      "spread_buffer_pct": 0.002,
      "max_spread_pct_to_enter": 0.005
    },
    "forex": {
      "method": "risk_per_stop_pips",
      "risk_per_trade_pct": 0.005,
      "min_sl_pips": 20,
      "min_tp_pips": 60
    }
  },
  "reject_rules": {
    "enforce_universe": true,
    "enforce_session": true,
    "enforce_position_limit": true,
    "enforce_heat_limit": true,
    "enforce_duplicate_block": true,
    "enforce_drawdown_pause": true,
    "enforce_streak_minimum": true,
    "enforce_spread_rules": true
  }
}
Assign this policy to your active GFT profiles in the profiles array.

4. Implementation Requirements in Phase 2b
Add these two new reject rules to policy_engine.py:

enforce_streak_minimum
If streak < 4 in requested direction → BLOCKED_LOW_STREAK

enforce_spread_rules
Already covered by crypto spread config, but now explicitly checked for all crypto trades.



5. Expected Performance (Realistic)

Trade frequency: 1–5 trades per day
Win rate on taken setups: 55–65%
Average RRR: 1:3 or better (after spread buffer)
Daily risk: very low → easy to stay inside GFT rules
Drawdown: minimal because of strict filtering

This strategy is specifically designed to:

Pass GFT evaluations with flying colors
Scale profitably once funded
Be psychologically easy to trust (you only trade elite setups)