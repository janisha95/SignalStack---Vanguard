# SignalStack Unified Spec — Candidates, Trade Desk, Trade Log, Reports
# Date: Mar 31, 2026
# Authors: Shan + Claude Opus 4.6
# Audience: Cursor (frontend), Claude Code (backend), Codex (Meridian fixes)

---

## 0. Design Principles

1. **Keep the Meridian React shell.** No new shell, no new nav system.
2. **Userviews are the core product value.** Dynamic columns, filters, sorts per source.
3. **Source-aware, not source-merged.** Meridian, S1, and Vanguard each have their own column sets. Never fake a universal score.
4. **Trade Desk = YOU pick trades.** The executor does NOT auto-blast. You select, review, confirm.
5. **Reports are configurable, not hardcoded.** Morning/evening Telegram reports are defined in UI, not in Python code.
6. **Investigate before building.** Don't break prod.

---

## 1. Navigation (Unchanged Shell + 1 New Page)

| Route | Current | After |
|---|---|---|
| `/` | Dashboard | Dashboard (add source summary cards) |
| `/candidates` | Meridian-only table | **Unified workspace: Source selector + Userviews** |
| `/trades` | Empty "No trades yet" | **Trade Desk + Trade Log** |
| `/model` | Model health (MOCK) | Model health (update for TCN v2 + LGBM) |
| `/reports` | ❌ Does not exist | **NEW: Report Configuration** |
| `/settings` | Read-only settings | Settings (unchanged for now) |

Sidebar nav adds one item: "Reports" (📊 icon) between Model and Settings.

---

## 2. `/candidates` — Unified Candidate Workspace

### 2.1 Header Control Bar

