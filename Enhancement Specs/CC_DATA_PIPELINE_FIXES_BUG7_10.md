# CC Data Pipeline Fix Spec — Bugs 7-10

**Work directory:** `/Users/sjani008/SS/Vanguard/` (PROD — be surgical)
**Context:** Enforce mode is live. Phase2a resolves 33 GFT symbols correctly. But V1 data fetching and V2 filtering still have gaps.

---

## BUG 7: TwelveData adapter pulls 56 symbols instead of 33

**Symptom:** `[V1] Twelve Data asset filter=['crypto', 'forex'] symbols=56` — should be 33 (27 forex + 6 crypto).

**Root cause:** `twelvedata_adapter.py` reads from `vanguard_universe_members` table (which has 371+ rows from Alpaca equity materialization + legacy entries) instead of the Phase2a resolved universe.

**Fix:** In `vanguard/data_adapters/twelvedata_adapter.py`, wherever the symbol list is loaded for polling (look for `_load_symbols`, `get_active_symbols`, or the query that reads from `universe_members`), filter to ONLY symbols that are in the Phase2a resolved universe for the current cycle.

The resolved universe is available via:
```python
from vanguard.accounts.runtime_universe import resolve_universe
resolved = resolve_universe()  # returns dict with 'symbols' key
# OR the orchestrator can pass the resolved symbol list to the adapter
```

The simplest fix: the orchestrator already has the resolved universe from Phase2a. Pass it to the TwelveData adapter as a `symbol_filter` parameter:
```python
# In orchestrator V1:
resolved_symbols = phase2a_result['symbols']  # ['EURUSD', 'GBPUSD', ..., 'BTCUSD', ...]
td_bars = self._td_adapter.poll(symbol_filter=resolved_symbols)

# In twelvedata_adapter.py poll():
if symbol_filter:
    symbols = [s for s in symbols if s in symbol_filter]
```

**Verify:** `[V1] Twelve Data asset filter=['crypto', 'forex'] symbols=33` (not 56). Credit usage should be 33 per cycle, well within the 55/minute limit with no sleep needed.

---

## BUG 8: IBKR adapter retries 5 client IDs when IBKR is off

**Symptom:** When IBKR/TWS is not running, the adapter tries client IDs 10, 12, 13, 14, 15 on every cycle — 5 connection attempts that all fail immediately. This produces 30+ ERROR log lines per cycle.

**Root cause:** No "skip if already failed" logic. The adapter always tries all 5 client IDs.

**Fix:** In `vanguard/data_adapters/ibkr_adapter.py`:

1. After the FIRST connection failure in a cycle, set a flag `_ibkr_unavailable = True`
2. On subsequent calls in the same cycle, check the flag and skip immediately
3. Reset the flag at the start of each new cycle
4. Log ONE warning: `[IBKR] Not connected — skipping IBKR data source this cycle`

Alternatively, add a config flag in `vanguard_runtime.json`:
```json
"data_sources": {
    "ibkr": {
        "enabled": false,  // <-- flip to true when IBKR is running
        ...
    }
}
```

And check `config['data_sources']['ibkr']['enabled']` before attempting connection.

**Verify:** When IBKR is off, only 1 log line per cycle (not 30+).

---

## BUG 9: Alpaca materializes 371 equity rows even when equity is not in session

**Symptom:** Every boot cycle runs `materialize_universe_members: wrote 371 rows` — this takes 4+ seconds and writes 371 equity symbols to `universe_members` even though:
1. Equity market is CLOSED (weekend)
2. Enforce mode only has 15 GFT equity symbols
3. These 371 rows pollute the TwelveData symbol list (Bug 7)

**Root cause:** `universe_builder.get_equity_universe()` runs unconditionally at boot, pulling 10,569 Alpaca assets and filtering to 259 liquid ones, then materializing 371 rows. It doesn't check if equity is in-session or if enforce mode limits to 15 symbols.

**Fix (two parts):**

### Part A: Skip Alpaca materialization when equity is out of session
In `vanguard_orchestrator.py` boot sequence, check if equity is in the current resolved universe asset classes before running `materialize_universe`:
```python
resolved = resolve_universe()
active_classes = resolved.get('active_classes', [])
if 'equity' not in active_classes:
    log.info("[BOOT] Skipping Alpaca materialization — equity not in session")
else:
    materialize_universe(...)
```

### Part B: In enforce mode, materialize ONLY GFT equity symbols
When enforce mode is active and equity IS in session, don't materialize all 371 Alpaca survivors — only insert the 15 GFT equity symbols from the config:
```python
if runtime_config['runtime']['resolved_universe_mode'] == 'enforce':
    equity_symbols = runtime_config['universes']['gft_universe']['symbols']['equity']
    # Insert only these 15 symbols into universe_members
else:
    # Legacy behavior: full Alpaca materialization
    equity_symbols = get_equity_universe(...)
```

**Verify:** 
- Weekend/after-hours: No Alpaca API call at all, boot takes <1s instead of 4+s
- Market hours + enforce: Only 15 equity rows materialized (not 371)
- Market hours + observe: Full 371 rows (legacy behavior preserved)

---

## BUG 10: Telegram shortlist should show ALL ranked pairs (not truncated)

**Symptom:** The Telegram shortlist shows up to 10 per side (Bug 3 fix). But user wants to see ALL ranked pairs — currently 26 (12L + 14S), and eventually all 33 when crypto comes online.

**Current state:** `shortlist_display_top_n: 10` in JSON config.

**Fix:** Change the JSON config value to 30 (or higher than the max possible):
```json
"shortlist_display_top_n": 30
```

Also verify the Telegram message builder doesn't have a character limit that would truncate. Telegram messages max out at 4096 chars. A 33-symbol shortlist at ~50 chars per row = ~1650 chars — well within limits.

**Verify:** Telegram shortlist shows all 26+ pairs (once all warm up, should show all 33).

---

## Summary of changes

| Bug | File(s) | Change |
|-----|---------|--------|
| 7 | `twelvedata_adapter.py`, `vanguard_orchestrator.py` | Filter TD symbols to resolved universe |
| 8 | `ibkr_adapter.py` | Skip after first failure or check config flag |
| 9 | `vanguard_orchestrator.py`, `universe_builder.py` | Skip Alpaca when equity OOS; enforce limits to 15 GFT symbols |
| 10 | `config/vanguard_runtime.json` | Set `shortlist_display_top_n: 30` |

## Verification Checklist

After all fixes, restart orchestrator and run 2 cycles:

- [ ] V1 shows `symbols=33` for TwelveData (not 56)
- [ ] No TwelveData credit sleep (33 < 55 limit)
- [ ] IBKR shows max 1 warning line when not running (not 30+)
- [ ] Boot does NOT call Alpaca API when equity is out of session
- [ ] Boot takes <2s (not 4+s)
- [ ] Telegram shortlist shows all 26+ pairs
- [ ] V2 still processes all 33 resolved universe symbols
- [ ] No regression in V3-V6 pipeline

## Report-back

Show:
1. V1 log line with TwelveData symbol count
2. Boot timing (should be <2s without Alpaca)
3. Telegram screenshot showing full shortlist
4. IBKR log output (should be 1 line, not 30)
