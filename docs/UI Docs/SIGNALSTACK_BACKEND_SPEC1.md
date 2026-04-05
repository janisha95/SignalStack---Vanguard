# SignalStack Unified Backend Spec — For Claude Code
# Date: Mar 31, 2026
# Read AGENTS.md and ROADMAP.md first

---

## What You're Building

A FastAPI server at port 8090 that serves unified candidate data from 3 systems (Meridian, S1, Vanguard) to a React frontend, handles trade execution via SignalStack webhook, and manages Userviews + Report configs.

**All new code goes in:** `~/SS/Vanguard/vanguard/api/`
**All new DB tables go in:** `~/SS/Vanguard/data/vanguard_universe.db`
**READ-ONLY access to:** Meridian DB (`~/SS/Meridian/data/v2_universe.db`) and S1 DB (`~/SS/Advance/data_cache/signalstack_results.db`)

---

## 1. File Structure

```
~/SS/Vanguard/vanguard/api/
├── unified_api.py            # FastAPI app, port 8090
├── adapters/
│   ├── meridian_adapter.py   # Reads shortlist_daily from Meridian DB
│   ├── s1_adapter.py         # Reads scorer_predictions + convergence JSON from S1
│   └── vanguard_adapter.py   # Reads Vanguard training data (placeholder for now)
├── field_registry.py         # Field metadata — drives dynamic columns
├── userviews.py              # Userview CRUD
├── trade_desk.py             # Picks by tier + execution via SignalStack
└── reports.py                # Report config CRUD + generation + Telegram sender
```

---

## 2. Source Adapters

### 2.1 Meridian Adapter (`meridian_adapter.py`)

**Reads from:** `~/SS/Meridian/data/v2_universe.db`
**Table:** `shortlist_daily` (60 rows/day: 30 LONG + 30 SHORT)

**Schema (from Codex investigation):**
```sql
CREATE TABLE shortlist_daily (
    date TEXT, ticker TEXT, direction TEXT, predicted_return REAL,
    beta REAL, market_component REAL, residual_alpha REAL,
    rank INTEGER, regime TEXT, sector TEXT, price REAL,
    top_shap_factors TEXT, factor_rank REAL, tcn_score REAL,
    final_score REAL, expected_return REAL, conviction REAL, alpha REAL,
    PRIMARY KEY (date, ticker)
);
```

**Returns normalized rows with Meridian-native fields:**
```python
{
    "row_id": "meridian:TRNO:LONG:2026-03-29",
    "source": "meridian",
    "symbol": "TRNO",
    "side": "LONG",
    "price": 60.71,
    "as_of": "2026-03-29T17:00:00-04:00",
    "tier": "meridian_long",
    "sector": "UNKNOWN",
    "regime": "TRENDING",
    "native": {
        "tcn_score": 0.860,
        "factor_rank": 0.902,
        "final_score": 0.8855,
        "residual_alpha": 0.0947,
        "predicted_return": 0.0947,
        "beta": 0.61,
        "rank": 1
    }
}
```

### 2.2 S1 Adapter (`s1_adapter.py`)

**Reads from:** `~/SS/Advance/data_cache/signalstack_results.db`
**Table:** `scorer_predictions`
**Also reads:** Latest `~/SS/Advance/evening_results/convergence_*.json`

**scorer_predictions columns:** ticker, direction, p_tp, nn_p_tp, scorer_prob, strategy, regime, sector, price, run_date, volume_ratio

**convergence JSON shape:** Array of objects with: ticker, direction, convergence_score, n_strategies_agree, strategies

**Merge logic:** Join scorer_predictions with convergence on (ticker, direction). If convergence missing for a symbol, set convergence_score=null, partial_data=true.

**Returns normalized rows with S1-native fields:**
```python
{
    "row_id": "s1:SHO:LONG:2026-03-29:scorer",
    "source": "s1",
    "symbol": "SHO",
    "side": "LONG",
    "price": 12.50,
    "as_of": "2026-03-29T17:16:00-04:00",
    "tier": "tier_dual",
    "sector": "REAL_ESTATE",
    "regime": "TRENDING",
    "native": {
        "p_tp": 0.614,
        "nn_p_tp": 0.894,
        "scorer_prob": 0.573,
        "convergence_score": 0.0,
        "n_strategies_agree": 0,
        "strategy": "edge",
        "volume_ratio": 1.2
    }
}
```

### 2.3 Vanguard Adapter (`vanguard_adapter.py`)

**Reads from:** `~/SS/Vanguard/data/vanguard_universe.db`
**For now:** Placeholder — returns Vanguard LGBM model predictions when available.
**Later:** Will read from a `vanguard_shortlist` table (after V5 Strategy Router is built).