```
┌──────────────────────────────────────────────────────────────┐
│ SOURCE: [Meridian] [S1] [Vanguard] [Combined]                │
│ VIEW:   [Default ▼] [+ New] [Save] [⚙ Manage]               │
│ DIR:    [LONG] [SHORT] [All]                                  │
│ CONTROLS: [Filter] [Sort] [Group] [Columns] [Display]        │
│ STATUS: 60 candidates · Updated 2026-03-29 17:15 ET          │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Source Selector

Segmented control: Meridian | S1 | Vanguard | Combined

Each source loads its own column set from the **field registry** (see §8).

### 2.3 Userviews

A Userview is a saved configuration of: source, direction, filters, sorts, visible columns, column order, grouping, display mode.

**CRUD:**
- **Create:** Click "+ New" → name the view → saves current config
- **Save:** Overwrites current view with current config
- **Duplicate:** Clone a view and rename
- **Delete:** With confirmation
- **Load:** Select from dropdown → restores full config
- **Local persistence:** Current working state survives refresh (localStorage)

**Preset views (system defaults, non-deletable):**
| View Name | Source | Filters | Sort | Inspired By |
|---|---|---|---|---|
| Convergence Picks | S1 | convergence_score > 0 | convergence_score DESC | S1 smart view |
| ML Confidence | S1 | nn_p_tp > 0.90 | nn_p_tp DESC | S1 smart view |
| Dual Filter | S1 | p_tp ≥ 0.50 AND nn_p_tp ≥ 0.50 | scorer_prob DESC | S1 smart view |
| NN Top | S1 | nn_p_tp > 0.90 | nn_p_tp DESC | S1 smart view |
| Scorer Top | S1 | scorer_prob > 0.55 | scorer_prob DESC | S1 smart view |
| Short Opportunities | S1 | direction = SHORT | scorer_prob DESC | S1 smart view |
| Meridian Longs | Meridian | direction = LONG | final_score DESC | Current default |
| Meridian Shorts | Meridian | direction = SHORT | final_score DESC | Current default |
| Vanguard Longs | Vanguard | direction = LONG | lgbm_long_prob DESC | New |
| Vanguard Shorts | Vanguard | direction = SHORT | lgbm_short_prob DESC | New |
| All Systems Combined | Combined | none | as_of DESC | New |

### 2.4 Dynamic Columns

Columns are **source-aware** and driven by the field registry (§8). The user can show/hide and reorder any column available for the current source.

**When source = "Combined":**
- Common columns always show (symbol, source, side, price, as_of)
- Source-native columns show but are sparse (empty cells for non-matching sources)
- Never show a fake "unified score" column

### 2.5 Table Rendering

Dense table (default) — same aesthetic as current Meridian candidates table.
Card/list mode (toggle) — compact cards inspired by S1 smart view rows.

Each row shows:
- Source badge (M / S1 / V)
- Tier badge if applicable (Dual, NN, Scorer, Meridian Long, etc.)
- Key ML scores for that source
- "Trade" button (sends to Trade Desk)

### 2.6 Detail Drawer

Clicking a row opens the right-side detail panel (existing Meridian pattern):
- Top: symbol, source badge, direction, price, freshness
- Middle: all native scores for that source
- Bottom: TradingView chart, ticker info card
- Action: "Add to Trade Desk" button

---

## 3. `/trades` — Trade Desk + Trade Log

This page has TWO sections, toggled by a tab bar at the top:

### 3.1 Trade Desk Tab

The Trade Desk is where you SELECT which trades to execute.

```
┌──────────────────────────────────────────────────────────────┐
│ TRADE DESK                    Mon Mar 31, 2026  9:25 ET      │
│ 37 picks available · 3 selected · 0 executed today           │
├──────────────────────────────────────────────────────────────┤
│ [☐ Select All]  [Execute Selected (3)]  [Risk: $500/trade]   │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: Dual (S1) — 2 picks                                 │
│   ☐ SHO   LONG  $XX  RF:0.614  NN:0.894  Scorer:0.573      │
│   ☐ TRIN  LONG  $XX  RF:0.620  NN:0.840  Scorer:0.595      │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: NN (S1) — 7 picks                                   │
│   ☑ BOXL  LONG  $XX  NN:0.993  Scorer:0.402  Conv:0.566    │
│   ☐ BXP   LONG  $XX  NN:0.982  Scorer:0.610                │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: Meridian Longs — 10 picks                            │
│   ☑ TRNO  LONG  $60.71  TCN:0.860  FR:0.902                │
│   ☑ ALL   LONG  $282.66  TCN:0.765  FR:0.963               │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: Meridian Shorts — 5 picks                            │
│   ☐ PAYO  SHORT  $XX  TCN:0.221  FR:0.988                  │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: S1 Shorts — 3 picks                                 │
│   ☐ CCNR  SHORT  $XX  Scorer:0.626  Conv:0.166             │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Tier: Scorer Long (S1) — 10 picks                         │
│   ☐ CRH   LONG  $XX  Scorer:0.689                          │
│   ...                                                        │
└──────────────────────────────────────────────────────────────┘
```

**Features:**
- Picks come from the executor's 7-tier logic (same as dry-run output)
- Checkbox per pick — YOU select which to execute
- Shares per trade editable (default from risk sizing: $500 risk / ATR)
- "Execute Selected" button — sends selected to SignalStack webhook
- Tier sections collapsible
- Color: green for LONG, red for SHORT
- Risk sizing panel (reuse existing `calcFallbackSize` from dashboard-client.tsx)
- Confirmation modal before execution: "Execute 3 trades on TTP Demo #DEMOSW1794?"

**Data source:** `GET /api/v1/picks/today` — returns all 7 tiers with picks
**Execute action:** `POST /api/v1/execute` — sends selected trades to SignalStack

### 3.2 Trade Log Tab

Shows execution history with tier tracking.

```
┌──────────────────────────────────────────────────────────────┐
│ TRADE LOG                          Filter: [Today ▼] [All]   │
├──────┬──────┬─────┬────────┬───────┬────────┬───────┬────────┤
│ Time │ Tick │ Dir │ Tier   │Shares │ Entry  │ Status│ P&L    │
├──────┼──────┼─────┼────────┼───────┼────────┼───────┼────────┤
│ 9:31 │ TRNO │ L   │ M-Long │  100  │ $60.71 │ FILLED│   —    │
│ 9:31 │ ALL  │ L   │ M-Long │   35  │$282.66 │ FILLED│   —    │
│ 9:32 │ BOXL │ L   │ S1-NN  │  500  │ $2.15  │ FILLED│   —    │
└──────┴──────┴─────┴────────┴───────┴────────┴───────┴────────┘
```

Tier abbreviations: M-Long, M-Short, S1-Dual, S1-NN, S1-RF, S1-Scorer, S1-Short, V-Long, V-Short

**Data source:** `GET /api/v1/execution/log` — returns `vanguard_execution_log` table

---

## 4. `/reports` — Report Configuration (NEW PAGE)

Currently, morning and evening Telegram reports are hardcoded in:
- `~/SS/Meridian/s1_morning_report_v2.py` (8 messages, 6 tiers)
- `~/SS/Meridian/s1_evening_report_v2.py` (10 messages, 25% looser thresholds)

This page makes reports configurable WITHOUT code changes.

### 4.1 Report List

```
┌──────────────────────────────────────────────────────────────┐
│ REPORT CONFIGURATION                                         │
├──────────────────────────────────────────────────────────────┤
│ ┌────────────────────────────────┐                           │
│ │ 📋 Morning Report              │ Enabled ✓  6:30 AM ET    │
│ │ 8 messages · 6 tiers · Haiku   │ [Edit] [Test] [Disable]  │
│ └────────────────────────────────┘                           │
│ ┌────────────────────────────────┐                           │
│ │ 📋 Evening Report              │ Enabled ✓  5:00 PM ET    │
│ │ 10 messages · 25% loose thresh │ [Edit] [Test] [Disable]  │
│ └────────────────────────────────┘                           │
│ [+ Create New Report]                                        │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 Report Editor

