# CODEX — Timezone Display & Storage Fixes (3 Codebases)

## Design Rule (DO NOT CHANGE)
- **DB storage**: UTC ISO-8601 (correct everywhere except Meridian meta timestamps)
- **Operator display**: America/New_York (ET)
- **Session logic**: America/New_York (ET)

## Advance (S1) — 7 display bugs

All fixes: use existing `_et_now()` / `_to_et()` helpers from `s1_automation.py`.
If those aren't importable from the target files, add a minimal helper:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")
def _to_et(dt): return dt.astimezone(_ET) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(_ET)
def _now_et(): return datetime.now(_ET)
def _now_et_str(): return _now_et().strftime("%Y-%m-%d %H:%M:%S ET")
```

### Fix 1: s1_morning_report_v2.py:929
`ts_start` printed in raw UTC → convert to ET before display.

### Fix 2: s1_evening_report_v2.py:889
Raw UTC start timestamp → convert to ET before display.

### Fix 3: s1_orchestrator_v2.py:43
`_ts()` returns UTC for operator banner → change to ET.

### Fix 4: agent_server.py:597
`/operator/logs` uses naive `fromtimestamp(...)` → use `datetime.fromtimestamp(..., tz=_ET)`.

### Fix 5: agent_server.py:707
`/pipeline/status` `last_run` returns naive host-local → convert to explicit ET.

### Fix 6: agent_server.py:1369
Intelligence payload `as_of` returns raw UTC `Z` → convert to ET for operator display.

### Fix 7: agent_server.py:1459
Regime endpoint `as_of` returns raw UTC `Z` → convert to ET for operator display.

## Meridian — 6 DB storage bugs + 1 display bug

Meridian has ET helpers in `stages/factors/__init__.py`: `now_et()` / `now_et_iso()`.
The problem: these ET helpers are being used for DB writes (should be UTC).

Add a UTC helper alongside the ET ones:

```python
def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

### Fix 1: v2_orchestrator.py:136
`cache_meta.updated_at` written with `now_et_iso()` → change to `now_utc_iso()`.

### Fix 2: v2_orchestrator.py:160
`orchestrator_run_at` stored as ET → change to UTC.

### Fix 3: v2_prefilter.py:312
`prefilter_run_at` and `updated_at` stored in ET → change to UTC.

### Fix 4: v2_factor_engine.py:525
`factor_engine_run_at` stored in ET → change to UTC.

### Fix 5: v2_selection.py:296
`selection_run_at` stored in ET → change to UTC.

### Fix 6: v2_risk_filters.py:891
`risk_filters_run_at` stored in ET → change to UTC.

### Fix 7: v2_api_server.py:253
`/health` returns `datetime.now().isoformat()` (naive host-local) → change to explicit ET for operator display.

## Vanguard — 5 display bugs + 4 session logic bugs + 1 DST bug

Vanguard has proper helpers in `vanguard/helpers/clock.py`: `now_et()`, `iso_utc()`.

### Display fixes

### Fix 1: vanguard_orchestrator.py:640
Cycle-start log prints raw UTC `cycle_ts` → add ET conversion for the log line.

### Fix 2: vanguard_prefilter.py:586
Validation output prints raw UTC cycle timestamp → convert display to ET.

### Fix 3-4: unified_api.py:876 and :934
`/api/v1/portfolio/live` returns `as_of` in raw UTC → add `as_of_et` field or convert.

### Fix 5: vanguard_adapter.py:111
Candidate row `as_of` is raw `cycle_ts_utc` → add `as_of_display` in ET.

### Session logic fixes (CRITICAL — affects real trading behavior)

All in `vanguard/api/trade_desk.py`. The problem: `date.today()` uses server-local time, which may not match ET business day boundaries (especially around midnight ET vs midnight UTC).

Add import at top:
```python
from vanguard.helpers.clock import now_et
```

### Fix 6: trade_desk.py:435
Daily P&L boundary uses `date.today()` → use `now_et().date()`.

### Fix 7: trade_desk.py:461
Weekly boundary uses `date.today()` → use `now_et().date()`.

### Fix 8: trade_desk.py:485
Trades-today count uses server-local day → use `now_et().date()`.

### Fix 9: trade_desk.py:490
Volume-today count uses server-local day → use `now_et().date()`.

### DST bug

### Fix 10: meridian_adapter.py:41
ET display value is hardcoded as `-04:00` → use `ZoneInfo("America/New_York")` which handles DST automatically. `-04:00` is EDT only; EST is `-05:00`. Between November and March this is wrong.

## VERIFY

```bash
# Compile all modified files
python3 -m py_compile ~/SS/Advance/s1_morning_report_v2.py
python3 -m py_compile ~/SS/Advance/s1_evening_report_v2.py
python3 -m py_compile ~/SS/Advance/s1_orchestrator_v2.py
python3 -m py_compile ~/SS/Advance/agent_server.py
python3 -m py_compile ~/SS/Meridian/stages/v2_orchestrator.py
python3 -m py_compile ~/SS/Meridian/stages/v2_prefilter.py
python3 -m py_compile ~/SS/Meridian/stages/v2_factor_engine.py
python3 -m py_compile ~/SS/Meridian/stages/v2_selection.py
python3 -m py_compile ~/SS/Meridian/stages/v2_risk_filters.py
python3 -m py_compile ~/SS/Meridian/stages/v2_api_server.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_prefilter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/unified_api.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/trade_desk.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py

# Verify no test regressions
cd ~/SS/Vanguard && python3 -m pytest tests/ -x -q 2>&1 | tail -5
cd ~/SS/Meridian && python3 -m pytest tests/ -x -q 2>&1 | tail -5
```

## GIT

```bash
cd ~/SS/Advance && git add -A && git commit -m "fix: timezone display — convert 7 operator surfaces from UTC to ET"
cd ~/SS/Meridian && git add -A && git commit -m "fix: timezone — 6 DB meta timestamps UTC, /health ET display"
cd ~/SS/Vanguard && git add -A && git commit -m "fix: timezone — 5 display ET, 4 trade_desk session logic ET, DST fix"
```
