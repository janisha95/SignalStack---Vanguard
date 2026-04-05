# CC Phase 2a — Active-Profile Universe Resolver

**Target env:** `Vanguard_QAenv`
**Est time:** 1.5–2 hours
**Depends on:** Phase 0 PASS
**Prod-path touches:** NONE

---

## 0. Why this exists

Today the orchestrator polls/analyzes a broad universe, then V6 rejects most of it late. We waste cycles analyzing symbols no active profile can trade, and the shortlist/Telegram becomes noisy.

**Goal:** before V1/V2 even start, compute the **resolved universe** = union of symbols reachable by currently-active profiles in the current session. Only those symbols enter the pipeline.

---

## 1. Hard rules

1. QAenv only — no Prod paths anywhere.
2. Both **observe** and **enforce** modes must be supported. Observe logs the resolved set but doesn't constrain the pipeline. Enforce constrains it.
3. If `vanguard_runtime.json` is missing, malformed, or has no active profiles → orchestrator **refuses to start** and logs the exact error (config kill switch, see §2.5).
4. Resolution runs **once per cycle**, cached for the duration of that cycle (no re-resolving mid-cycle).
5. The debug endpoint must return machine-readable truth, not a Telegram-style human message.

---
Addendum 1 — Symbol Identity Contract
Paste target: CC_PHASE_2A_UNIVERSE_RESOLVER.md, new section before §2 "What to build"1.5 Symbol identity contractRule: internal canonical symbols are broker-neutral. Broker-specific forms live only at the execution boundary.Canonical form (used everywhere internally):

AAPL, MSFT, TSLA — equity
EURUSD, GBPUSD, USDJPY — forex (no slash, no suffix)
BTCUSD, ETHUSD, SOLUSD — crypto (no slash, no suffix)
Everything downstream of ingest uses canonical form: DB tables (vanguard_bars_*, vanguard_features, vanguard_predictions, vanguard_shortlist, vanguard_tradeable_portfolio, vanguard_trade_journal, vanguard_open_positions), JSON config universes, API payloads, UI, Telegram, logs.Broker symbols are a translation layer, not internal truth.Two required helpers: Vanguard_QAenv/vanguard/helpers/symbol_identity.py (new)pythondef to_canonical(source: str, raw_symbol: str) -> str:
    """
    Normalize source-native symbols into canonical internal form.
    source: 'alpaca' | 'twelve_data' | 'ibkr' | 'metaapi_gft' | 'yfinance'
    Examples:
      to_canonical('alpaca', 'AAPL')       -> 'AAPL'
      to_canonical('twelve_data', 'BTC/USD') -> 'BTCUSD'
      to_canonical('ibkr', 'EUR/USD')      -> 'EURUSD'
      to_canonical('metaapi_gft', 'EURUSD.x') -> 'EURUSD'
    Raises UnknownSymbolFormat on unexpected input — never guesses.
    """

