# CC Master Prompt — QA Sync + Bug Fixes + Sprint 2 Day 1

**CRITICAL:** All changes in Vanguard_QAenv ONLY. Do NOT touch prod.
**Work directory:** `/Users/sjani008/SS/Vanguard_QAenv/`
**Prod directory (read-only reference):** `/Users/sjani008/SS/Vanguard/`

---

## STEP 0: Sync QA with Prod

Before any changes, sync QA environment with current prod state:

```bash
cd /Users/sjani008/SS
# Copy all prod files to QA (preserving QA's DB)
rsync -av --exclude='data/' --exclude='*.db' --exclude='__pycache__' \
  /Users/sjani008/SS/Vanguard/ /Users/sjani008/SS/Vanguard_QAenv/

# Copy the runtime config
cp /Users/sjani008/SS/Vanguard/config/vanguard_runtime.json \
   /Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json
```

Verify QA runs a clean cycle before making changes:
```bash
cd /Users/sjani008/SS/Vanguard_QAenv
python3 stages/vanguard_orchestrator.py --once --execution-mode manual
```

---

## BUG FIXES (do these FIRST, verify each one)

### Bug 15: V6 Telegram line too verbose

**Current:** Lists all 39 rejected symbol names
**Target:** Only list APPROVED symbol names, rejected as count

In `vanguard_orchestrator.py`, find where the V6 summary line is built.
Change from listing all rejected symbols to:
```
🧮 V6 GFT: ✅ 2 approved (SOLUSD, BTCUSD) | 🚫 39 rejected
```

Only the approved symbols listed by name. Rejected = count only.

### Bug 16: Equity CFD SL/TP/Lot blank

**Current:** Equity symbols show `SL - TP - Lot -` in shortlist
**Root cause:** V6 policy engine has no sizing logic for `asset_class='equity'`

**Fix in `vanguard/risk/policy_engine.py`:**
When asset_class is 'equity', apply ATR-based sizing:
```python
# equity sizing (already in JSON config as "atr_position_size")
if asset_class == 'equity':
    atr = get_atr(symbol)  # from V3 features
    sl_distance = atr * sizing_cfg.get('stop_atr_multiple', 1.5)
    tp_distance = atr * sizing_cfg.get('tp_atr_multiple', 3.0)
    risk_dollars = account_equity * sizing_cfg.get('risk_per_trade_pct', 0.005)
    qty = risk_dollars / sl_distance  # shares (not lots)
    qty = math.floor(qty)  # whole shares for equity
```

The JSON config already has the equity sizing block:
```json
"equity": {
    "method": "atr_position_size",
    "risk_per_trade_pct": 0.005,
    "stop_atr_multiple": 1.5,
    "tp_atr_multiple": 3.0
}
```

The policy_engine just needs to handle `method: "atr_position_size"` for equity.

Also add index and commodity sizing (same method):
```json
"index": {
    "method": "atr_position_size",
    "risk_per_trade_pct": 0.005,
    "stop_atr_multiple": 1.5,
    "tp_atr_multiple": 3.0
},
"commodity": {
    "method": "atr_position_size",
    "risk_per_trade_pct": 0.005,
    "stop_atr_multiple": 2.0,
    "tp_atr_multiple": 4.0
}
```

### Bug 17: Indices and Commodities missing from shortlist

**Current:** SPX500, UK100, US30, WTI, XAGUSD not in Telegram message
**Root cause:** V3 factor engine skips symbols without a trained model, OR V2 marks them STALE because no data source feeds them bars.

**Investigate first:**
```bash
cd /Users/sjani008/SS/Vanguard_QAenv
# Check if index symbols exist in universe_members
sqlite3 data/vanguard_universe.db "SELECT symbol, asset_class, data_source FROM vanguard_universe_members WHERE symbol IN ('SPX500','UK100','US30','WTI','XAGUSD','BRENT')"

# Check V2 health status
sqlite3 data/vanguard_universe.db "SELECT symbol, status, asset_class FROM vanguard_health WHERE symbol IN ('SPX500','UK100','US30','WTI','XAGUSD','BRENT') ORDER BY cycle_ts DESC LIMIT 20"

# Check if bars exist
sqlite3 data/vanguard_universe.db "SELECT symbol, COUNT(*) as bars FROM vanguard_bars_5m WHERE symbol IN ('SPX500','UK100','US30','WTI','XAGUSD','BRENT') GROUP BY symbol"
```

