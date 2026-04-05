# CODEX — Fix Forex Pipeline + Remove V6 Constraints + IBKR Streaming

## Context
Equity intraday is working: 60 picks, V6 approving, 28s cycles.
But forex is broken: V2 shows 665 ACTIVE forex, V3 computes 0 forex features.
Also V6 HEAT_CAP is blocking 56/60 candidates. Remove all V6 testing constraints.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-forex-fix"
```

---

## PROBLEM 1: Forex Not Reaching V3 Factor Engine

V2 health shows 665 ACTIVE forex. But V3 factor_matrix has 0 forex rows.
V3 does compute crypto (14 rows). So V3 can handle non-equity — it just
doesn't receive forex survivors.

### Debug: Where do forex survivors drop out?

```bash
# 1. What does the orchestrator pass from V2 to V3?
grep -n "survivors\|v2.*result\|v3.*symbols\|factor.*engine\|run_v3\|asset_class" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30

# 2. Does the orchestrator filter by asset_class between V2 and V3?
grep -n "equity\|forex\|crypto\|asset_class.*filter\|skip.*forex\|only.*equity" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20

# 3. What does V3 receive as input? Does it filter by asset class?
grep -n "asset_class\|forex\|equity\|survivors\|input\|symbols" \
  ~/SS/Vanguard/stages/vanguard_factor_engine.py | head -20

# 4. Are IBKR forex symbols in universe_members?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, COUNT(*), SUM(is_active)
FROM vanguard_universe_members
WHERE asset_class = 'forex'
GROUP BY data_source
"

# 5. Does V2 output forex survivors for the latest cycle?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, status, COUNT(*)
FROM vanguard_health
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_health)
AND asset_class IN ('forex', 'crypto', 'equity')
GROUP BY asset_class, status
ORDER BY asset_class, status
"

# 6. What V3 actually processed in the latest cycle
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*)
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
GROUP BY asset_class
"
```

### Likely causes (check in order):

A) **Orchestrator filters survivors to equity-only before passing to V3.**
   Look for any filter like `survivors = [s for s in survivors if s.asset_class == 'equity']`
   FIX: Remove or expand to include forex and crypto.

B) **IBKR forex symbols not in vanguard_universe_members.**
   The orchestrator may only pass symbols that exist in universe_members.
   Check if IBKR forex pairs (EURUSD, GBPUSD, etc.) are registered.
   FIX: Register them:
   ```python
   # Add IBKR forex to universe_members
   for pair in ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCHF','USDCAD',
                 'EURGBP','EURJPY','GBPJPY','NZDUSD','AUDJPY','EURCHF',
                 'EURAUD','GBPAUD']:
       con.execute("""
           INSERT OR REPLACE INTO vanguard_universe_members
           (symbol, asset_class, data_source, universe, is_active, last_seen_at)
           VALUES (?, 'forex', 'ibkr', 'ibkr_forex', 1, datetime('now'))
       """, (pair,))
   ```

C) **V3 factor engine skips forex symbols** because it can't compute equity-specific features.
   Check if V3 has separate code paths for equity vs forex vs crypto.
   FIX: Ensure V3 computes what it can for forex (skip equity-only features).

D) **V2 survivors use a different cycle_ts_utc than V3 expects.**
   If V2 health rows have an older cycle_ts, V3 won't find them.
   FIX: Check timestamp alignment.

Report what you find, then fix it. The goal: forex symbols reach V3, get features,
reach V4B, get scored by the forex regressor ensemble (RF + LGBM), and appear in
V5 shortlist.

---

## PROBLEM 2: V6 HEAT_CAP Blocking Everything

V6 `HEAT_CAP` rejects 56/60 candidates. For testing, remove ALL V6 constraints:

```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
UPDATE account_profiles SET 
    max_positions = 30,
    max_per_sector = 50,
    max_portfolio_heat_pct = 1.0,
    max_single_position_pct = 1.0,
    max_batch_exposure_pct = 1.0,
    dll_headroom_pct = 0.99,
    dd_headroom_pct = 0.99,
    max_correlation = 1.0,
    max_trades_per_day = 100
WHERE id = 'TTP_20K_SWING';

UPDATE account_profiles SET 
    max_positions = 30,
    max_per_sector = 50,
    max_portfolio_heat_pct = 1.0,
    max_single_position_pct = 1.0,
    max_batch_exposure_pct = 1.0,
    dll_headroom_pct = 0.99,
    dd_headroom_pct = 0.99,
    max_correlation = 1.0,
    max_trades_per_day = 100
WHERE id = 'ftmo_100k';
"
```

Also check if HEAT_CAP is a code check or a profile check:
```bash
grep -n "HEAT_CAP\|heat_cap\|portfolio_heat\|max_heat" \
  ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -10