**Returns normalized rows with Vanguard-native fields:**
```python
{
    "row_id": "vanguard:AAPL:LONG:2026-03-29",
    "source": "vanguard",
    "symbol": "AAPL",
    "side": "LONG",
    "price": 217.50,
    "as_of": "2026-03-29T16:00:00-04:00",
    "tier": "vanguard_long",
    "native": {
        "lgbm_long_prob": 0.65,
        "lgbm_short_prob": 0.12,
        "spread_proxy": 0.003,
        "session_phase": 0.7,
        "gap_pct": 0.012
    }
}
```

---

## 3. Field Registry (`field_registry.py`)

The field registry is a Python dict that defines every available column per source. The React frontend fetches this at startup and builds all filter/sort/column pickers dynamically.

```python
FIELD_REGISTRY = [
    # === COMMON (all sources) ===
    {"key": "symbol", "label": "Symbol", "type": "string", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": True, "format": "ticker"},
    {"key": "source", "label": "Source", "type": "enum", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": True, "format": "badge", "options": ["meridian", "s1", "vanguard"]},
    {"key": "side", "label": "Direction", "type": "enum", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": True, "format": "direction", "options": ["LONG", "SHORT"]},
    {"key": "price", "label": "Price", "type": "number", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "currency"},
    {"key": "as_of", "label": "As Of", "type": "datetime", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": True, "format": "datetime"},
    {"key": "tier", "label": "Tier", "type": "string", "sources": ["meridian", "s1", "vanguard"], "filterable": True, "sortable": True, "groupable": True, "format": "badge"},
    {"key": "sector", "label": "Sector", "type": "string", "sources": ["meridian", "s1"], "filterable": True, "sortable": False, "groupable": True, "format": "text"},
    {"key": "regime", "label": "Regime", "type": "string", "sources": ["meridian", "s1"], "filterable": True, "sortable": False, "groupable": True, "format": "badge"},

    # === MERIDIAN-NATIVE ===
    {"key": "tcn_score", "label": "TCN Score", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1", "description": "TCN v2 classifier probability"},
    {"key": "factor_rank", "label": "Factor Rank", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "final_score", "label": "Final Score", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "residual_alpha", "label": "Residual Alpha", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "percent"},
    {"key": "predicted_return", "label": "Predicted Return", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "percent"},
    {"key": "beta", "label": "Beta", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "decimal_2"},
    {"key": "m_lgbm_long_prob", "label": "LGBM Long (M)", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1", "description": "Meridian LGBM long classifier (when wired)"},
    {"key": "m_lgbm_short_prob", "label": "LGBM Short (M)", "type": "number", "sources": ["meridian"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1", "description": "Meridian LGBM short classifier (when wired)"},

    # === S1-NATIVE ===
    {"key": "p_tp", "label": "RF Prob", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "nn_p_tp", "label": "NN Prob", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "scorer_prob", "label": "Scorer Prob", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "convergence_score", "label": "Convergence", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "n_strategies_agree", "label": "Strategy Count", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "integer"},
    {"key": "strategy", "label": "Strategy", "type": "string", "sources": ["s1"], "filterable": True, "sortable": False, "groupable": True, "format": "text"},
    {"key": "volume_ratio", "label": "Volume Ratio", "type": "number", "sources": ["s1"], "filterable": True, "sortable": True, "groupable": False, "format": "decimal_1"},

    # === VANGUARD-NATIVE ===
    {"key": "v_lgbm_long_prob", "label": "LGBM Long (V)", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "v_lgbm_short_prob", "label": "LGBM Short (V)", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "v_tcn_long_score", "label": "TCN Long (V)", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "v_tcn_short_score", "label": "TCN Short (V)", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1"},
    {"key": "spread_proxy", "label": "Spread Proxy", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "decimal_4"},
    {"key": "session_phase", "label": "Session Phase", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "decimal_2"},
    {"key": "gap_pct", "label": "Gap %", "type": "number", "sources": ["vanguard"], "filterable": True, "sortable": True, "groupable": False, "format": "percent"},
]
```

**Endpoint:** `GET /api/v1/field-registry` — returns this list.

---

## 4. Candidate Endpoints

### GET /api/v1/candidates
Query params: `source` (meridian|s1|vanguard|combined), `direction` (LONG|SHORT|all), `sort` (field:asc|desc), `filters` (JSON), `page` (int), `page_size` (int)

**Logic:**
1. Based on `source`, call the appropriate adapter(s)
2. Apply filters (using field registry to validate)
3. Apply sort
4. Apply pagination
5. Return `{rows: [...], total: N, page: N, field_registry: [...]}`

### GET /api/v1/candidates/:row_id
Returns full detail for one candidate including all native fields.

---

## 5. Userview Endpoints