def to_broker(route: str, canonical: str) -> str:
    """
    Translate canonical internal symbol to broker-native form at execution boundary.
    route: 'metaapi_gft' | 'signalstack_ttp' | 'alpaca_live' (future)
    Examples:
      to_broker('metaapi_gft', 'EURUSD') -> 'EURUSD.x'
      to_broker('metaapi_gft', 'AAPL')   -> 'AAPL.x'
      to_broker('metaapi_gft', 'BTCUSD') -> 'BTCUSD.x'
      to_broker('signalstack_ttp', 'AAPL') -> 'AAPL'
    Raises UnmappedSymbol if canonical symbol has no mapping for that route.
    """Translation tables driven by config (no hardcoded conversion rules in adapter code):json"execution": {
  "routes": {
    "metaapi_gft":   {"suffix_all": ".x"},
    "signalstack_ttp": {"suffix_all": ""}
  }
}Enforcement
All ingest adapters (alpaca_adapter.py, twelve_data_adapter.py, ibkr_adapter.py, metaapi_client.py read path) call to_canonical() on every symbol at the moment of ingest, before any DB write.
All execution adapters call to_broker() on every canonical symbol at the moment of submit, right before the broker API call.
Downstream code (V1–V6, lifecycle daemon, reconciler, API endpoints, UI) never handles source-raw or broker-raw symbols.
Config universe shapeThe universes.gft_universe.symbols block in vanguard_runtime.json uses canonical form only:json"gft_universe": {
  "type": "static",
  "symbols": {
    "crypto": ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LTCUSD", "BCHUSD"],
    "forex":  ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"],
    "equity": ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "META", "NVDA", "NFLX", "AMD", "INTC", "CRM", "ORCL", "ADBE", "JPM", "BAC"]
  }
}Acceptance tests (add to Phase 2a §3)Test 9 — Canonical form purity in DB
sqlSELECT DISTINCT symbol FROM vanguard_health WHERE symbol LIKE '%.x%' OR symbol LIKE '%/%';
Expect: zero rows. No .x suffix or slash anywhere internal.Test 10 — Translation round-trip
pythonassert to_broker('metaapi_gft', to_canonical('metaapi_gft', 'EURUSD.x')) == 'EURUSD.x'
assert to_canonical('twelve_data', 'BTC/USD') == 'BTCUSD'
assert to_broker('metaapi_gft', 'BTCUSD') == 'BTCUSD.x'
Expect: all assertions pass.Test 11 — Unmapped symbol fails loud
to_broker('metaapi_gft', 'UNKNOWNCOIN') raises UnmappedSymbol. No silent passthrough.Test 12 — Ingest adapters canonicalize at the door
After one cycle, grep -r "\.x\|BTC/USD\|EUR/USD" Vanguard_QAenv/vanguard/ --include="*.py" | grep -v symbol_identity.py | grep -v "# " returns zero matches.

## 2. What to build

### 2.1 File: `Vanguard_QAenv/config/vanguard_runtime.json` (new)

Starter shape — user will fill in profiles manually:

```json
{
  "config_version": "2026.04.05.01",
  "runtime": {
    "env": "qa-shadow",
    "resolved_universe_mode": "observe",
    "cycle_interval_s": 300
  },
  "universes": {
    "gft_universe": {
      "type": "static",
      "symbols": {
        "crypto": ["BTCUSD.x", "ETHUSD.x", "SOLUSD.x", "XRPUSD.x", "LTCUSD.x", "BCHUSD.x"],
        "forex":  ["EURUSD.x", "GBPUSD.x", "USDJPY.x", "AUDUSD.x", "USDCAD.x", "NZDUSD.x", "USDCHF.x"],
        "equity": ["AAPL.x", "MSFT.x", "GOOGL.x", "AMZN.x", "TSLA.x", "META.x", "NVDA.x", "NFLX.x", "AMD.x", "INTC.x", "CRM.x", "ORCL.x", "ADBE.x", "JPM.x", "BAC.x"]
      }
    },
    "ttp_equity_dynamic": {
      "type": "dynamic_equity",
      "source": "alpaca_tradable",
      "filters": {"min_price": 5.0, "min_avg_dollar_vol_30d": 5000000}
    }
  },
  "profiles": [
    {"id": "gft_5k",           "is_active": true,  "instrument_scope": "gft_universe",       "policy_id": "gft_standard_v1"},
    {"id": "gft_10k",          "is_active": true,  "instrument_scope": "gft_universe",       "policy_id": "gft_standard_v1"},
    {"id": "ttp_10k_swing",    "is_active": true,  "instrument_scope": "ttp_equity_dynamic", "policy_id": "ttp_swing_v1"}
  ],
  "session_windows_utc": {
    "crypto": {"days": [0,1,2,3,4,5,6], "open": "00:00", "close": "23:59"},
    "forex":  {"days": [0,1,2,3,4],     "open": "22:00", "close": "22:00_next"},
    "equity": {"days": [0,1,2,3,4],     "open": "13:30", "close": "20:00"}
  }
}
```

**Note:** `policy_templates` block is added in Phase 2b. This phase only cares about universe resolution.

### 2.2 File: `Vanguard_QAenv/vanguard/accounts/runtime_config.py` (new)

