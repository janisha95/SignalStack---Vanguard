# CC Prod Hotfix Spec — 5 Bugs

**Work directory:** `/Users/sjani008/SS/Vanguard/` (PROD — be surgical)  
**Do NOT refactor.** Targeted fixes only.  
**Do NOT touch:** V3, V4B, V5, lifecycle daemon, trade journal, reconciler, or any Phase 3 code.

---

## BUG 1: 9 accounts active instead of 2

**Symptom:** Session start Telegram shows:
```
Accounts: ttp_50k_intraday, ttp_100k_intraday, topstep_100k, ftmo_100k, TTP_20K_SWING, gft_5k, gft_10k, ttp_20k_swing, ttp_40k_swing
```

**Expected:** `Accounts: gft_10k, gft_5k`

**Root cause:** `load_accounts()` or `__init__` in `vanguard_orchestrator.py` reads from DB `account_profiles` table instead of the JSON config's `profiles` array.

**Fix:** In `stages/vanguard_orchestrator.py`, wherever active profiles are loaded at boot (look for `load_accounts`, `_load_profiles`, `get_active_profiles`, or similar), filter to ONLY profile IDs that appear in the JSON config's `profiles` array where `is_active=true`. The runtime config is loaded via `from vanguard.config.runtime_config import get_runtime_config` — call `get_runtime_config()` and read `config['profiles']` to get the authoritative list. The DB `account_profiles` table is legacy and must NOT be the source of truth.

**Verify:** Session start log and Telegram show only `gft_10k, gft_5k`.

---

## BUG 2: Crypto not flowing through V2

**Symptom:** Phase2a resolves 33 symbols (27 forex + 6 crypto). TwelveData polls 70 crypto bars successfully. But V2 log says:
```
V2 Prefilter | universe=ttp_equity | Checking 14 specified symbols
by_class={'forex': 14}
```
Crypto is completely missing from V2 onward.

**Root cause:** `stages/vanguard_prefilter.py` gets its symbol list from a hardcoded or legacy path (likely querying `vanguard_universe_members` for IBKR-streamed symbols only, or using a fixed `ttp_equity` universe name) instead of consuming the Phase2a resolved universe output.

**Fix:** In `stages/vanguard_prefilter.py`, find where the symbol list is sourced. The Phase2a resolved universe is written to `vanguard_resolved_universe_log` table by the orchestrator before V2 runs. The orchestrator also logs `[Phase2a] Resolved universe: mode=enforce profiles=['gft_10k', 'gft_5k'] classes=['crypto', 'forex'] symbols=33`.

The fix is one of:
1. Pass the resolved symbol list from the orchestrator directly to V2 (preferred — the orchestrator already has it)
2. Have V2 read from `vanguard_resolved_universe_log` for the current cycle

Find how the orchestrator calls V2. Look for something like:
```python
v2_result = run_prefilter(cycle_ts=..., symbols=..., ...)
```
The `symbols` parameter likely passes only IBKR symbols. It should pass ALL resolved universe symbols (forex + crypto).

Also fix the `universe=ttp_equity` label — it should reflect the actual universe being used (e.g., `gft_universe(enforce)`).

**Verify:** V2 log shows `Checking 33 specified symbols` and `by_class={'forex': 27, 'crypto': 6}` (or similar counts based on session).

---

## BUG 3: Telegram shortlist truncated to 5 per side

**Symptom:** Telegram shows only 5 LONG and 5 SHORT despite JSON config:
```json
"shortlist_display_top_n": 10
```

**Root cause:** The Telegram shortlist builder in `vanguard_orchestrator.py` has a hardcoded `[:5]` slice or similar limit.

**Fix:** Find the shortlist Telegram builder. Search for patterns like:
- `[:5]`
- `[:N]`
- `top_n = 5`
- `display_top_n`
- `SHORTLIST_DISPLAY`

Replace the hardcoded limit with:
```python
from vanguard.config.runtime_config import get_runtime_config
cfg = get_runtime_config()
display_n = cfg.get('runtime', {}).get('shortlist_display_top_n', 10)
```

Then use `display_n` as the slice limit for both LONG and SHORT sections.

**Verify:** Telegram shortlist shows up to 10 per side (or all if fewer than 10).

---

## BUG 4: Lot sizing uses flat $10 pip value for all pairs

**Symptom:** 
- USDCAD: Lot 0.25 (correct for USD-quoted pairs)
- EURCAD: Lot 0.02 (wrong — should be ~0.12–0.35 depending on pip value)
- All non-USD-quoted pairs show Lot 0.02 (the minimum)