### DB Table
```sql
CREATE TABLE IF NOT EXISTS userviews (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    direction TEXT,
    filters TEXT,
    sorts TEXT,
    grouping TEXT,
    visible_columns TEXT,
    column_order TEXT,
    display_mode TEXT DEFAULT 'table',
    is_system INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Endpoints
- `GET /api/v1/userviews` — list all views
- `POST /api/v1/userviews` — create (body: name, source, direction, filters, sorts, visible_columns, column_order, display_mode)
- `PUT /api/v1/userviews/:id` — update
- `DELETE /api/v1/userviews/:id` — delete (reject if is_system=1)

### Seed System Views on First Run
Insert 11 preset views (from unified spec §2.3) with is_system=1. These cannot be deleted.

---

## 6. Trade Desk Endpoints

### GET /api/v1/picks/today
Returns all 7 tiers with today's picks. Reuse the tier logic from `~/SS/Vanguard/scripts/execute_daily_picks.py`.

**Response shape:**
```json
{
    "date": "2026-03-29",
    "tiers": [
        {
            "tier": "tier_dual",
            "label": "Dual Filter (S1)",
            "source": "s1",
            "picks": [
                {"symbol": "SHO", "direction": "LONG", "shares": 100, "scores": {"p_tp": 0.614, "nn_p_tp": 0.894, "scorer_prob": 0.573}}
            ]
        },
        {
            "tier": "tier_meridian_long",
            "label": "Meridian Longs",
            "source": "meridian",
            "picks": [
                {"symbol": "TRNO", "direction": "LONG", "shares": 100, "scores": {"tcn_score": 0.860, "factor_rank": 0.902}}
            ]
        }
    ],
    "total_picks": 37
}
```

**Data sources (same as execute_daily_picks.py):**
- S1: `~/SS/Advance/data_cache/signalstack_results.db` table `scorer_predictions`
- S1 convergence: `~/SS/Advance/evening_results/convergence_*.json`
- Meridian: `~/SS/Meridian/data/v2_universe.db` table `shortlist_daily`

**7 Tier Definitions:**
| Tier | Source | Filter |
|---|---|---|
| tier_dual | S1 | RF ≥ 0.60 AND NN ≥ 0.75 |
| tier_nn | S1 | NN ≥ 0.90 (excl dual) |
| tier_rf | S1 | RF > 0.60 AND Conv ≥ 0.80 (excl dual/nn) |
| tier_scorer_long | S1 | scorer ≥ 0.55 (excl above), top 10 |
| tier_s1_short | S1 | scorer ≥ 0.55, direction = SHORT |
| tier_meridian_long | Meridian | TCN ≥ 0.70 AND FR ≥ 0.80, top 10 |
| tier_meridian_short | Meridian | TCN < 0.30 AND FR > 0.90, top 5 |

### POST /api/v1/execute
**Body:** `{"trades": [{"symbol": "TRNO", "direction": "LONG", "shares": 100, "tier": "tier_meridian_long", "tags": ["tcn", "alpha"], "notes": "Strong TCN + FR alignment"}]}`

**Logic:**
1. For each trade, send to SignalStack webhook via existing `signalstack_adapter.py`
2. Log to `execution_log` table
3. Return results

**Webhook payload (SignalStack format):**
```json
{"symbol": "TRNO", "quantity": 100, "action": "buy"}
```
For shorts: `{"symbol": "PAYO", "quantity": 100, "action": "sell"}`

**Env var:** `SIGNALSTACK_WEBHOOK_URL` from `~/SS/Vanguard/.env`

### GET /api/v1/execution/log
Query params: `date` (default today), `limit`, `tag` (filter by tag), `outcome` (filter by WIN/LOSE/OPEN)
Returns execution history from `execution_log` table.

### PUT /api/v1/execution/:id
Update a trade's tags, notes, exit_price, outcome. Used for:
1. Adding tags at execution time ("I took this because of TCN score")
2. Updating outcome after 3-5 days ("This was a WIN, +2.3%")

### GET /api/v1/execution/analytics
Returns signal attribution analytics:
```json
{
    "by_tag": {
        "tcn": {"trades": 15, "wins": 9, "losses": 4, "open": 2, "win_rate": 0.692, "avg_pnl_pct": 1.23},
        "alpha": {"trades": 8, "wins": 5, "losses": 2, "open": 1, "win_rate": 0.714, "avg_pnl_pct": 0.95},
        "convergence": {"trades": 12, "wins": 7, "losses": 3, "open": 2, "win_rate": 0.700, "avg_pnl_pct": 1.45}
    },
    "by_tier": {
        "tier_dual": {"trades": 5, "wins": 4, "losses": 1, "win_rate": 0.800},
        "tier_meridian_long": {"trades": 20, "wins": 12, "losses": 6, "open": 2, "win_rate": 0.667}
    },
    "by_source": {
        "meridian": {"trades": 25, "win_rate": 0.680},
        "s1": {"trades": 18, "win_rate": 0.722}
    },
    "period": "last_30_days"
}
```
This is how you validate what's working after 3-5 days of trading.

### Execution Log Table
```sql
CREATE TABLE IF NOT EXISTS execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    tier TEXT NOT NULL,
    shares INTEGER NOT NULL,
    entry_price REAL,
    status TEXT DEFAULT 'SUBMITTED',
    signalstack_response TEXT,
    account TEXT,
    source_scores TEXT,             -- JSON: {tcn_score: 0.86, factor_rank: 0.90, ...}
    tags TEXT,                       -- JSON array: ["tcn", "alpha", "convergence"]
    notes TEXT,                      -- Free text: "Strong TCN + factor rank alignment"
    -- Forward tracking (updated after 3-5 days)
    exit_price REAL,                 -- Filled manually or via future integration
    exit_date TEXT,
    pnl_dollars REAL,
    pnl_pct REAL,
    outcome TEXT,                    -- WIN|LOSE|TIMEOUT|OPEN
    days_held INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