Loader + validator. Exports:

```python
def load_runtime_config(path: str = None) -> dict: ...
# - resolves path from env var VANGUARD_RUNTIME_CONFIG or falls back to default QA path
# - validates required top-level keys: config_version, runtime, universes, profiles, session_windows_utc
# - validates every profile has id, is_active, instrument_scope, policy_id
# - validates every instrument_scope referenced actually exists in universes
# - on any validation failure: raises ConfigValidationError with specific field path
# - logs loaded config_version to stdout

class ConfigValidationError(Exception): ...
```

### 2.3 File: `Vanguard_QAenv/vanguard/accounts/runtime_universe.py` (new)

The resolver itself. Exports:

```python
@dataclass(frozen=True)
class ResolvedUniverse:
    cycle_ts_utc: str
    active_profile_ids: list[str]
    expected_asset_classes: list[str]   # what's in session NOW
    in_scope_symbols: list[str]          # final union
    excluded: dict[str, str]             # {symbol: reason} for symbols in a universe but excluded this cycle
    mode: str                            # "observe" | "enforce"

def resolve_universe_for_cycle(config: dict, cycle_ts_utc: datetime) -> ResolvedUniverse:
    """
    1. Filter profiles where is_active=true.
    2. For each active profile, look up its instrument_scope → universe block.
    3. For static universes: take symbols matching asset classes in session RIGHT NOW.
    4. For dynamic universes (ttp_equity_dynamic): call the dynamic source loader (§2.4).
    5. Union all symbols, dedupe.
    6. Return ResolvedUniverse.
    """
```

**Session logic:** given `cycle_ts_utc`, determine which asset classes are currently open using `session_windows_utc`. A symbol is in scope only if its asset class is currently in session.

Example behavior:
- Saturday 15:00 UTC, only `gft_5k` + `gft_10k` active → only GFT crypto symbols in scope (forex/equity closed).
- Monday 14:00 UTC, all 3 profiles active → GFT crypto + GFT forex + GFT equity + TTP dynamic equity universe.
- Sunday 23:00 UTC (forex reopens) → GFT crypto + GFT forex (no equity).

### 2.4 File: `Vanguard_QAenv/vanguard/accounts/dynamic_universe_sources.py` (new)

One function per dynamic source type:

```python
def load_alpaca_tradable_equity(filters: dict) -> list[str]:
    """Query alpaca REST for tradable US equities, apply filters, return symbol list."""
```

For now, if Alpaca isn't configured, return a stub list of 50 large-cap tickers and log a warning. **Do not silently return empty.**

### 2.5 Wire into orchestrator: `Vanguard_QAenv/stages/vanguard_orchestrator.py` (edit)

At **orchestrator startup** (before `_main_loop()`):
1. Call `load_runtime_config()`. On `ConfigValidationError`, log error and **exit with code 1**. Do not start the loop.
2. Log `config_version` being used.
3. Attach config to orchestrator as `self._runtime_config`.

At **cycle start** inside `run_cycle()` (before `_run_v1()`):
1. Call `resolve_universe_for_cycle(self._runtime_config, cycle_ts)`.
2. Store as `self._current_resolved_universe`.
3. Write one row to new table `vanguard_resolved_universe_log` (see §2.7).
4. If `resolved_universe_mode == "enforce"`:
   - Pass `in_scope_symbols` into `_run_v1()` and `vanguard_prefilter.run()` as a filter.
   - V1 polls only those symbols. V2 reads only those symbols.
5. If `resolved_universe_mode == "observe"`:
   - Do NOT constrain V1/V2. Log what WOULD have been filtered.

### 2.6 File: V1 + V2 surface change

- `_run_v1()` accepts optional `in_scope_symbols: list[str] | None`. If set (enforce mode), polls only those. If None (observe mode), polls full universe as before.
- `vanguard_prefilter.run()` accepts `in_scope_symbols: list[str] | None`. Same behavior.

### 2.7 DB: new table `vanguard_resolved_universe_log`

