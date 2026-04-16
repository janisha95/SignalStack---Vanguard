# VP45, Backfill, And Data Contract Findings

Date: 2026-04-13
Repo: `/Users/sjani008/SS/Vanguard_QAenv`

## Scope

This note captures the concrete work completed and the findings established during the Apr 12 to Apr 13 overnight debug session around:

- VP45 live serving quality
- Forex post-fix replay results
- Training/backfill blockers
- Twelve Data historical ingestion
- 1m to 5m aggregation performance

The goal was to separate real model weakness from bad live data contract and bad historical data coverage.

## Main Conclusions

1. VP45 had a real live serving bug.
2. Fixing VP freshness did not rescue VP45 in the first clean post-fix replay window.
3. The QA DB did not contain enough historical forex bars for meaningful rebuilds.
4. The training backfill code had a real date-window bug, but missing bars were the larger blocker.
5. Twelve Data historical ingestion worked, but the original control flow mishandled rate limits.
6. Historical download and aggregation were doing unnecessary work and were upgraded.
7. VP45 should not be treated as the active candidate anymore; it should only continue as an observation stream until replacement training is ready.

## Model Findings

### VP45 live behavior

Post-fix replay from `10:47 PM ET` onward using matured `30m` windows:

- Latest forex `1m` bar at replay time: `2026-04-13T04:40:00Z`
- Mature cycle cutoff: `2026-04-13T04:10:00Z`

Selected forex rows:

- All forex:
  - `n = 86`
  - hit rate: `18.6%`
  - avg signed `30m`: `-53.94 pips`
  - median signed `30m`: `-1.39 pips`
- G10-only:
  - `n = 67`
  - hit rate: `19.4%`
  - avg signed `30m`: `-2.05 pips`
  - median signed `30m`: `-1.2 pips`

Frequent bad G10 clusters in this replay:

- `USDCHF LONG`
- `AUDJPY SHORT`
- `AUDCAD SHORT`
- `AUDCHF SHORT`
- `CHFJPY SHORT`
- `NZDJPY SHORT`

Interpretation:

- The VP freshness fix was still worth doing.
- It did not make VP45 a usable live model.
- VP45 remains unfit for autonomous trading.

### Final overnight replay update

Final matured replay window reviewed before stopping:

- Requested window: `10:47 PM ET` to `2:24 AM ET`
- Latest forex `1m` bar at replay time: `2026-04-13T06:19:00Z`
- Effective matured replay cutoff: `2026-04-13T05:49:00Z`
- Matured selected rows: `203`

Results:

- All forex:
  - `n = 203`
  - hit rate: `43.8%`
  - avg signed `30m`: `+126.54 pips`
  - median signed `30m`: `-0.445 pips`
- G10-only:
  - `n = 154`
  - hit rate: `43.5%`
  - avg signed `30m`: `-0.643 pips`
  - median signed `30m`: `-0.445 pips`

Important note:

- The all-forex average was distorted by exotics and should not be treated as the clean read.
- The G10-only slice is the correct decision signal.

Small positive glimmer:

- `TIER_1`: `n = 11`, hit rate `63.6%`, median `+0.8 pips`
- `direction_streak >= 3`: `n = 12`, hit rate `58.3%`, median `+0.8 pips`
- `TIER_1 + streak >= 3`: same tiny sample, same direction

Final interpretation:

- Broad VP45 routing remained weak.
- The model was not rescued by the VP freshness fix.
- A tiny persistence/high-conviction subset showed some life, but sample size was too small to promote into the main strategy.

### Supporting offline finding

Earlier side-by-side and shootout investigation indicated:

- VP freshness and benchmark drift were real serving issues.
- VP45 still underperformed live even after the serving fix.
- Old prod model was only "less bad", not a validated answer.

## Data Contract Findings

### 1. VP freshness was broken

Problem:

- The factor engine was reading stale rows from `vanguard_features_vp`.
- Live factor rows were reusing old VP snapshots instead of rolling forward intraday.

Fix implemented:

- Added shared VP logic module and incremental refresh path.
- Added VP staleness guard so stale data is no longer silently reused.

Files changed:

- `/Users/sjani008/SS/Vanguard_QAenv/vanguard/features/volume_profile.py`
- `/Users/sjani008/SS/Vanguard_QAenv/vanguard/features/feature_computer.py`
- `/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_factor_engine.py`
- `/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_training_backfill.py`
- `/Users/sjani008/SS/Vanguard_QAenv/scripts/backfill_volume_profile.py`

### 2. Benchmark contract drift existed

Problem:

- Live forex benchmark resolution and training benchmark resolution were not aligned.
- This polluted benchmark-relative features.

Fix implemented:

- Unified forex benchmark candidate chain to:
  - `DXY`
  - `EURUSD`
  - `EUR/USD`

This affects future rebuilt training data and future live factor rows.

### 3. Historical training backfill had a date-window bug

Problem:

- `vanguard_training_backfill.py` loaded only the latest `50,000` bars per symbol.
- `--start-date` was applied after loading.
- Older history could not be reached even if present in DB.

Fix implemented:

- Added SQL-level `start_ts_utc` / `end_ts_utc` support in the DB bar loader.
- Training backfill now passes date bounds into SQL.

Files changed:

- `/Users/sjani008/SS/Vanguard_QAenv/vanguard/helpers/db.py`
- `/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_training_backfill.py`

Important follow-up finding:

- Even after fixing this, QA still lacked older forex bars.
- So the main blocker became missing source data, not backfill logic.

