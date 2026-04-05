# Sprint 2 — Multi-Timeframe + Multi-Horizon Architecture

**Target env:** `Vanguard_QAenv` (all training + validation), then promote per Phase 6 pattern
**Est time:** 8–10 days of work (overnight training runs dominate)
**Depends on:** Phases 0, 2a, 2b, 3, 4, 5 all complete and promoted to prod
**Source of truth:** `SignalStack_Multi_Timeframe_Multi_Horizon_Architecture_v1_2.md`

---

## 0. Why this exists

The 5m crypto model is producing near-random signals (AAVE flipped 4× in 5 cycles, caused 5-6 losing crypto trades). This isn't fixed by a stability gate — the model itself is noise-dominated on 5m bars. We need **timeframe-specific models** + a **meta-model ensemble** so the 5m decision is anchored by 15m/30m/Daily structure.

This spec implements Stage 1 of the v1.2 architecture doc: multi-timeframe models per asset class, with a meta-model on top.

---

## 1. Scope for Sprint 2

**In scope:**
- Bar aggregation (5m → 10m, 15m, 30m) for crypto + forex + equity
- Per-timeframe feature computation at each TF
- Per-timeframe regressor training per asset class (predicting forward return at that TF's horizon)
- Meta-model that takes TF-level predictions as input and outputs final unified EV
- Integration into V4B scorer + V5 selection

**Out of scope (later sprints):**
- Volume Profile features (v1.2 §11)
- Order flow / cumulative delta features
- 1m trigger logic beyond existing entry timing
- Investment horizon classification
- Attention Set lead scoring from Daily → Intraday
- Full daily handshake ("Meridian leads → Vanguard intraday timing")

---

## 2. Timeframe roles (from v1.2 doc)

| TF | Role | Horizon (bars forward for TBM label) |
|---|---|---|
| 5m | Main decision layer | 12 bars (1h) |
| 10m | Structure confirmation | 6 bars (1h) |
| 15m | Structure + regime | 4 bars (1h) |
| 30m | Higher intraday structure | 4 bars (2h) |
| Daily | Conviction + macro bias | 3 bars (3 days) — already exists via Meridian |

**Primary decision layer:** 5m + Daily conviction (from Meridian). 10m/15m/30m confirm or conflict.

---

## 3. Architecture — per-timeframe stack

```
For each asset class (crypto, forex, equity):

  Bar aggregation:
    1m raw bars → 5m bars (exists)
    5m bars → 10m, 15m, 30m bars (new, vectorized resample)

  Feature computation per TF:
    Same feature engine, parameterized by TF
    Output: vanguard_features_{tf}m table

  Model per TF per asset class:
    Regressor predicting forward_return at that TF's horizon
    Output: vanguard_predictions_{tf}m table

  Meta-model:
    Input:  [pred_5m, pred_10m, pred_15m, pred_30m, daily_conviction]
    Output: unified_ev_score, mtf_alignment, direction, confidence
    Output table: vanguard_mtf_predictions
```

---

## 4. Work breakdown

### Day 1 — Bar aggregation + validation (QA)
**Deliverable:** `Vanguard_QAenv/scripts/aggregate_bars_mtf.py`

- Reads `vanguard_bars_5m` for each symbol
- Resamples to 10m, 15m, 30m using pandas `df.resample('10T').agg({...})` with OHLCV-correct aggregation (first/max/min/last/sum)
- Respects session boundaries (no bar spans session close)
- Writes to `vanguard_bars_10m`, `vanguard_bars_15m`, `vanguard_bars_30m`
- Handles crypto 24/7 vs forex weekend gaps vs equity RTH-only correctly

**Acceptance:**
- For BTCUSD.x, 24h of 5m bars (288 bars) produces exactly 144 10m, 96 15m, 48 30m bars
- For EURUSD.x, forex weekend gap produces no phantom bars
- For AAPL.x, no 30m bar spans 16:00 ET close

### Day 2 — Feature computation at each TF
**Deliverable:** extend `vanguard_factor_engine.py` or new `vanguard_factor_engine_mtf.py`

- Same features as existing V3 (momentum, ATR, rvol, etc.) computed at each TF
- Feature names suffixed: `momentum_3bar_5m`, `momentum_3bar_15m`, etc.
- Writes to `vanguard_features_5m`, `vanguard_features_10m`, etc. (or one wide table `vanguard_features_mtf`)

**Decision point (Shan):** separate tables per TF, or one wide table with all features? My recommendation: one wide table per cycle_ts × symbol with all TF features as columns. Easier joins for training.

### Day 3 — Training data backfill per TF per asset
**Deliverable:** `scripts/backfill_training_mtf.py`

- Computes forward returns at each TF's horizon (§2 table)
- Writes `vanguard_training_data_mtf` with columns: asset_class, tf, symbol, asof_ts, features..., forward_return_Nbars, tbm_label
- Overnight run per asset class (crypto first — most affected)

**Acceptance:**
- Row count reasonable: crypto should have ~100K rows per TF (from 689K 5m bars)
- No leakage: forward_return computed from bars STRICTLY after asof_ts

### Days 4–5 — Per-TF model training per asset
**Deliverable:** `scripts/train_mtf_models.py`

- Per (asset_class, tf): train regressor on vanguard_training_data_mtf filtered rows
- Model families to try: Ridge, LightGBM, XGBoost (start simple, Ridge already won the shootout per prior results)
- Walk-forward validation (same methodology as existing V4B)
- Save to `models/vanguard/mtf/{asset_class}_{tf}_{model_family}_v1.pkl`
- Run on Vast.ai A100 overnight per asset class

**Acceptance:**
- Per-TF IC > 0.02 on holdout for at least 3 of 4 TFs per asset class
- If a TF model IC < 0.01, flag it — possibly that TF adds no info for that asset
- Model metadata written to `vanguard_model_registry` with `tf` and `horizon_bars` fields

### Day 6 — Meta-model training
**Deliverable:** `scripts/train_meta_model.py`

- Input features: predictions from 5m/10m/15m/30m models (run inference on training set) + daily_conviction from Meridian
- Target: same as 5m — forward return at 5m horizon (since 5m is primary decision layer per v1.2)
- Simpler model (Ridge or shallow LGBM) — meta-model overfits easily
- Save to `models/vanguard/mtf/{asset_class}_meta_v1.pkl`

**Acceptance:**
- Meta-model IC ≥ max(individual TF IC) — meta must beat its best input
- If meta doesn't beat best input, do NOT ship — debug first

### Day 7 — Inference pipeline integration
**Deliverable:** new `stages/vanguard_scorer_mtf.py` + wire into V4B

- At each cycle: compute features at every TF for in-scope symbols
- Run per-TF model inference → 4 predictions per symbol
- Pull daily_conviction from Meridian's `predictions_daily` (if available, else default 0.5)
- Run meta-model → final unified_ev_score
- Write to `vanguard_mtf_predictions` with columns per v1.2 output contract (§7 of doc)

### Day 8 — Selection layer integration
**Deliverable:** update V5 to use MTF output

- V5 reads `vanguard_mtf_predictions.unified_ev_score` as primary ranking signal
- Direction comes from MTF model output
- Add `mtf_alignment` as a new shortlist column (STRONG_LONG, WEAK_LONG, CONFLICT, etc.)
- Reject candidates with `mtf_alignment == CONFLICT` (e.g., 5m says long, 30m says short, meta says weak)

### Day 9 — A/B shadow mode
**Deliverable:** run both V4B old model + MTF model in parallel, log both, compare

- Add `model_family` column to predictions: `legacy_5m` vs `mtf_meta_v1`
- For 3–5 days, log both. Don't switch live routing yet.
- Produce daily comparison report: same candidates, which model approved, which would have been more profitable

### Day 10 — Cutover decision
Based on A/B results: if MTF demonstrably beats legacy on win rate AND RRR, flip V5 to use MTF predictions as primary. If not, keep legacy and iterate.

---

## 5. What this does NOT fix tonight

- Crypto flipping during tonight's Phase 2b work is still a known issue
- Short-term: Shan continues manual GFT crypto trading this week
- Sprint 2 is week 2+ work, not the 6-8 hour overnight session

---

## 6. Risks

- **Training data quantity per TF.** 30m bars = 1/6 the count of 5m bars. Small data = fragile models. Mitigate: start with Ridge, not deep models.
- **Meta-model overfitting.** Classic multi-model trap. Mitigate: strong walk-forward, keep meta simple.
- **TF misalignment on asof timing.** 5m model prediction at 14:05 must align with 15m prediction valid at 14:00 or 14:15 (not mid-bar). Define exact asof rules before training.
- **Session boundary edge cases.** 30m bar spanning forex weekly open can pollute features. Already a known issue, needs careful handling.

---

## 7. Success criteria for Sprint 2

- Crypto signal stability measurably improved: < 1 flip per 5 cycles on in-scope symbols (vs 4/5 today)
- Meta-model IC beats legacy 5m model IC on same holdout
- MTF shortlist delivers higher avg RRR on shadow-traded approvals than legacy
- Shan is comfortable flipping V5 primary ranking to MTF output

---

## 8. What we learn in Sprint 1 that feeds Sprint 2

- Crypto spread-aware sizing (Phase 2b) applies to MTF-selected trades too
- Trade journal (Phase 3) provides the data to compare legacy vs MTF candidate selection post-hoc
- Shadow execution log (Phase 5) is the A/B comparison substrate

---

## 9. Open design questions (from v1.2 §15)

These don't block Sprint 2 start but should be answered by end of Day 9:
1. Daily conviction as lead generator, soft gate, or hard gate? — Sprint 2 uses as **soft feature** for meta-model only.
2. Exact conflict scoring math — to be defined during Day 6.
3. Horizon classification (Intraday vs Swing vs Investment) — not touched in Sprint 2.
4. Investment horizon — deferred.
5. Daily expansion to all asset classes — deferred.