Each report is defined as a list of **message blocks**:

| Block Type | Config | Example |
|---|---|---|
| Tier Block | source, tier, threshold, max_picks, sort_by | "S1 Dual: RF≥0.60 AND NN≥0.75, top 5, sort by scorer_prob" |
| Overlap Block | sources to cross-reference | "Tickers appearing in both Meridian Long and S1 Scorer" |
| Summary Block | stats to include | "Total picks, long/short split, top sectors" |
| AI Brief Block | model (haiku/sonnet), prompt template | "Generate 2-sentence trading recommendation" |
| Forward Track Block | lookback days, metrics | "Last 5 days: WR, avg P&L per tier" |
| Custom Text Block | static text | "--- Good morning, here are today's picks ---" |

**Config per block:**
- Thresholds: any field from field registry (§8) with operator (>, <, ≥, ≤, =)
- Sort: any sortable field
- Max picks: limit per block
- Format: compact (one line per pick) or detailed (multi-line with scores)

**Report schedule:** cron expression or preset (daily 6:30 AM, daily 5:00 PM, etc.)
**Delivery:** Telegram (bot token + chat ID from env), or future: email, Slack

### 4.3 Test Button

"Test" sends the report NOW to your Telegram, using latest data. No waiting for schedule.

**Data source:**
- `GET /api/v1/reports` — list configured reports
- `GET /api/v1/reports/:id` — get report config
- `POST /api/v1/reports` — create report
- `PUT /api/v1/reports/:id` — update report
- `DELETE /api/v1/reports/:id` — delete report
- `POST /api/v1/reports/:id/test` — send test report now
- Reports stored in `vanguard_universe.db` table `report_configs`

---

## 5. Dashboard Updates (Minimal)

Add to existing dashboard:
- Source summary strip: "Meridian: 60 candidates | S1: 22 picks | Vanguard: 8 picks"
- Today's execution summary: "3 trades executed · $1,500 deployed · 0 filled"
- Quick link to Trade Desk

Do NOT redesign the dashboard. These are additive cards only.

---

## 6. Model Page Updates

- Update model health to show TCN v2 stats (IC: +0.1046, not MOCK)
- Add LGBM Long/Short model health cards
- Add Vanguard model section when available
- Show per-model forward tracking if data exists

---

## 7. Backend API (Claude Code)

### 7.1 Unified API Server

File: `~/SS/Vanguard/vanguard/api/unified_api.py`
Framework: FastAPI
Port: 8090 (does NOT conflict with Meridian 8080 or S1 8000)

### 7.2 Endpoints

**Candidates:**
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/candidates` | GET | Normalized candidates for source/filters/sort/pagination |
| `/api/v1/candidates/:row_id` | GET | Full detail for one candidate |
| `/api/v1/field-registry` | GET | Field metadata for filter/sort/column builders |

**Userviews:**
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/userviews` | GET | List saved views |
| `/api/v1/userviews` | POST | Create view |
| `/api/v1/userviews/:id` | PUT | Update view |
| `/api/v1/userviews/:id` | DELETE | Delete view |

**Trade Desk:**
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/picks/today` | GET | All 7 tiers with today's picks |
| `/api/v1/execute` | POST | Execute selected trades via SignalStack |
| `/api/v1/execution/log` | GET | Today's execution log |
| `/api/v1/sizing/:symbol/:direction` | GET | Position sizing for a symbol |

**Reports:**
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/reports` | GET | List configured reports |
| `/api/v1/reports` | POST | Create report config |
| `/api/v1/reports/:id` | PUT | Update report config |
| `/api/v1/reports/:id` | DELETE | Delete report config |
| `/api/v1/reports/:id/test` | POST | Send test report now |