---

## 7. Report Endpoints

### Report Config Table
```sql
CREATE TABLE IF NOT EXISTS report_configs (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    schedule TEXT NOT NULL,
    delivery_channel TEXT DEFAULT 'telegram',
    blocks TEXT NOT NULL,
    telegram_bot_token TEXT,
    telegram_chat_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### Block Types (stored as JSON in `blocks` column)
```json
[
    {"type": "tier_block", "source": "s1", "tier": "tier_dual", "max_picks": 5, "sort_by": "scorer_prob", "format": "compact"},
    {"type": "tier_block", "source": "meridian", "tier": "tier_meridian_long", "max_picks": 10, "sort_by": "final_score", "format": "detailed"},
    {"type": "overlap_block", "sources": ["meridian", "s1"], "label": "Cross-System Overlap"},
    {"type": "summary_block", "stats": ["total_picks", "long_short_split", "top_sectors"]},
    {"type": "ai_brief_block", "model": "haiku", "prompt": "Generate a 2-sentence trading recommendation for these picks"},
    {"type": "forward_track_block", "lookback_days": 5, "metrics": ["win_rate", "avg_pnl_per_tier"]},
    {"type": "text_block", "content": "--- Good morning, here are today's picks ---"}
]
```

### Endpoints
- `GET /api/v1/reports` — list all report configs
- `POST /api/v1/reports` — create report config
- `PUT /api/v1/reports/:id` — update report config
- `DELETE /api/v1/reports/:id` — delete report config
- `POST /api/v1/reports/:id/test` — generate and send report NOW to Telegram

### Report Generation Logic
For each block in the report config:
1. **tier_block:** Query the appropriate adapter for that tier's picks, format as Telegram message
2. **overlap_block:** Cross-reference picks from multiple sources, find common tickers
3. **summary_block:** Aggregate stats across all tiers
4. **ai_brief_block:** Call Anthropic API (Haiku) with filtered picks as context
5. **forward_track_block:** Query pick_tracking / execution_log for historical performance
6. **text_block:** Static text, just include as-is

Send via Telegram using bot token and chat ID from report config (or env vars as fallback).

---

## 8. Health Endpoint

### GET /api/v1/health
```json
{
    "status": "ok",
    "sources": {
        "meridian": {"available": true, "last_date": "2026-03-29", "candidates": 60},
        "s1": {"available": true, "last_date": "2026-03-29", "candidates": 152},
        "vanguard": {"available": false, "reason": "No shortlist yet"}
    },
    "webhook": {"configured": true, "url": "https://app.signalstack.com/hook/..."},
    "telegram": {"configured": true}
}
```

---

## 9. CORS + Startup

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SignalStack Unified API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# On startup: create tables, seed system userviews
@app.on_event("startup")
async def startup():
    init_db()
    seed_system_userviews()
```

Port: 8090. Run with: `uvicorn vanguard.api.unified_api:app --host 0.0.0.0 --port 8090`

---

## 10. Testing

After building, verify:
```bash
# Start server
cd ~/SS/Vanguard
python3 -m uvicorn vanguard.api.unified_api:app --host 0.0.0.0 --port 8090 &

# Test endpoints
curl http://localhost:8090/api/v1/health
curl http://localhost:8090/api/v1/field-registry
curl "http://localhost:8090/api/v1/candidates?source=meridian&direction=LONG"
curl "http://localhost:8090/api/v1/candidates?source=s1"
curl "http://localhost:8090/api/v1/candidates?source=combined"
curl http://localhost:8090/api/v1/picks/today
curl http://localhost:8090/api/v1/userviews
curl http://localhost:8090/api/v1/reports
```

Write pytest tests for all adapters and endpoints.
