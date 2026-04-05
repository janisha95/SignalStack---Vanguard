# CC Phase 0 — QA vs Prod Parity Harness

**Target env:** `Vanguard_QAenv`
**Est time:** 30–45 min
**Blocks:** All subsequent phases. Must pass before Phase 2a merges.
**Prod-path touches:** READ-ONLY, from a one-time snapshot copy. See rule below.

---

## 0. Why this exists

QAenv was copied from Prod yesterday. We're about to bolt new runtime behavior onto it (universe resolver, JSON policy, lifecycle daemon). Before we touch anything, we need mechanical proof that QA produces the **same full-pipeline output as Prod** given the same cycle_ts. If parity is broken now, every future phase builds on drift.

This is a **snapshot diff**, not a live comparison. We run a single historical cycle on Prod, snapshot its inputs + outputs, then replay the same cycle in QA and diff.

---

## 1. Hard rules

1. **Zero writes to Prod DB.** Not even a `VACUUM`. Not even `BEGIN EXCLUSIVE`.
2. **Read-only snapshot first.** Use `sqlite3 prod.db ".backup /tmp/prod_snapshot.db"` → do all comparison against `/tmp/prod_snapshot.db`, never the live Prod DB.
3. **No new dependencies on Prod paths in QA code.** The harness lives in `Vanguard_QAenv/scripts/parity_harness.py` and reads the snapshot via an absolute path passed as a CLI arg.
4. **If snapshot read fails, fail loudly.** No silent fallback to "just compare QA to itself."

---

## 2. What to build

### 2.1 File: `Vanguard_QAenv/scripts/parity_harness.py`

A CLI script that takes:

```
--prod-snapshot-path /tmp/vanguard_prod_snapshot.db
--qa-db-path /Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db
--cycle-ts-utc 2026-04-04T14:30:00Z   # a real cycle that exists in Prod snapshot
--output-report /tmp/parity_report.md
```

**Behavior:**

1. Open Prod snapshot read-only (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`).
2. Open QA DB read-write.
3. For the given `cycle_ts_utc`, extract **full-pipeline outputs** from Prod snapshot for these tables:
   - `vanguard_health` (V2)
   - `vanguard_features` (V3)
   - `vanguard_predictions` (V4B scorer output)
   - `vanguard_shortlist` (V5)
   - `vanguard_tradeable_portfolio` (V6 approvals + rejects)
4. Check whether QA has a row for the same `cycle_ts_utc`. If not → **replay**:
   - Copy required input bars from Prod snapshot into QA DB (`vanguard_bars_1m`, `vanguard_bars_5m` for the 2-hour window ending at cycle_ts) — this is a QA write, allowed.
   - Run the QA orchestrator in single-cycle mode with `--force-cycle-ts <cycle_ts_utc> --execution-mode test --no-telegram`.
5. Diff the 5 output tables row-by-row. For each table, report:
   - row count Prod vs QA
   - rows missing in QA
   - rows missing in Prod
   - rows present in both with differing values (list columns that differ)
6. Write a markdown report to `--output-report`.
7. Exit code 0 only if all 5 tables match within tolerance (see §3).

### 2.2 Tolerance rules (per table)

| Table | Match key | Exact columns | Float-tolerant columns (ε=1e-6) |
|---|---|---|---|
| `vanguard_health` | `(cycle_ts_utc, symbol)` | `status`, `asset_class` | `last_bar_age_s`, `liquidity_score` |
| `vanguard_features` | `(cycle_ts_utc, symbol)` | `asset_class` | all numeric feature cols |
| `vanguard_predictions` | `(cycle_ts_utc, symbol, direction)` | `model_family` | `score`, `probability` |
| `vanguard_shortlist` | `(cycle_ts_utc, symbol, strategy)` | `direction`, `status` | `rank`, `composite_score` |
| `vanguard_tradeable_portfolio` | `(cycle_ts_utc, profile_id, symbol)` | `decision`, `reject_reason` | `qty`, `sl_price`, `tp_price` |

**Exact columns must match 100%. Float-tolerant columns must match within 1e-6 absolute.**

### 2.3 Report format

```markdown
# Parity Report — cycle_ts_utc=2026-04-04T14:30:00Z
Generated: <iso ts>
Prod snapshot: <path>
QA DB: <path>

## Summary
- Result: PASS | FAIL
- Tables matched: 5/5
- Total row divergence: 0

## Per-table results
### vanguard_health
- Prod rows: 47 | QA rows: 47
- Missing in QA: 0 | Missing in Prod: 0
- Value mismatches: 0
- Status: MATCH

... (same for other tables)

## Divergence detail (only if FAIL)
<full listing of mismatched rows>
```

---

## 3. Acceptance tests

Claude Code must run these and paste output in its report-back:

### Test 1 — Snapshot creation works
```bash
sqlite3 /Users/sjani008/SS/Vanguard/data/vanguard_universe.db ".backup /tmp/vanguard_prod_snapshot.db"
ls -lh /tmp/vanguard_prod_snapshot.db
# Expect: file exists, size > 100MB
```

### Test 2 — Snapshot is read-only when opened by harness
```bash
python3 Vanguard_QAenv/scripts/parity_harness.py \
  --prod-snapshot-path /tmp/vanguard_prod_snapshot.db \
  --qa-db-path /Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db \
  --cycle-ts-utc <pick-a-recent-prod-cycle> \
  --output-report /tmp/parity_report.md
# Expect: exit 0, report written, "Result: PASS"
```

### Test 3 — Harness detects injected drift
Manually insert one bogus row into QA's `vanguard_tradeable_portfolio` for the test cycle_ts, rerun harness.
Expect: exit != 0, report shows "Missing in Prod: 1" for that table.
Then delete the bogus row.

### Test 4 — Harness refuses to write to snapshot
Attempt to modify Prod snapshot via harness (you'll need to temporarily add a write call to prove the read-only open rejects it). Expect: SQLite raises `attempt to write a readonly database`.
Then remove the test write call.

---

## 4. Report-back criteria

CC must produce:

1. `Vanguard_QAenv/scripts/parity_harness.py` (new file, ~200 lines)
2. Output of Test 1, 2, 3, 4 pasted verbatim
3. The generated `/tmp/parity_report.md` contents
4. If Test 2 FAILs: list of diverging rows, and CC must **stop and report** — do not attempt to "fix" QA to match Prod. That's my call.

---

## 5. Non-goals

- No continuous parity monitoring daemon — this is one-shot, run-on-demand.
- No GUI / dashboard.
- No cross-phase parity (we'll re-run this harness after Phase 2a, 2b, etc.).
- No migration of Prod behavior to QA if drift is found. Report, stop, wait for instructions.

---

## 6. Stop-the-line triggers

If **any** of these happen, CC stops and reports:
- Prod snapshot cannot be opened read-only
- QA DB cannot be opened read-write
- cycle_ts_utc doesn't exist in Prod snapshot
- Replay fails (orchestrator errors out)
- Any diff > 0 on exact columns