**Root cause:** `pip_value_usd_per_standard_lot` in the policy config only has `"EURUSD": 10.0` and `"default": 10.0`. All pairs fall through to the default $10, which is only correct for pairs where USD is the quote currency.

**Fix (two parts):**

### Part A: Update `config/vanguard_runtime.json`

In BOTH `gft_standard_v1` and `gft_10k_v1` policy templates, replace the `pip_value_usd_per_standard_lot` block with:

```json
"pip_value_usd_per_standard_lot": {
  "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0, "NZDUSD": 10.0,
  "USDJPY": 6.27, "USDCAD": 7.19, "USDCHF": 11.24,
  "EURGBP": 12.60, "EURJPY": 6.27, "EURCAD": 7.19, "EURCHF": 11.24,
  "EURAUD": 6.50, "EURNZD": 5.70,
  "GBPJPY": 6.27, "GBPAUD": 6.50, "GBPCAD": 7.19, "GBPCHF": 11.24, "GBPNZD": 5.70,
  "AUDJPY": 6.27, "AUDCAD": 7.19, "AUDCHF": 11.24, "AUDNZD": 5.70,
  "CADJPY": 6.27, "CADCHF": 11.24, "CHFJPY": 6.27,
  "NZDCAD": 7.19, "NZDCHF": 11.24,
  "default": 10.0
}
```

These are approximate pip values at current exchange rates. The formula:
- USD as quote (EURUSD, GBPUSD, AUDUSD, NZDUSD): pip = $10.00 per standard lot
- USD as base, JPY as quote (USDJPY): pip = $10.00 / rate × 100 ≈ $6.27
- USD as base, non-JPY quote (USDCAD, USDCHF): pip = $10.00 / rate × 10000 / 10000 ≈ varies
- Cross pairs: derive from the quote currency's USD rate

### Part B: Verify sizing code reads the lookup

In `vanguard/risk/sizing_forex.py` (or wherever forex lot sizing happens), verify the code:
1. Reads `pip_value_usd_per_standard_lot` from the policy config
2. Looks up the CANONICAL symbol (e.g., `EURCAD`, not `EUR/CAD`)
3. Falls back to `default` if the symbol isn't in the map

The sizing formula should be:
```
risk_amount = account_size × risk_per_trade_pct  (e.g., 10000 × 0.005 = $50)
sl_pips = max(min_sl_pips, calculated_sl_pips)   (e.g., 20 pips)
pip_value = lookup[symbol] or lookup["default"]    (e.g., 7.19 for EURCAD)
lots = risk_amount / (sl_pips × pip_value)         (e.g., 50 / (20 × 7.19) = 0.35 lots)
```

If `sizing_forex.py` doesn't exist or doesn't do this lookup, find where forex sizing happens and add the lookup.

**Verify:** Lot sizes vary by pair:
- EURUSD: ~0.25 lots (pip_value=10.0)
- USDJPY: ~0.40 lots (pip_value=6.27)  
- EURGBP: ~0.20 lots (pip_value=12.60)
- EURCAD: ~0.35 lots (pip_value=7.19)
- CHFJPY: ~0.40 lots (pip_value=6.27)

---

## BUG 5: V2 universe label says `ttp_equity`

**Symptom:** `V2 Prefilter | universe=ttp_equity` — this is a legacy label from before the JSON config layer.

**Root cause:** V2 has a hardcoded universe name string or reads from old config.

**Fix:** In `stages/vanguard_prefilter.py`, find where the `universe=` label is set in the log line. Replace with the actual resolved universe scope from the runtime config, e.g.:
```python
universe_label = f"gft_universe(enforce)"  # or read from runtime config
```

**Verify:** No `universe=ttp_equity` in logs.

---

## Verification Checklist

After ALL 5 fixes, restart the orchestrator and run ONE full cycle. Verify:

- [ ] Session start Telegram shows ONLY `gft_10k, gft_5k` (not 9 accounts)
- [ ] V2 log shows 33+ symbols (not 14)
- [ ] V2 `by_class` includes both `forex` and `crypto`
- [ ] Telegram shortlist shows up to 10 per side (not 5)
- [ ] Crypto section in Telegram is populated (not `No GFT crypto rows`)
- [ ] V6 crypto rows appear for gft_10k and gft_5k with proper lot sizes
- [ ] Lot sizes vary by pair (not flat 0.02/0.25)
- [ ] No `universe=ttp_equity` in V2 logs

## Report-back

Produce a report showing:
1. Each checklist item with PASS/FAIL
2. Sample Telegram messages (shortlist + V6 + execution)
3. V2 log line showing symbol count and asset class breakdown
4. Three example lot sizes from V6 output showing different pip values applied