```sql
CREATE TABLE IF NOT EXISTS vanguard_resolved_universe_log (
  cycle_ts_utc TEXT NOT NULL,
  config_version TEXT NOT NULL,
  mode TEXT NOT NULL,
  active_profile_ids TEXT NOT NULL,       -- JSON array
  expected_asset_classes TEXT NOT NULL,   -- JSON array
  in_scope_symbols TEXT NOT NULL,         -- JSON array
  in_scope_count INTEGER NOT NULL,
  excluded_count INTEGER NOT NULL,
  PRIMARY KEY (cycle_ts_utc)
);
```

### 2.8 Debug endpoint: `GET /api/v1/runtime/resolved-universe`

Returns the most recent row from `vanguard_resolved_universe_log` as JSON:

```json
{
  "cycle_ts_utc": "2026-04-05T18:00:00Z",
  "config_version": "2026.04.05.01",
  "mode": "observe",
  "active_profile_ids": ["gft_5k", "gft_10k", "ttp_10k_swing"],
  "expected_asset_classes": ["crypto"],
  "in_scope_symbols": ["BTCUSD.x", "ETHUSD.x", "SOLUSD.x", "XRPUSD.x", "LTCUSD.x", "BCHUSD.x"],
  "in_scope_count": 6,
  "excluded_count": 0
}
```

---

## 3. Acceptance tests

### Test 1 — Config kill switch works
Temporarily corrupt `vanguard_runtime.json` (delete `profiles` key). Start orchestrator.
**Expect:** exit code 1, stderr contains `ConfigValidationError: missing required key 'profiles'`.
Restore config.

### Test 2 — GFT-only crypto hours
Set all 3 profiles active. Run one cycle on a Saturday (or force-timestamp Sat 15:00 UTC).
**Expect:** `GET /api/v1/runtime/resolved-universe` returns exactly 6 GFT crypto symbols, no forex, no equity.

### Test 3 — Forex open adds forex
Force-timestamp Monday 08:00 UTC (forex open, equity closed).
**Expect:** in_scope = GFT crypto + GFT forex = 13 symbols.

### Test 4 — Equity open adds equity + TTP dynamic
Force-timestamp Monday 14:30 UTC.
**Expect:** in_scope = GFT crypto + GFT forex + GFT equity + TTP dynamic equity (so ~50+ symbols).

### Test 5 — Observe mode doesn't constrain pipeline
Set `resolved_universe_mode=observe`. Run one cycle. Check V2 processed full universe, log says "WOULD have filtered to N symbols."

### Test 6 — Enforce mode constrains pipeline
Set `resolved_universe_mode=enforce`. Force-timestamp Saturday. Run one cycle. Check `vanguard_health` rows for that cycle_ts contain **only** the 6 GFT crypto symbols.

### Test 7 — Deactivating TTP reduces scope
Flip `ttp_10k_swing.is_active=false`. Re-run Monday 14:30 cycle.
**Expect:** TTP dynamic equity symbols disappear, GFT 15 equity CFDs remain.

### Test 8 — Debug endpoint truth
Hit `GET /api/v1/runtime/resolved-universe` after a cycle. Verify `in_scope_count == len(in_scope_symbols)` and active profiles match JSON.

---

## 4. Report-back criteria

CC must produce:
1. List of files created/modified with line counts
2. Output of Tests 1–8 pasted verbatim
3. JSON dump from `/api/v1/runtime/resolved-universe` after running Test 2, 3, 4, 7
4. Confirmation that **no code path references any Prod DB path**
5. `grep -r "/SS/Vanguard/data" Vanguard_QAenv/` must return zero matches (only QA paths allowed)

---

## 5. Non-goals (this phase)

- No policy enforcement (Phase 2b)
- No position lifecycle (Phase 3)
- No config UI (Phase 5)
- No removal of V6 hardcoded GFT constants (Phase 2b does that)

---

## 6. Stop-the-line triggers

- Config fails to validate → stop, report exact error
- `grep` finds Prod path reference → stop, report matches
- Enforce mode doesn't actually constrain V2 (Test 6 fails) → stop, do not claim phase complete