**Health:**
| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/health` | GET | System health + source availability |

### 7.3 Source Adapters (READ-ONLY on S1 and Meridian DBs)

| Adapter | Reads From | Returns |
|---|---|---|
| Meridian | `~/SS/Meridian/data/v2_universe.db` → `shortlist_daily` | Normalized rows with Meridian-native fields |
| S1 | `~/SS/Advance/data_cache/signalstack_results.db` → `scorer_predictions` + `~/SS/Advance/evening_results/convergence_*.json` | Normalized rows with S1-native fields |
| Vanguard | `~/SS/Vanguard/data/vanguard_universe.db` → `vanguard_training_data` (for now, later shortlist) | Normalized rows with Vanguard-native fields |

All writes go to `~/SS/Vanguard/data/vanguard_universe.db` only.

---

## 8. Field Registry — THE Source of Truth for Dynamic Columns

The field registry defines every column available per source, including type, sortability, filterability, and display format. The UI reads this at startup and builds all filter/sort/column pickers dynamically.

### 8.1 Common Fields (all sources)

| Field | Type | Filter | Sort | Group | Display |
|---|---|---|---|---|---|
| symbol | string | ✓ | ✓ | ✓ | ✓ |
| source | enum | ✓ | ✓ | ✓ | ✓ |
| side | enum | ✓ | ✓ | ✓ | ✓ |
| price | number | ✓ | ✓ | ✗ | ✓ |
| as_of | datetime | ✓ | ✓ | ✓ | ✓ |
| tier | string | ✓ | ✓ | ✓ | ✓ |
| sector | string | ✓ | ✗ | ✓ | ✓ |
| regime | string | ✓ | ✗ | ✓ | ✓ |

### 8.2 Meridian-Native Fields

| Field | Type | Filter | Sort | Display | Notes |
|---|---|---|---|---|---|
| tcn_score | number | ✓ | ✓ | ✓ | TCN v2 probability (0-1) |
| factor_rank | number | ✓ | ✓ | ✓ | Cross-sectional percentile |
| final_score | number | ✓ | ✓ | ✓ | 0.60×FR + 0.40×TCN |
| residual_alpha | number | ✓ | ✓ | ✓ | Alpha after beta strip |
| predicted_return | number | ✓ | ✓ | ✓ | From predictions_daily |
| beta | number | ✓ | ✓ | ✓ | Market beta |
| lgbm_long_prob | number | ✓ | ✓ | ✓ | LGBM long classifier (when wired) |
| lgbm_short_prob | number | ✓ | ✓ | ✓ | LGBM short classifier (when wired) |

### 8.3 S1-Native Fields

| Field | Type | Filter | Sort | Display | Notes |
|---|---|---|---|---|---|
| p_tp | number | ✓ | ✓ | ✓ | RF probability |
| nn_p_tp | number | ✓ | ✓ | ✓ | NN probability |
| scorer_prob | number | ✓ | ✓ | ✓ | LightGBM scorer |
| convergence_score | number | ✓ | ✓ | ✓ | Cross-strategy agreement |
| n_strategies_agree | number | ✓ | ✓ | ✓ | Strategy count |
| strategy | string | ✓ | ✗ | ✓ | rct/srs/edge/wyckoff/brf/mr |
| volume_ratio | number | ✓ | ✓ | ✓ | Relative volume |

### 8.4 Vanguard-Native Fields

| Field | Type | Filter | Sort | Display | Notes |
|---|---|---|---|---|---|
| lgbm_long_prob | number | ✓ | ✓ | ✓ | Vanguard LGBM long |
| lgbm_short_prob | number | ✓ | ✓ | ✓ | Vanguard LGBM short |
| tcn_long_score | number | ✓ | ✓ | ✓ | Vanguard TCN long (when trained) |
| tcn_short_score | number | ✓ | ✓ | ✓ | Vanguard TCN short (when trained) |
| spread_proxy | number | ✓ | ✓ | ✓ | Top LGBM feature |
| session_phase | number | ✓ | ✓ | ✓ | Time-of-day factor |
| gap_pct | number | ✓ | ✓ | ✓ | Opening gap |

### 8.5 Registry API Response Shape

```json
{
  "fields": [
    {
      "key": "tcn_score",
      "label": "TCN Score",
      "type": "number",
      "sources": ["meridian"],
      "filterable": true,
      "sortable": true,
      "groupable": false,
      "displayable": true,
      "format": "score_0_1",
      "comparable_across_sources": false,
      "description": "TCN v2 classifier probability"
    }
  ]
}
```

---

## 9. DB Schema Additions

All new tables in `~/SS/Vanguard/data/vanguard_universe.db`:

```sql
-- Userviews
CREATE TABLE userviews (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,          -- meridian|s1|vanguard|combined
    direction TEXT,                -- LONG|SHORT|null (all)
    filters TEXT,                  -- JSON array of filter objects
    sorts TEXT,                    -- JSON array of sort objects
    grouping TEXT,                 -- JSON grouping config
    visible_columns TEXT,          -- JSON array of field keys
    column_order TEXT,             -- JSON array of field keys in display order
    display_mode TEXT DEFAULT 'table',  -- table|card
    is_system BOOLEAN DEFAULT 0,  -- system presets can't be deleted
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Execution log
CREATE TABLE execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    tier TEXT NOT NULL,
    shares INTEGER NOT NULL,
    entry_price REAL,
    status TEXT DEFAULT 'SUBMITTED',  -- SUBMITTED|FILLED|REJECTED|CANCELLED
    signalstack_response TEXT,        -- JSON response from webhook
    account TEXT,                      -- ttp_demo_1m, etc.
    source_scores TEXT,               -- JSON: {tcn_score: 0.86, factor_rank: 0.90, ...}
    created_at TEXT DEFAULT (datetime('now'))
);

