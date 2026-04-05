# SignalStack Unified Frontend Spec — For Cursor
# Date: Mar 31, 2026
# React App: ~/SS/Meridian/ui/signalstack-app/

---

## What You're Building

Extend the existing Meridian React app to support 3 data sources (Meridian, S1, Vanguard) with dynamic Userviews, a Trade Desk for manual trade selection/execution, a Trade Log, and a Reports configuration page.

**API Base:** `http://localhost:8090` (unified API, separate from Meridian's 8080)
**Set in:** `.env.local` as `NEXT_PUBLIC_UNIFIED_API_URL=http://localhost:8090`

**Design rule:** Keep the existing Meridian shell (AppShell, sidebar, top bar, status banner) completely unchanged. Only modify page content components.

---

## 1. Navigation Update

Add one new nav item to `app-shell.tsx` sidebar:

| Current | After |
|---|---|
| Dash | Dash (unchanged) |
| Cands | Cands (extended) |
| Trades | Trades (rewritten: Trade Desk + Trade Log) |
| Model | Model (minor updates later) |
| **—** | **Reports** (NEW: 📊 icon) |
| Set | Set (unchanged) |

---

## 2. `/candidates` — Unified Candidate Workspace

### 2.1 Replace the current simple LONG/SHORT toggle header with:

```
┌──────────────────────────────────────────────────────────────┐
│ SOURCE: [Meridian] [S1] [Vanguard] [Combined]  segmented ctrl│
│ VIEW: [Default ▼] [+ New] [Save] [⚙ Manage]    dropdown+btns│
│ DIR: [LONG] [SHORT] [All]                       segmented ctrl│
│ [Filter] [Sort] [Group] [Columns] [Display]  compact popovers│
│ 60 candidates · Updated 2026-03-29 17:15 ET          status  │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Source Selector
Segmented control: Meridian | S1 | Vanguard | Combined
When source changes:
1. Fetch `GET /api/v1/field-registry` (cache this — rarely changes)
2. Filter available fields to those matching the selected source
3. Update visible columns to the default set for that source
4. Fetch `GET /api/v1/candidates?source={source}&direction={dir}`

### 2.3 Userviews
**State:** `selectedViewId`, `isUnsaved` (true if current config differs from saved view)

**UI Components:**
- **View dropdown:** Shows saved view names. Selecting one restores: source, direction, filters, sorts, visible_columns, column_order, display_mode
- **"+ New" button:** Opens modal → name input → saves current config as new view
- **"Save" button:** Overwrites current view with current config (disabled if no view selected or if system view)
- **"⚙ Manage" button:** Opens side sheet with all views, rename/duplicate/delete actions
- **View name badge:** Always visible showing current view name. If unsaved changes, show "(modified)" suffix

**API calls:**
- `GET /api/v1/userviews` — on mount, list all views
- `POST /api/v1/userviews` — create
- `PUT /api/v1/userviews/:id` — update
- `DELETE /api/v1/userviews/:id` — delete (confirmation dialog)

**localStorage:** Persist `lastViewId` and current working state so refresh doesn't lose context.

### 2.4 Dynamic Columns
Driven by field registry. The "Columns" popover shows:
- Checkboxes for each field available for the current source
- Drag-to-reorder (or up/down buttons)
- "Reset to Default" button

**In Combined mode:** Show common columns by default. Source-native columns can be shown but will have empty cells for non-matching sources. Show a subtle "—" for missing values, not blank space.

### 2.5 Filter Builder
"Filter" popover opens a builder:
- Each row: [Field dropdown] [Operator dropdown] [Value input]
- Field dropdown: only shows fields where `filterable=true` for current source
- Operators: depends on field type (string: contains/equals, number: >/</≥/≤/=, enum: is/is not)
- "+ Add Filter" button
- Remove filter (X button per row)

### 2.6 Sort Builder
"Sort" popover:
- Each row: [Field dropdown] [ASC/DESC toggle]
- Only fields where `sortable=true`
- Multi-sort: drag to reorder priority
- "+ Add Sort" button

### 2.7 Table Rendering
Reuse current dense table aesthetic from `candidates-client.tsx`.

**Each row shows:**
- Source badge: colored chip (M=blue, S1=amber, V=green)
- Tier badge: small label (Dual, NN, Scorer, M-Long, etc.)
- All visible columns from the current view
- Score values with appropriate formatting (0.860 for scores, +9.47% for returns, $60.71 for prices)

**Row click:** Opens detail drawer (existing `CandidateDetailPanel` pattern)
**Row action button:** "Trade" button → adds this pick to the Trade Desk

### 2.8 Card/List Mode (Toggle)
"Display" control toggles between Table (default) and Card mode.
Card mode: each candidate as a compact card showing symbol, direction, source badge, key scores. Inspired by S1 smart view rows from mobile_v2.html.

### 2.9 Detail Drawer
Extend existing `CandidateDetailPanel`:
- Top: symbol, source badge, direction, price, freshness
- Middle: **All native fields for that source** (dynamic from field registry)
- Bottom: TradingView chart (existing), ticker info card (existing)
- Action button: "Add to Trade Desk"

---

## 3. `/trades` — Trade Desk + Trade Log

Replace the current empty `trades-client.tsx` with a tabbed page:

### 3.1 Tab Bar
```
[Trade Desk] [Trade Log]
```

### 3.2 Trade Desk Tab

**Fetch:** `GET /api/v1/picks/today` on mount

**Layout:**
```
┌──────────────────────────────────────────────────────────────┐
│ TRADE DESK            Mon Mar 31, 2026  9:25 ET              │
│ 37 picks · 3 selected · $1,500 risk deployed                 │
├──────────────────────────────────────────────────────────────┤
│ [☐ Select All]  [Execute Selected (3)] [Risk/Trade: $500 ▼]  │
├──────────────────────────────────────────────────────────────┤
│ ▼ Dual Filter (S1) — 2 picks                                │
│   ☐ SHO   LONG  $12.50  RF:0.614  NN:0.894  Scorer:0.573   │
│   ☐ TRIN  LONG  $3.20   RF:0.620  NN:0.840  Scorer:0.595   │
├──────────────────────────────────────────────────────────────┤
│ ▼ NN Top (S1) — 7 picks                                     │
│   ☑ BOXL  LONG  $2.15   NN:0.993  Conv:0.566                │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ▼ Meridian Longs — 10 picks                                  │
│   ☑ TRNO  LONG  $60.71  TCN:0.860  FR:0.902                 │
│   ☑ ALL   LONG  $282.66 TCN:0.765  FR:0.963                 │
│   ...                                                        │
├──────────────────────────────────────────────────────────────┤
│ ... more tier sections ...                                    │
└──────────────────────────────────────────────────────────────┘
```

**Components:**
- `TierSection`: collapsible section per tier, shows tier name, source badge, pick count
- `PickRow`: checkbox, symbol, direction, price, key scores (different per tier), shares input
- `ExecuteBar`: sticky top bar with select all, execute button, risk per trade input
- `ConfirmModal`: "Execute 3 trades on TTP Demo #DEMOSW1794?" with trade list

**State:**
```typescript
const [tiers, setTiers] = useState<Tier[]>([]);
const [selected, setSelected] = useState<Set<string>>(new Set()); // "tier:symbol" keys
const [riskPerTrade, setRiskPerTrade] = useState(500);
const [executing, setExecuting] = useState(false);
const [showConfirm, setShowConfirm] = useState(false);
```

**Execute flow:**
1. User clicks "Execute Selected"
2. Confirmation modal opens showing selected trades
3. User confirms
4. `POST /api/v1/execute` with selected trades
5. Show toast with result
6. Refresh execution log

**Score display per tier:**
| Tier | Scores Shown |
|---|---|
| tier_dual | RF, NN, Scorer |
| tier_nn | NN, Scorer, Conv |
| tier_rf | RF, Conv |
| tier_scorer_long | Scorer |
| tier_s1_short | Scorer, Conv |
| tier_meridian_long | TCN, Factor Rank |
| tier_meridian_short | TCN, Factor Rank |

**Tag selector per pick (shown when checkbox is checked):**
Multi-select chips: TCN | Alpha | RF | NN | Convergence | Factor Rank | LGBM | Manual
Plus a free-text notes field (optional, one-liner).
Tags are submitted with the execute request and stored in `execution_log.tags` + `execution_log.notes`.
This is how the trader records WHY they took each trade — for signal attribution after 3-5 days.

**Color:** Green background tint for LONG rows, red tint for SHORT rows.

### 3.3 Trade Log Tab

**Fetch:** `GET /api/v1/execution/log` on mount

**Table columns:** Time, Symbol, Direction, Tier, Shares, Entry, Tags, Status, Outcome, P&L

**Features:**
- **Tier badges:** Color-coded chips (same colors as source badges)
- **Tag chips:** Small colored chips showing signal attribution (TCN, Alpha, etc.)
- **Status:** SUBMITTED (yellow), FILLED (green), REJECTED (red), CANCELLED (gray)
- **Outcome column:** WIN (green), LOSE (red), OPEN (amber), TIMEOUT (gray) — updated after 3-5 days
- **Inline edit:** Click a row to update exit_price, outcome, notes (calls `PUT /api/v1/execution/:id`)
- **Date filter:** Today (default), This Week, All
- **Tag filter:** Filter by tag to see which signals are working
- **Analytics strip at top:** Win rate by tag, win rate by tier (from `GET /api/v1/execution/analytics`)

---

## 4. `/reports` — Report Configuration (NEW PAGE)

### 4.1 New Route
Add `app/reports/page.tsx` with `ReportsClient` component.

### 4.2 Report List View
**Fetch:** `GET /api/v1/reports` on mount

Shows cards for each configured report:
```
┌────────────────────────────────────────┐
│ 📋 Morning Report          Enabled ✓   │
│ 8 blocks · 6:30 AM ET                  │
│ [Edit] [Test Now] [Disable]            │
└────────────────────────────────────────┘
┌────────────────────────────────────────┐
│ 📋 Evening Report          Enabled ✓   │
│ 10 blocks · 5:00 PM ET                 │
│ [Edit] [Test Now] [Disable]            │
└────────────────────────────────────────┘
[+ Create New Report]
```

**"Test Now"** calls `POST /api/v1/reports/:id/test` → shows toast "Report sent to Telegram ✓"

### 4.3 Report Editor (Modal or Full Page)
Opens when clicking "Edit" or "+ Create":

**Header:** Report name (editable), schedule selector (6:30 AM / 5:00 PM / custom cron), enabled toggle

**Block list:** Draggable list of message blocks. Each block has:
- Type selector: Tier Block | Overlap Block | Summary Block | AI Brief | Forward Track | Text
- Config fields (varies by type):
  - **Tier Block:** Source dropdown, tier dropdown, max picks (number), sort by (field dropdown from registry), format (compact/detailed), threshold filters
  - **Overlap Block:** Multi-select sources to cross-reference
  - **Summary Block:** Checkboxes for stats to include
  - **AI Brief Block:** Model selector (haiku/sonnet), prompt textarea
  - **Forward Track Block:** Lookback days (number), metric checkboxes
  - **Text Block:** Textarea for static content

- "+ Add Block" button at bottom
- Drag to reorder blocks
- Delete block (X button)

**Save:** `POST /api/v1/reports` (new) or `PUT /api/v1/reports/:id` (update)

---

## 5. API Client Updates (`lib/api.ts`)

Add unified API client alongside existing Meridian API:

```typescript
const UNIFIED_API = process.env.NEXT_PUBLIC_UNIFIED_API_URL ?? 'http://localhost:8090';

// Candidates
export async function getUnifiedCandidates(params: CandidateQuery): Promise<CandidateResponse> { ... }
export async function getCandidateDetail(rowId: string): Promise<CandidateDetail> { ... }
export async function getFieldRegistry(): Promise<FieldDef[]> { ... }

// Userviews
export async function getUserviews(): Promise<Userview[]> { ... }
export async function createUserview(view: UserviewCreate): Promise<Userview> { ... }
export async function updateUserview(id: string, view: UserviewUpdate): Promise<Userview> { ... }
export async function deleteUserview(id: string): Promise<void> { ... }

// Trade Desk
export async function getTodayPicks(): Promise<TierResponse> { ... }
export async function executeTrades(trades: TradeOrder[]): Promise<ExecuteResult> { ... }
export async function getExecutionLog(date?: string): Promise<ExecutionEntry[]> { ... }

// Reports
export async function getReports(): Promise<ReportConfig[]> { ... }
export async function createReport(config: ReportConfigCreate): Promise<ReportConfig> { ... }
export async function updateReport(id: string, config: ReportConfigUpdate): Promise<ReportConfig> { ... }
export async function deleteReport(id: string): Promise<void> { ... }
export async function testReport(id: string): Promise<void> { ... }
```

---

## 6. Types (`lib/types.ts`)

```typescript
interface CandidateRow {
  row_id: string;
  source: 'meridian' | 's1' | 'vanguard';
  symbol: string;
  side: 'LONG' | 'SHORT';
  price: number;
  as_of: string;
  tier: string;
  sector?: string;
  regime?: string;
  native: Record<string, number | string | null>;
}

interface FieldDef {
  key: string;
  label: string;
  type: 'string' | 'number' | 'enum' | 'datetime' | 'boolean';
  sources: string[];
  filterable: boolean;
  sortable: boolean;
  groupable: boolean;
  format: string;
  description?: string;
  options?: string[];
}

interface Userview {
  id: string;
  name: string;
  source: string;
  direction: string | null;
  filters: FilterDef[];
  sorts: SortDef[];
  visible_columns: string[];
  column_order: string[];
  display_mode: 'table' | 'card';
  is_system: boolean;
}

interface TierResponse {
  date: string;
  tiers: TierGroup[];
  total_picks: number;
}

interface TierGroup {
  tier: string;
  label: string;
  source: string;
  picks: Pick[];
}

interface Pick {
  symbol: string;
  direction: string;
  shares: number;
  price: number;
  scores: Record<string, number>;
}

interface ExecutionEntry {
  id: number;
  executed_at: string;
  symbol: string;
  direction: string;
  tier: string;
  shares: number;
  entry_price: number | null;
  status: string;
  account: string;
}

interface ReportConfig {
  id: string;
  name: string;
  enabled: boolean;
  schedule: string;
  blocks: ReportBlock[];
}

type ReportBlock =
  | { type: 'tier_block'; source: string; tier: string; max_picks: number; sort_by: string; format: string }
  | { type: 'overlap_block'; sources: string[]; label: string }
  | { type: 'summary_block'; stats: string[] }
  | { type: 'ai_brief_block'; model: string; prompt: string }
  | { type: 'forward_track_block'; lookback_days: number; metrics: string[] }
  | { type: 'text_block'; content: string };
```

---

## 7. Visual Language

- **Keep Meridian's dark theme** (navy #0b1326, amber accents)
- **Source badges:** Meridian = blue chip, S1 = amber chip, Vanguard = green chip
- **Tier badges:** Smaller, muted color chips matching source
- **LONG rows:** Subtle green left border or tint
- **SHORT rows:** Subtle red left border or tint
- **Score formatting:** 0.860 (3 decimal for probabilities), +9.47% (percent with sign), $60.71 (currency)
- **Empty/null values:** Show "—" in muted color, never blank space
- **Popovers for controls:** Filter, Sort, Group, Columns — use popover/dropdown pattern, not full modals
- **Tables:** Same dense monospace aesthetic as current candidates table

---

## 8. Implementation Order

1. **Trade Desk** (Phase 1 — Monday priority)
   - Rewrite `trades-client.tsx` with Trade Desk + Trade Log tabs
   - Wire to `GET /api/v1/picks/today` and `POST /api/v1/execute`
   
2. **Candidates Source Selector + Basic Userviews** (Phase 2)
   - Add source selector and view dropdown to candidates header
   - Wire to unified API candidates endpoint
   - Dynamic columns from field registry

3. **Full Userview CRUD + Filter/Sort Builders** (Phase 3)
   - Create/Save/Delete views
   - Filter builder popover
   - Sort builder popover
   - Column chooser popover

4. **Reports Page** (Phase 4)
   - New route + ReportsClient component
   - Report list + editor

5. **Card/List Display Mode** (Phase 5)
   - Toggle between table and card views in candidates