## Historical Data Findings

### 1. QA DB coverage was shallow

Core forex symbols in `vanguard_bars_5m` only went back to early April 2026.

Examples observed:

- `EURGBP`: starts `2026-04-02`
- `EURJPY`: starts `2026-04-02`
- `USDCAD`: starts `2026-04-02`
- `USDCHF`: starts `2026-04-02`
- `EURUSD`: starts `2026-04-02`

This explained why a full training rebuild did not move earlier than Aug 2025.

### 2. Twelve Data script worked, but the original flow was wrong

Observed behavior:

- It could fetch large history for a symbol.
- It would then hit the minute credit cap.
- After a quota hit, it often moved through later symbols with zero useful writes.

This was not a dead script. It was a bad control-flow implementation.

## Twelve Data Fixes Implemented

### 1. Adapter error propagation

Problem:

- The adapter logged API rate-limit errors but did not append them to `_last_errors`.
- The script could not distinguish "rate limited, retry" from "done".

Fix:

- API and HTTP historical errors now append to `_last_errors`.
- `_error_count` is incremented on those paths.

File changed:

- `/Users/sjani008/SS/Vanguard_QAenv/vanguard/data_adapters/twelvedata_adapter.py`

### 2. Retry-state bug in the backfill script

Problem:

- After a successful retry, the script still looked at an older stored rate-limit error.
- It kept sleeping as if the retry had failed.

Fix:

- Script now inspects only new errors from the current attempt.

File changed:

- `/Users/sjani008/SS/Vanguard_QAenv/scripts/backfill_twelvedata.py`

### 3. Deterministic request pacing

Problem:

- Original logic discovered the limit by failing.
- This wasted time and API budget.

Fix:

- Added rolling per-minute pacing.
- Added explicit `--max-calls-per-minute`.
- Current configured path uses `55`.

File changed:

- `/Users/sjani008/SS/Vanguard_QAenv/scripts/backfill_twelvedata.py`

### 4. Historical direct `5min` support

Problem:

- Pulling years of `1min` data for all forex symbols is not realistic under a `55/min` cap.
- Historical pipeline was forcing `1min` then aggregating upward.

Fix:

- Historical backfill now supports direct `5min` download.
- Default historical interval is now `5min`.
- Direct `5min` history writes into `vanguard_bars_5m`.

Files changed:

- `/Users/sjani008/SS/Vanguard_QAenv/scripts/backfill_twelvedata.py`
- `/Users/sjani008/SS/Vanguard_QAenv/vanguard/data_adapters/twelvedata_adapter.py`

## Aggregation Fixes Implemented

### Incremental 1m to 5m / 5m to 1h aggregation

Problem:

- `aggregate_non_equity_bars.py` rescanned full 1m history for every symbol every run.
- This is too slow for repeated top-ups.

Fix:

- Aggregation is now incremental by default.
- It only loads a recent overlap tail when 5m and 1h bars already exist.
- `--full-rebuild` remains available for a full rescan when explicitly needed.
- SQLite settings were also improved for this path.

File changed:

- `/Users/sjani008/SS/Vanguard_QAenv/scripts/aggregate_non_equity_bars.py`

## Running State At End Of Session

At the final check:

- Twelve Data historical backfill was running with:
  - `--asset-class forex`
  - `--start-date 2023-01-01`
  - `--end-date 2026-04-12`
  - `--interval 5min`
  - `--max-calls-per-minute 55`
- Orchestrator was also running in manual mode.

Important constraint:

- This created write contention against SQLite during debugging.
- Read-only queries were still possible, but broader introspection became slower.

## Recommended Next Steps

### Tomorrow morning

1. Verify historical forex `5m` bars actually landed back into `2023`.
2. Run forex training backfill on the expanded historical bar set.
3. Verify `vanguard_training_data` now spans the expanded date range.
4. Train replacement candidates on the rebuilt data.

### Model decision

Do not continue trying to save VP45 as the main candidate.

Use it only as:

- an overnight observation stream
- a serving-contract sanity check

Then move to the replacement candidate once rebuilt data is available.

## Commands To Reuse

### Verify historical `5m` bars

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db')
cur = conn.cursor()
for sym in ['EURUSD','GBPUSD','USDJPY','USDCHF','USDCAD','EURGBP','EURJPY','AUDUSD','NZDUSD','AUDCAD']:
    cur.execute("select min(bar_ts_utc), max(bar_ts_utc), count(*) from vanguard_bars_5m where symbol=?", (sym,))
    print(sym, cur.fetchone())
conn.close()
PY
```

### Incremental aggregation

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
python3 scripts/aggregate_non_equity_bars.py --asset-classes forex
```

### Forex training rebuild

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
python3 -u stages/vanguard_training_backfill.py --asset-class forex --workers 8 --start-date 2023-01-01 --full-rebuild
```

### Verify training-data span

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db')
cur = conn.cursor()
cur.execute("select count(*), min(asof_ts_utc), max(asof_ts_utc) from vanguard_training_data where asset_class='forex'")
print(cur.fetchone())
conn.close()
PY
```

## Bottom Line

Tonight did not produce a working VP45 live result.

It did produce several high-value truths:

- VP serving really was broken.
- Historical training rebuild really was blocked.
- QA forex bar coverage really was too shallow.
- Twelve Data historical ingestion really needed control-flow fixes.
- Incremental aggregation and direct `5min` history are the right path for speed.

That is enough to make tomorrow materially cleaner than today.