**If bars exist but V2 marks STALE:** Fix the V2 health gate for these asset classes.
**If NO bars exist:** These symbols need a data source. MT5 DWX provides them (Task 1 below). Short-term: add them to shortlist with neutral edge (0.50) and ATR-based SL/TP from whatever bar data exists.
**If no model exists:** V4B scorer should pass through with edge=0.50 (neutral) instead of skipping. The model directory in config has `"index": {"model_dir": "models/lgbm_index_v1"}` — check if this model file actually exists.

### Bug 18: Forex shortlist lot sizes all show 0.02

**Current:** Every forex pair shows `Lot 0.02` in the shortlist
**This is wrong.** For a $10K account with 0.5% risk ($50) and 20-pip SL:
- EURUSD: $50 / (20 pips × $10/pip) = 0.25 lots
- USDJPY: $50 / (20 pips × $6.27/pip) = 0.40 lots

The 0.02 comes from the FALLBACK display function, not V6.

**Fix in `_build_fallback_gft_display_details()` or `_build_fallback_gft_forex_details()`:**
Use the actual pip_value from the JSON config to compute correct lot sizes:
```python
pip_value_table = policy.get('sizing_by_asset_class', {}).get('forex', {}).get('pip_value_usd_per_standard_lot', {})
pip_value = pip_value_table.get(symbol, pip_value_table.get('default', 10.0))
risk_dollars = 50.0  # fallback assumes $50 risk
sl_pips = 20.0  # from config min_sl_pips
lot_size = risk_dollars / (sl_pips * pip_value)
```

This gives realistic lot sizes in the shortlist display even for non-approved pairs.

---

## SPRINT 2 TASKS (after bugs fixed)

### Task 1: DWX Adapter

See separate spec file: `CC_SPRINT2_DAY1_DWX_BACKFILL.md`

Summary: Build `mt5_dwx_adapter.py`, wire into V1 as primary data source, expose execution methods. Full spec with function signatures and config changes in the separate file.

### Task 2: Lifecycle Daemon with DWX Executor + Signal-Driven Exits

See separate spec file: `CC_SPRINT2_DAY1_DWX_BACKFILL.md`

Summary: Replace MetaApi with DWX calls. Add exit rules: close on signal flip, SL to breakeven on streak drop, min hold 120min, max hold 240min. All rules in JSON `exit_rules` section.

### Task 3: Backfill Scripts

See separate spec file: `CC_SPRINT2_DAY1_DWX_BACKFILL.md`

Summary: Write 3 scripts (MTF bar aggregation, Volume Profile features, training data assembly). User kicks off overnight. ~10-15 hours total runtime.

---

## EXECUTION ORDER

1. Sync QA with prod (Step 0)
2. Bug 15 fix (Telegram) — 15 min
3. Bug 16 fix (equity sizing) — 1 hr
4. Bug 17 fix (indices/commodities missing) — 1 hr (investigate first)
5. Bug 18 fix (forex fallback lot display) — 30 min
6. Run QA cycle, verify ALL 43 symbols show with SL/TP/Lot — 15 min
7. Task 1: DWX adapter — 2-3 hrs
8. Task 2: Lifecycle daemon + exit rules — 1-2 hrs
9. Task 3: Write backfill scripts — 1 hr (user runs overnight)

---

## VERIFICATION CHECKLIST

After all bug fixes (before Sprint 2 tasks):
- [ ] Telegram V6 line shows only approved symbol names, rejected as count
- [ ] All 15 equity symbols show SL/TP/Lot (not blank)
- [ ] Indices (SPX500, UK100, US30) appear in shortlist with edge values
- [ ] Commodities (WTI, XAGUSD) appear in shortlist with edge values
- [ ] Forex lot sizes vary by pair (not all 0.02)
- [ ] EURUSD fallback lot ≈ 0.25, USDJPY ≈ 0.40
- [ ] Crypto lot sizes still capped by max_notional_pct (SOLUSD < 10)
- [ ] Total Telegram message under 4096 chars (or split into 2 messages)
- [ ] All 43+ symbols visible in shortlist

After Sprint 2 tasks:
- [ ] DWX adapter connects to MT5, reads bars, writes to DB
- [ ] 1 orchestrator cycle runs with mt5_local data source
- [ ] Lifecycle daemon reads positions from DWX
- [ ] Signal-driven exit rules logged (not executing yet until promoted)
- [ ] 3 backfill scripts written and ready to run

**Report back with:** Telegram screenshot showing all symbols + DB row counts + DWX test output.