```

If it's a profile check, the SQL above fixes it. If it's hardcoded, fix the code too.

---

## PROBLEM 3: IBKR Streaming Adapter (Weekend Build)

Current IBKR adapter uses `reqHistoricalData` per symbol (165s for 200 symbols).
Rewrite to use `keepUpToDate=True` streaming.

### Architecture:

```python
class IBKRStreamingAdapter:
    """
    At startup: connect, qualify contracts in bulk, subscribe with keepUpToDate=True.
    Bars arrive via callbacks, written to DB continuously.
    Per-cycle V1: just reads latest bars from DB (instant, <0.5s).
    """
    
    def __init__(self, host='127.0.0.1', port=4001, client_id=10):
        import asyncio
        try: asyncio.get_event_loop()
        except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB
        self.ib = IB()
        self.valid_contracts = {}
        self.bad_symbols = set()
        self.subscriptions = {}
    
    def connect(self):
        self.ib.connect(self.host, self.port, self.client_id)
    
    def qualify_and_subscribe(self, symbols, asset_class='equity', db_path=None):
        """
        RUNS ONCE AT STARTUP.
        1. Bulk qualify all contracts (fast — one API call)
        2. Subscribe to keepUpToDate=True for each valid contract
        3. Bars arrive via callback → written to DB
        """
        from ib_insync import Stock, Forex, Crypto
        
        # Build contracts
        contracts = []
        for sym in symbols:
            if asset_class == 'forex':
                c = Forex(sym)
            elif asset_class == 'crypto':
                c = Crypto(sym, 'PAXOS', 'USD')
            else:
                c = Stock(sym, 'SMART', 'USD')
            contracts.append(c)
        
        # BULK qualify — much faster than one-by-one
        self.ib.qualifyContracts(*contracts)
        
        valid = [c for c in contracts if c.conId and c.conId > 0]
        bad = [c.symbol for c in contracts if not c.conId or c.conId <= 0]
        self.bad_symbols.update(bad)
        
        logger.info(f"Qualified {len(valid)}/{len(contracts)} {asset_class} contracts "
                     f"({len(bad)} bad: {bad[:5]})")
        
        # Subscribe with keepUpToDate=True
        for c in valid:
            what = 'MIDPOINT' if asset_class == 'forex' else 'TRADES'
            bars = self.ib.reqHistoricalData(
                c, endDateTime='',
                durationStr='3600 S',
                barSizeSetting='1 min',
                whatToShow=what,
                useRTH=False,          # Include extended hours
                keepUpToDate=True,     # STREAMING — no pacing limits
                formatDate=2
            )
            bars.updateEvent += lambda b, new, sym=c.symbol, ac=asset_class: \
                self._on_new_bar(sym, ac, b, new, db_path)
            self.subscriptions[c.symbol] = bars
            self.valid_contracts[c.symbol] = c
        
        logger.info(f"Streaming {len(self.subscriptions)} {asset_class} symbols")
    
    def _on_new_bar(self, symbol, asset_class, bars, has_new, db_path):
        """Callback fired automatically when new bar arrives."""
        if not has_new or not bars:
            return
        bar = bars[-1]
        import sqlite3
        con = sqlite3.connect(db_path, timeout=10)
        con.execute('PRAGMA journal_mode=WAL')
        # Convert bar open time to end time (add 60s for 1m bars)
        from datetime import timedelta
        bar_end = bar.date + timedelta(seconds=60)
        con.execute(
            "INSERT OR REPLACE INTO ibkr_bars_1m "
            "(symbol, bar_ts_utc, open, high, low, close, volume, asset_class, data_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ibkr')",
            (symbol, bar_end.isoformat(), bar.open, bar.high,
             bar.low, bar.close, int(bar.volume or 0), asset_class)
        )
        con.commit()
        con.close()
```

### Wire into orchestrator startup:

```python
# At startup (runs once):
self.ibkr = IBKRStreamingAdapter()
self.ibkr.connect()

# Equity: prefiltered 1500-2000 symbols
equity_syms = self.get_intraday_equity_universe()
self.ibkr.qualify_and_subscribe(equity_syms, 'equity', IBKR_INTRADAY_DB)

# Forex: all IDEALPRO pairs (free)
forex_syms = ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCHF','USDCAD',
              'EURGBP','EURJPY','GBPJPY','NZDUSD','AUDJPY','EURCHF',
              'EURAUD','GBPAUD']
self.ibkr.qualify_and_subscribe(forex_syms, 'forex', IBKR_INTRADAY_DB)

# Per-cycle V1 (runs every 5 min):
# Just read latest bars from DB — INSTANT (<0.5s)
# No reqHistoricalData calls, no pacing limits
```

### IBKR pacing notes:
- `keepUpToDate=True` has NO per-request pacing limit (it's a subscription, not a poll)
- But the INITIAL `reqHistoricalData` call to set up streaming still has the 60/10min limit
- So at startup, batch the initial subscriptions: 50 at a time, sleep 10s between batches
- After startup, bars flow continuously with zero pacing issues

### Also support `useRTH=False` for extended hours trading:
The adapter already uses `useRTH=False` in the code above. This gives pre-market
(4 AM ET) through after-hours (8 PM ET) data for equity + 24/5 for forex.

---

## Verification

```bash
# Run single cycle
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -30

# Check forex made it through
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) as features
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
GROUP BY asset_class
"
# Expected: equity|179, forex|14, crypto|14

# Check forex predictions
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, COUNT(*)
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class, direction
"
# Expected: equity LONG + SHORT, forex LONG + SHORT

# Check V6 approves more (heat cap removed)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account_id, status
"

# Start loop
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 2>&1 | tee ~/SS/logs/vanguard_loop.log &
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: forex pipeline + V6 constraints removed + IBKR streaming adapter"
```
