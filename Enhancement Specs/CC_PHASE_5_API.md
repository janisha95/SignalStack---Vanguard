# CC Phase 5 — Backend API for UI + Shadow Execution Log

**Target env:** `Vanguard_QAenv`
**Est time:** 45 min – 1 hour
**Depends on:** Phase 4 PASS
**Prod-path touches:** NONE

---

## 0. Why this exists

React admin UI comes later, but the backend config contract has to exist first. This phase exposes **safe read + validated write endpoints** for runtime config, profiles, universes, policy templates, and live runtime state. Also adds **shadow execution log** so paper-trade behavior is logged and diff-able against future real fills.

---

## 1. Hard rules

1. QAenv only — these endpoints live on the QA FastAPI server (port 8090), not prod.
2. **Every PUT validates before write.** Malformed JSON returns 400 with exact validation error, never corrupts the config file.
3. **Every PUT writes atomically.** Write to `<path>.tmp`, fsync, rename. Never partial writes.
4. **Every config write increments `config_version`** and appends to `vanguard_config_audit_log`.
5. **No auth for now** (single-user local dev). Add `X-Auth: super-user` header check as a placeholder that any value passes.
6. Shadow execution log writes **every would-be-submit** when `execution_mode=manual|test`, so you can diff paper vs real later.

---

## 2. What to build

### 2.1 New DB table: `vanguard_config_audit_log`

```sql
CREATE TABLE IF NOT EXISTS vanguard_config_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  updated_at_utc TEXT NOT NULL,
  old_config_version TEXT,
  new_config_version TEXT NOT NULL,
  section_changed TEXT NOT NULL,         -- runtime | profiles | universes | policy_templates | temporary_overrides | calendar_blackouts | full
  diff_summary TEXT NOT NULL,            -- human-readable summary
  full_new_config_json TEXT NOT NULL     -- snapshot for rollback
);
```

### 2.2 New DB table: `vanguard_shadow_execution_log`

```sql
CREATE TABLE IF NOT EXISTS vanguard_shadow_execution_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_ts_utc TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  would_have_submitted_at_utc TEXT NOT NULL,
  qty REAL NOT NULL,
  expected_entry REAL NOT NULL,
  expected_sl REAL NOT NULL,
  expected_tp REAL NOT NULL,
  execution_mode TEXT NOT NULL,          -- manual | test
  metaapi_payload_json TEXT NOT NULL,    -- exact payload that WOULD have been sent
  notes TEXT
);
```

### 2.3 API endpoints (FastAPI router)

Create `Vanguard_QAenv/vanguard/api/runtime_config_router.py`:

#### Read endpoints
```
GET  /api/v1/config/runtime          -> full runtime block
GET  /api/v1/config/profiles         -> all profiles
GET  /api/v1/config/universes        -> all universes  
GET  /api/v1/config/policy-templates -> all policy templates
GET  /api/v1/config/overrides        -> temporary_overrides
GET  /api/v1/config/blackouts        -> calendar_blackouts
GET  /api/v1/config/full             -> entire config + config_version
GET  /api/v1/config/audit-log?limit=20 -> recent config changes
```

#### Write endpoints
```
PUT  /api/v1/config/runtime          
PUT  /api/v1/config/profiles
PUT  /api/v1/config/universes
PUT  /api/v1/config/policy-templates
PUT  /api/v1/config/overrides
PUT  /api/v1/config/blackouts
```

Each PUT:
1. Validates the submitted JSON against the full-config validator from `runtime_config.py`.
2. Reads current config, merges the section, re-validates the full config.
3. Bumps `config_version` to `{YYYY.MM.DD}.{sequence}` where sequence is today's change count + 1.
4. Atomic file write (`.tmp` → `fsync` → rename).
5. Appends to `vanguard_config_audit_log`.
6. Returns `{"success": true, "new_config_version": "...", "reload_required": true}`.
7. If validation fails: returns 400 with `{"success": false, "errors": [...]}` and **does not touch the file**.

#### Runtime state endpoints
```
GET /api/v1/runtime/resolved-universe     -> from Phase 2a
GET /api/v1/runtime/account-state         -> all profiles with equity, dd, paused status
GET /api/v1/runtime/open-positions        -> from Phase 3 vanguard_open_positions
GET /api/v1/runtime/trade-journal?profile=&status=&limit= -> filtered journal rows
GET /api/v1/runtime/reconciliation-events?limit= -> recent reconciliation log
GET /api/v1/runtime/shadow-executions?since= -> shadow executions for diff
GET /api/v1/runtime/config-version        -> currently-loaded config_version + loaded_at
```

#### Lifecycle control endpoints
```
POST /api/v1/lifecycle/reload-config      -> tells orchestrator to reload config on next cycle
POST /api/v1/lifecycle/pause-profile      -> manually pause a profile (body: {profile_id, until_utc, reason})
POST /api/v1/lifecycle/unpause-profile    -> clear manual pause (body: {profile_id})
```

### 2.4 Wire shadow execution log

