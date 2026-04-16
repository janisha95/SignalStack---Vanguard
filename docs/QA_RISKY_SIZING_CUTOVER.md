# QA Risky Sizing Cutover

This QA pass moves active FX/Crypto sizing ownership into the Risky-backed
Vanguard path.

Current state after this patch:
- Risky is the final sizing owner for forex and crypto.
- Sizing inputs come from DB truth:
  - context quote/spec/account tables
  - forex pair-state / crypto symbol-state
  - `vanguard_v5_tradeability`
  - `vanguard_v5_selection`
- `WEAK` and `FAIL` tradeability rows are rejected before sizing.
- Missing live spread/account/spec truth blocks sizing; no synthetic fallback.
- `vanguard_risky_decisions` is the canonical audit table for risk/sizing results.

Deferred to next pass:
- full FX TP/SL redesign
- full crypto TP/SL redesign
- lifecycle / horizon-exit cleanup
