# CC Spec: Crypto Sizing via JSON Config + Telegram Fix

**Priority:** URGENT — crypto lot sizes are dangerously wrong
**Principle:** All sizing logic must be readable from JSON config. No magic numbers in Python.

---

## STEP 0: Query DWX for contract sizes (do this FIRST)

Run this script to get the actual contract specs from MT5:

```python
#!/usr/bin/env python3
"""Query MT5 via DWX for crypto contract sizes."""
import sys, time, json
sys.path.insert(0, '/tmp/dwxconnect/python')
from api.dwx_client import dwx_client

MT5_FILES = '/Users/sjani008/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files'

client = dwx_client(metatrader_dir_path=MT5_FILES)
time.sleep(2)

# Subscribe to all GFT crypto symbols to get tick data
symbols = ['BTCUSD.x', 'ETHUSD.x', 'SOLUSD.x', 'BNBUSD.x', 'LTCUSD.x', 'BCHUSD.x']
client.subscribe_symbols(symbols)
time.sleep(3)

print("Market data:", json.dumps(client.market_data, indent=2))
print("\nAccount info:", json.dumps(client.account_info, indent=2))

# The contract_size is NOT in DWX's standard API.
# We need to infer it from the tick_value field.
# tick_value = profit per 1-pip move per 1 lot
# For crypto: if 1 lot = 1 coin, tick_value = 1.0 (USD-quoted pairs)
# If 1 lot = 10 coins, tick_value = 10.0

client.close()
```

**Report the tick_value for each crypto symbol.** This tells us the contract size.

---

## STEP 1: Add crypto sizing config to vanguard_runtime.json

Add this block under `policy_crypto`:

```json
"policy_crypto": {
    "risk_pct": 0.005,
    "max_notional_pct": 0.05,
    "min_sl_pct": 0.008,
    "min_tp_pct": 0.024,
    "min_qty": 1e-06,
    "contract_sizes": {
        "BTCUSD": 1,
        "ETHUSD": 1,
        "SOLUSD": 1,
        "BNBUSD": 1,
        "LTCUSD": 1,
        "BCHUSD": 1
    },
    "sizing_formula": "notional_cap",
    "sizing_comment": "qty = min(risk / sl_distance, max_notional_pct * equity / price). Caps position to max_notional_pct of account."
}
```

The key parameter is `max_notional_pct`. With 5% cap on a $10K account:
- SOL at $82: max 6.1 lots ($500 notional) — reasonable
- BNB at $605: max 0.83 lots ($500 notional) — reasonable  
- BTC at $69K: max 0.007 lots ($500 notional) — reasonable
- ETH at $2144: max 0.23 lots ($500 notional) — reasonable

The formula is:
```
risk_qty = risk_dollars / sl_distance          # pure risk-based
notional_qty = max_notional_pct * equity / price  # notional cap
final_qty = min(risk_qty, notional_qty)
```

---

## STEP 2: Update sizing function

In `vanguard/risk/sizing.py`, function `size_crypto_spread_aware()`:

```python
def size_crypto_spread_aware(...):
    # ... existing SL/TP calculation ...
    
    # Read from config (NOT hardcoded)
    max_notional_pct = policy_crypto.get("max_notional_pct", 0.05)
    contract_size = policy_crypto.get("contract_sizes", {}).get(symbol, 1)
    
    # Risk-based qty (existing formula, adjusted for contract size)
    risk_qty = risk_dollars / (sl_distance * contract_size)
    
    # Notional cap: max X% of equity per position
    notional_qty = (max_notional_pct * account_equity) / (mid * contract_size)
    
    # Take the smaller of the two
    qty = min(risk_qty, notional_qty)
    qty = max(qty, policy_crypto.get("min_qty", 1e-6))
    
    return qty
```

**DO NOT add any other logic.** The JSON config is the single source of truth.

---

## STEP 3: Fix Telegram messages (Bug 13)

**Current:** 3 messages per cycle (shortlist + V6 crypto + V6 execution)
**Target:** 1 message per cycle (consolidated shortlist with V6 overlay)

In `vanguard_orchestrator.py`:

1. Remove the separate `_build_v6_summary_telegram()` call for crypto
2. The shortlist already shows crypto with lot sizes from gft_overlay
3. Append a single line at the bottom of the crypto section: `"V6: X approved, Y rejected (position limit)"`
4. Keep the forex V6 execution message (it's useful for manual trading)

**Also fix equity filter:** When `universe_mode == 'enforce'`, filter equity shortlist rows to ONLY the 15 GFT equity symbols from `runtime_config['universes']['gft_universe']['symbols']['equity']`. Currently showing NFLX/TSLA/NVDA from Alpaca — these should NOT appear.

---

## Verification

After changes, run 1 cycle and verify:

- [ ] SOLUSD lot size < 10 (not 75)
- [ ] BNBUSD lot size < 2 (not 10)  
- [ ] BTCUSD lot size ~0.007-0.009
- [ ] ETHUSD lot size ~0.23
- [ ] Only 1 Telegram message for shortlist (no separate crypto V6)
- [ ] Equity section shows only GFT 15 symbols (not NFLX/TSLA/NVDA)
- [ ] `max_notional_pct` is readable in vanguard_runtime.json
- [ ] Changing `max_notional_pct` in JSON changes lot sizes next cycle

**Report:** Show the JSON config block + Telegram screenshot + lot sizes for all 6 crypto symbols.