In `_execute_approved()`, when `execution_mode in ("manual", "test")`:
- Build the exact MetaApi payload that WOULD be sent.
- Insert row to `vanguard_shadow_execution_log`.
- Continue existing forward-tracking behavior.

When `execution_mode == "live"`:
- Still insert shadow log row (for later comparison with real fill).
- Also actually submit to MetaApi (existing behavior).

### 2.5 Config reload mechanism

Orchestrator must support live config reload without restart:
1. Add `self._config_mtime = os.path.getmtime(config_path)` at startup.
2. At cycle start, check if file mtime changed. If yes, reload + revalidate.
3. If new config fails validation: keep old config in memory, log error, send Telegram alert.
4. If reload succeeds: log new `config_version`, continue.
5. The POST `/api/v1/lifecycle/reload-config` endpoint just writes a flag file that triggers reload on next cycle check.

### 2.6 Static mount for future UI

Reserve `/config-admin` path on the FastAPI server for the future React UI. For now, serve a placeholder HTML:

```html
<!DOCTYPE html><html><body>
  <h1>Vanguard Config Admin (Phase 5 placeholder)</h1>
  <p>Backend API ready. UI ships in a later phase.</p>
  <p><a href="/api/v1/config/full">View current config JSON</a></p>
</body></html>
```

---

## 3. Acceptance tests

### Test 1 — Read endpoints return current config
```bash
curl http://localhost:8090/api/v1/config/full | jq .config_version
```
**Expect:** matches current file's config_version.

### Test 2 — PUT validates and rejects malformed config
```bash
curl -X PUT http://localhost:8090/api/v1/config/profiles \
  -H "Content-Type: application/json" \
  -d '[{"id": "missing_required_fields"}]'
```
**Expect:** 400 response, error lists missing `is_active`, `instrument_scope`, `policy_id`. Config file unchanged.

### Test 3 — PUT writes atomically and bumps version
```bash
OLD=$(curl -s http://localhost:8090/api/v1/config/full | jq -r .config_version)
curl -X PUT http://localhost:8090/api/v1/config/overrides \
  -H "Content-Type: application/json" \
  -d '{"gft_10k": {"side": "SHORT_ONLY", "expires_at_utc": "2099-12-31T23:59:59Z", "reason": "test"}}'
NEW=$(curl -s http://localhost:8090/api/v1/config/full | jq -r .config_version)
echo "Old: $OLD New: $NEW"
```
**Expect:** NEW > OLD, audit_log has new row with `section_changed=temporary_overrides`.

### Test 4 — Config reload picks up new version within 1 cycle
After Test 3 succeeds, wait one cycle. Check orchestrator logs.
**Expect:** log line `[cycle_ts=... config_version={NEW} profiles=...]`.

### Test 5 — Bad config on reload keeps old config in memory
Manually corrupt `vanguard_runtime.json` to invalid JSON. Wait one cycle.
**Expect:** Telegram alert fires, orchestrator continues with previous valid config, log shows `config_reload_failed: ...`. Restore file.

### Test 6 — Shadow execution log populated
Run orchestrator in `execution_mode=manual`. Wait for a cycle where V6 approves a candidate.
**Expect:** row in `vanguard_shadow_execution_log` with full MetaApi payload JSON.

### Test 7 — Runtime state endpoints return live data
```bash
curl http://localhost:8090/api/v1/runtime/account-state | jq
curl http://localhost:8090/api/v1/runtime/open-positions | jq
curl http://localhost:8090/api/v1/runtime/trade-journal?limit=5 | jq
```
**Expect:** all return 200 with populated data (from Phase 3 daemon).

### Test 8 — Manual pause endpoint works
```bash
curl -X POST http://localhost:8090/api/v1/lifecycle/pause-profile \
  -H "Content-Type: application/json" \
  -d '{"profile_id":"gft_10k","until_utc":"2099-12-31T23:59:59Z","reason":"test"}'
```
Wait one cycle. Inject a candidate for gft_10k.
**Expect:** V6 blocks with BLOCKED_DRAWDOWN_PAUSE reason=manual_pause.
Then POST `/unpause-profile` and verify unblocked.

### Test 9 — Audit log shows all changes
```bash
curl http://localhost:8090/api/v1/config/audit-log?limit=10 | jq '.[] | {updated_at_utc, section_changed, new_config_version}'
```
**Expect:** chronological list of config changes from Tests 3 and 8.

---

## 4. Report-back criteria

CC must produce:
1. New files + line counts
2. curl output for each test
3. One example of a rejected malformed PUT showing the 400 error body
4. Sample shadow_execution_log row as JSON
5. Confirmation: port 8090 serves all endpoints listed in §2.3

---

## 5. Non-goals (this phase)

- React UI (separate later phase)
- Authentication beyond placeholder header
- Rate limiting on endpoints
- WebSocket live updates (polling is fine)

---

## 6. Stop-the-line triggers

- A PUT with bad JSON corrupts the config file (atomic write broken)
- Config reload mid-cycle causes a NaN/crash
- Shadow log misses an approved candidate
- Any endpoint returns 500 for well-formed input
