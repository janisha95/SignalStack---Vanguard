# SignalStack Backend API Contract — Source of Truth
# Every endpoint, every field, every behavior. Frontend must conform to this.
# Date: Mar 31, 2026

---

## GET /api/v1/candidates?source={source}&direction={dir}&page_size={n}

### Request
| Param | Type | Default | Values |
|---|---|---|---|
| source | string | meridian | meridian, s1, vanguard, combined |
| direction | string | all | LONG, SHORT, all |
| page_size | int | 500 | 1-1000 |
| sort | string | final_score:desc | {field}:{asc|desc} |
| filters | JSON | [] | [{field, op, value}] |

### Response
```json
{
  "rows": [...],
  "total": 152,
  "source": "s1",
  "page_size": 500,
  "as_of": "2026-03-29T17:16:00-04:00"
}
```

### Row shape — Meridian
```json
{
  "row_id": "meridian:TRNO:LONG:2026-03-30",
  "source": "meridian",
  "symbol": "TRNO",
  "side": "LONG",
  "price": 60.71,
  "tier": "meridian_long",
  "sector": "UNKNOWN",
  "regime": "TRENDING",
  "as_of": "2026-03-30T17:00:00-04:00",
  "native": {
    "tcn_score": 0.860,
    "factor_rank": 0.902,
    "final_score": 0.886,
    "residual_alpha": 0.0947,
    "predicted_return": 0.0947,
    "beta": 0.61,
    "m_lgbm_long_prob": 0.476,
    "m_lgbm_short_prob": 0.124
  }
}
```

**RULES:**
- `tcn_score` MUST be from TCN scorer, NOT hardcoded 0.5. If TCN falls back, set to null not 0.5
- `m_lgbm_long_prob` and `m_lgbm_short_prob` MUST come from predictions_daily JOIN, NOT hardcoded None
- `price` MUST be non-zero. If shortlist_daily.price is NULL, fall back to daily_bars latest close
- `tier` MUST be computed from thresholds: tcn>=0.70 AND fr>=0.80 = meridian_long, tcn<0.30 AND fr>0.90 = meridian_short. If TCN is null/0.5, tier = "meridian_untiered"
- All 60 candidates (30L + 30S) from latest shortlist_daily date MUST be returned

### Row shape — S1
```json
{
  "row_id": "s1:JPM:LONG:2026-03-29:scorer",
  "source": "s1",
  "symbol": "JPM",
  "side": "LONG",
  "price": 248.50,
  "tier": "tier_scorer_long",
  "sector": "FINANCIALS",
  "regime": "TRENDING",
  "as_of": "2026-03-29T17:16:00-04:00",
  "native": {
    "p_tp": 0.6144,
    "nn_p_tp": 0.0059,
    "scorer_prob": 0.5801,
    "convergence_score": 1.0672,
    "n_strategies_agree": 1,
    "strategy": "edge",
    "volume_ratio": 1.0
  }
}
```

**RULES:**
- `side` MUST be normalized: BUY→LONG, SELL→SHORT
- `convergence_score` MUST come from latest convergence file by mtime (NOT by filename date). Load using sorted(glob, key=os.path.getmtime, reverse=True)[0]
- Convergence data is under the `shortlist` key in the JSON, keyed by (ticker, direction)
- If ticker not in convergence file, set convergence_score=0.0 and partial_data=true
- `price` MUST be non-zero. If scorer_predictions.price is NULL/0, pull from daily_bars
- ALL scorer_predictions rows for latest run_date MUST be returned (not capped at 50)
- `tier` computed from: RF>=0.60 AND NN>=0.75 = tier_dual, NN>=0.90 = tier_nn, scorer>=0.55 = tier_scorer_long, direction=SHORT AND scorer>=0.55 = tier_s1_short

### Row shape — Combined
Returns BOTH Meridian and S1 rows merged. Default sort: source ASC then as_of DESC.
Never fake a unified score. Source-native fields show "—" for non-matching sources.

---

## GET /api/v1/picks/today

Returns all 7 tiers with today's picks for the Trade Desk.

```json
{
  "date": "2026-03-30",
  "tiers": [
    {"tier": "tier_dual", "label": "Dual Filter (S1)", "source": "s1", "picks": [...]},
    {"tier": "tier_nn", "label": "NN Top (S1)", "source": "s1", "picks": [...]},
    {"tier": "tier_rf", "label": "RF High (S1)", "source": "s1", "picks": [...]},
    {"tier": "tier_scorer_long", "label": "Scorer Long (S1)", "source": "s1", "picks": [...]},
    {"tier": "tier_s1_short", "label": "S1 Shorts", "source": "s1", "picks": [...]},
    {"tier": "tier_meridian_long", "label": "Meridian Longs", "source": "meridian", "picks": [...]},
    {"tier": "tier_meridian_short", "label": "Meridian Shorts", "source": "meridian", "picks": [...]}
  ],
  "total_picks": 37
}
```