-- Report configs
CREATE TABLE report_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN DEFAULT 1,
    schedule TEXT NOT NULL,            -- cron expression or preset
    delivery_channel TEXT DEFAULT 'telegram',
    blocks TEXT NOT NULL,              -- JSON array of block configs
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

---

## 10. Implementation Sequence

### Phase 1: Trade Desk (MONDAY PRIORITY)
**Backend (Claude Code):** Build `/api/v1/picks/today` and `/api/v1/execute` endpoints.
**Frontend (Cursor):** Build Trade Desk tab on `/trades` page with tier sections and execute flow.
**Goal:** Manual trade selection and execution by Monday morning.

### Phase 2: Candidates + Userviews
**Backend (Claude Code):** Build source adapters, field registry, candidate endpoints, Userview CRUD.
**Frontend (Cursor):** Build source selector, Userview controls, dynamic column rendering.
**Goal:** Full candidate browsing across all 3 sources with saved views.

### Phase 3: Trade Log
**Backend (Claude Code):** Build `/api/v1/execution/log` endpoint.
**Frontend (Cursor):** Build Trade Log tab on `/trades` page.
**Goal:** Execution history with tier tracking.

### Phase 4: Reports
**Backend (Claude Code):** Build report config CRUD + report generation engine + Telegram sender.
**Frontend (Cursor):** Build `/reports` page with report editor.
**Goal:** Configure morning/evening reports without code changes.

### Phase 5: Dashboard + Model Updates
**Frontend (Cursor):** Add source summary cards to dashboard, update model page.
**Goal:** Polish.

---

## 11. Files Affected

### NEW files (Claude Code builds):
- `~/SS/Vanguard/vanguard/api/unified_api.py` — FastAPI server, port 8090
- `~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py` — reads shortlist_daily
- `~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py` — reads scorer_predictions + convergence
- `~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py` — reads vanguard data
- `~/SS/Vanguard/vanguard/api/field_registry.py` — field metadata
- `~/SS/Vanguard/vanguard/api/userviews.py` — Userview CRUD
- `~/SS/Vanguard/vanguard/api/trade_desk.py` — picks + execution logic
- `~/SS/Vanguard/vanguard/api/reports.py` — report config + generation

### MODIFIED files (Cursor builds):
- `~/SS/Meridian/ui/signalstack-app/app/trades/page.tsx` — Trade Desk + Log
- `~/SS/Meridian/ui/signalstack-app/app/candidates/page.tsx` — Source selector + Userviews
- `~/SS/Meridian/ui/signalstack-app/app/reports/page.tsx` — NEW page
- `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx` — rewrite
- `~/SS/Meridian/ui/signalstack-app/components/candidates-client.tsx` — extend
- `~/SS/Meridian/ui/signalstack-app/lib/api.ts` — add unified API endpoints
- `~/SS/Meridian/ui/signalstack-app/components/app-shell.tsx` — add Reports nav item

### UNCHANGED:
- All Meridian pipeline code (stages/*.py)
- All S1 code (agent_server.py)
- All Vanguard pipeline code (stages/*.py)
- Shell layout, sidebar structure, top bar