---

## POST /api/v1/execute

### Request
```json
{
  "trades": [{
    "symbol": "TRNO",
    "direction": "LONG",
    "shares": 328,
    "tier": "tier_meridian_long",
    "tags": ["tcn", "alpha"],
    "notes": "Strong TCN + FR",
    "entry_price": 60.71,
    "stop_loss": 59.49,
    "take_profit": 63.14,
    "order_type": "market",
    "limit_buffer": 0.01
  }]
}
```

### Response
```json
{
  "results": [
    {"symbol": "TRNO", "id": 42, "status": "SUBMITTED", "webhook_response": "OK"}
  ]
}
```

---

## GET /api/v1/execution/log?date={date}&tag={tag}&outcome={outcome}&limit={n}

### Response
```json
{
  "rows": [{
    "id": 42,
    "executed_at": "2026-03-31T09:31:00",
    "symbol": "TRNO",
    "direction": "LONG",
    "tier": "tier_meridian_long",
    "shares": 328,
    "entry_price": 60.71,
    "stop_loss": 59.49,
    "take_profit": 63.14,
    "order_type": "market",
    "status": "SUBMITTED",
    "tags": ["tcn", "alpha"],
    "notes": "Strong TCN + FR",
    "outcome": null,
    "exit_price": null,
    "pnl_dollars": null,
    "pnl_pct": null,
    "account": "ttp_demo_1m"
  }]
}
```

---

## DELETE /api/v1/execution/{id}

Deletes a trade from the execution log. Returns 204 on success, 404 if not found.

**RULE:** This endpoint MUST exist. The operator needs to clear test trades.

---

## PUT /api/v1/execution/{id}

Updates forward-tracking fields: exit_price, outcome, notes, tags.

---

## GET /api/v1/execution/analytics?period={period}

Returns win rate by tag, tier, source.

---

## GET /api/v1/field-registry

Returns all available fields with metadata. Frontend uses this to build filter/sort/column pickers dynamically.

**RULE:** Every field in this registry MUST actually be populated in the candidate rows. If a field shows "—" in the UI, either the backend isn't sending it or the frontend isn't reading it correctly.

---

## GET /api/v1/userviews

Returns all saved views. System views have is_system=true and cannot be deleted.

**RULE:** Every view (system or custom) MUST be fully restorable. Loading a view restores: source, direction, filters, sorts, visible_columns, column_order, display_mode.

## POST /api/v1/userviews
## PUT /api/v1/userviews/{id}
## DELETE /api/v1/userviews/{id}

Standard CRUD. DELETE returns 403 for system views.

---

## GET /api/v1/reports
## POST /api/v1/reports
## PUT /api/v1/reports/{id}
## DELETE /api/v1/reports/{id}
## POST /api/v1/reports/{id}/test

Report config CRUD + test send to Telegram.

---

## GET /api/v1/health

```json
{
  "status": "ok",
  "sources": {
    "meridian": {"available": true, "last_date": "2026-03-30", "candidates": 60},
    "s1": {"available": true, "last_date": "2026-03-29", "candidates": 152}
  },
  "webhook": {"configured": true},
  "telegram": {"configured": true}
}
```

---

## KNOWN BUGS TO FIX

1. **Meridian TCN score = 0.5 everywhere** — factor_history missing 7 columns historically. TCN falls back to 0.5. Display as null/"—" not 0.5 so operator knows it's fallback
2. **Meridian LGBM scores showing "—"** — predictions_daily has predicted_return but may not have lgbm_long_prob/lgbm_short_prob columns. Need lgbm_scorer.py to write both probs
3. **S1 convergence loading wrong file** — adapter must use os.path.getmtime sort, read `shortlist` key from JSON
4. **$0.00 prices** — shortlist_daily.price is NULL, need daily_bars fallback
5. **Trade log delete** — DELETE endpoint missing, operator can't remove test trades
6. **ATR calculation hardcoded** — Stop methods use fake percentages not real ATR. Need real ATR from backend
7. **"n strategies agree" chip label** — raw field name displayed, should be "N Agree" or "Strategies"
8. **S1 Short Picks view shows Meridian data** — System view has wrong source filter
