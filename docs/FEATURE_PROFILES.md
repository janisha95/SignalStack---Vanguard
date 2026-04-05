# Per-Asset Feature Profiles — V4B Training Curation

**Status:** APPROVED  
**Date:** March 31, 2026  
**Purpose:** Curated feature lists per asset class for LightGBM training  
**Config file:** `config/vanguard_feature_profiles.json`  
**Read by:** `stages/vanguard_model_trainer.py`

---

## Problem

V4B was training on ALL columns from `vanguard_training_data` including noise features. The trained models latched onto metadata instead of price signal:

**Forex long model top features (BEFORE curation):**
1. `time_in_session_pct` — session timing metadata, NOT price signal
2. `atr_expansion` — OK
3. `spread_proxy` — data quality indicator
4. `nan_ratio` — NOT EVEN A REAL FEATURE

Result: garbage IC (0.001 equity, -0.034 crypto).

## Meridian TCN Precedent

Meridian TCN trained on **19 curated features** → IC = 0.031 (PASS):

```
adx, bb_position, dist_from_ma20_atr, rs_vs_spy_10d, volume_participation,
momentum_acceleration, volatility_rank, wyckoff_phase, ma_alignment,
leadership_score, setup_score, damage_depth, volume_climax, rs_vs_spy_20d,
ma_death_cross_proximity, downside_volume_dominance, phase_confidence,
directional_conviction, vix_regime
```

Key principle: **fewer high-signal features > all features with noise**

---

## Universal Excludes (NEVER train on these)

| Feature | Reason |
|---|---|
| `nan_ratio` | Data quality metric, not a feature |
| `asset_class_encoded` | Constant within per-asset training |
| `daily_conviction` | Always 0 (placeholder for future) |
| `time_in_session_pct` | Session metadata, not price signal |
| `bars_since_session_open` | Correlated with above |
| `spread_proxy` | Data quality indicator |

---

## Vanguard 35 Features (V3 computes ALL for ALL symbols)

### Group 1: Price Location (4)
1. `session_vwap_distance`
2. `session_opening_range_position`
3. `gap_pct`
4. `dist_from_session_high_low`

### Group 2: Momentum (4)
5. `momentum_3bar`
6. `momentum_12bar`
7. `momentum_acceleration`
8. `atr_expansion`

### Group 3: Volume (4)
9. `relative_volume`
10. `volume_burst_z`
11. `effort_vs_result`
12. `down_volume_ratio`

### Group 4: Market Context (3)
13. `rs_vs_benchmark_intraday`
14. `benchmark_momentum_12bar`
15. `cross_asset_correlation`

### Group 5: Daily Bridge (5)
16. `daily_atr_pct`
17. `daily_conviction` ← ALWAYS 0, EXCLUDED
18. `daily_rs_vs_benchmark`
19. `daily_drawdown_from_high`
20. `daily_adx`

### Group 6: SMC 5m (6)
21. `fvg_bullish_nearest`
22. `fvg_bearish_nearest`
23. `ob_proximity_5m`
24. `structure_break`
25. `liquidity_sweep`
26. `premium_discount_zone`

### Group 7: SMC HTF 1H (4)
27. `htf_trend_direction`
28. `htf_structure_break`
29. `htf_fvg_nearest`
30. `htf_ob_proximity`

### Group 8: Session/Time (3)
31. `session_phase`
32. `time_in_session_pct` ← EXCLUDED (metadata)
33. `bars_since_session_open` ← EXCLUDED (metadata)

### Group 9: Quality (1)
34. `spread_proxy` ← EXCLUDED (quality indicator)

### Group 10: Meta (1)
35. `asset_class_encoded` ← EXCLUDED (constant in per-asset)

---

## Per-Asset Feature Profiles

### Equity — 25 features

**Included:**
- Price Location: `session_vwap_distance`, `session_opening_range_position`, `gap_pct`, `dist_from_session_high_low`
- Momentum: `momentum_3bar`, `momentum_12bar`, `momentum_acceleration`, `atr_expansion`
- Volume: `relative_volume`, `volume_burst_z`, `effort_vs_result`, `down_volume_ratio`
- Market Context: `rs_vs_benchmark_intraday`, `benchmark_momentum_12bar`
- Daily Bridge: `daily_atr_pct`, `daily_rs_vs_benchmark`, `daily_drawdown_from_high`, `daily_adx`
- SMC 5m: `fvg_bullish_nearest`, `fvg_bearish_nearest`, `ob_proximity_5m`, `structure_break`, `premium_discount_zone`
- SMC HTF: `htf_trend_direction`, `htf_structure_break`

**Excluded:** 6 universal excludes + `cross_asset_correlation` (needs tuning) + `liquidity_sweep` (sparse) + `htf_fvg_nearest` (sparse) + `htf_ob_proximity` (sparse)

### Forex — 25 features

**Included:**
- Price Location: `session_vwap_distance`, `dist_from_session_high_low`
- Momentum: `momentum_3bar`, `momentum_12bar`, `momentum_acceleration`, `atr_expansion`
- Volume: `effort_vs_result` (only volume feature — tick volume unreliable for rest)
- Market Context: `rs_vs_benchmark_intraday`, `benchmark_momentum_12bar`, `cross_asset_correlation`
- Daily Bridge: `daily_atr_pct`, `daily_rs_vs_benchmark`, `daily_drawdown_from_high`, `daily_adx`
- SMC 5m: ALL 6 (`fvg_bullish_nearest`, `fvg_bearish_nearest`, `ob_proximity_5m`, `structure_break`, `liquidity_sweep`, `premium_discount_zone`)
- SMC HTF: ALL 4 (`htf_trend_direction`, `htf_structure_break`, `htf_fvg_nearest`, `htf_ob_proximity`)
- Session: `session_phase`

**Excluded:** 6 universal excludes + `gap_pct` (no gaps 24/5) + `session_opening_range_position` (ambiguous) + `relative_volume` (tick vol) + `volume_burst_z` (tick vol) + `down_volume_ratio` (tick vol)

### Crypto — 19 features

**Included:**
- Price Location: `dist_from_session_high_low`
- Momentum: `momentum_3bar`, `momentum_12bar`, `momentum_acceleration`, `atr_expansion`
- Volume: `effort_vs_result`
- Daily Bridge: `daily_atr_pct`, `daily_drawdown_from_high`, `daily_adx`
- SMC 5m: ALL 6
- SMC HTF: ALL 4

**Excluded:** 6 universal excludes + `session_vwap_distance` (no clear session 24/7) + `session_opening_range_position` (no session open) + `gap_pct` (24/7) + ALL remaining volume features (tick vol) + ALL benchmark features (crypto self-referential) + `session_phase` (no sessions)

### Metal — 23 features

**Included:**
- Price Location: `session_vwap_distance`, `dist_from_session_high_low`, `gap_pct`
- Momentum: ALL 4
- Volume: `effort_vs_result`
- Market Context: `rs_vs_benchmark_intraday`, `benchmark_momentum_12bar`, `cross_asset_correlation`
- Daily Bridge: `daily_atr_pct`, `daily_rs_vs_benchmark`, `daily_drawdown_from_high`, `daily_adx`
- SMC 5m: `fvg_bullish_nearest`, `fvg_bearish_nearest`, `ob_proximity_5m`, `structure_break`, `premium_discount_zone`
- SMC HTF: `htf_trend_direction`, `htf_structure_break`
- Session: `session_phase`

**Excluded:** 6 universal excludes + volume features (tick vol) + `session_opening_range_position` (ambiguous) + sparse HTF + sparse `liquidity_sweep`

### Commodity — 17 features

**Included:**
- Price Location: `session_vwap_distance`, `dist_from_session_high_low`, `gap_pct`
- Momentum: ALL 4
- Daily Bridge: `daily_atr_pct`, `daily_drawdown_from_high`, `daily_adx`
- SMC 5m: `fvg_bullish_nearest`, `fvg_bearish_nearest`, `ob_proximity_5m`, `structure_break`, `premium_discount_zone`
- SMC HTF: `htf_trend_direction`
- Session: `session_phase`

**Excluded:** 6 universal excludes + ALL volume + ALL benchmark (no clear benchmark for agriculture/energy) + most HTF (sparse)

---

## Summary

| Asset Class | Feature Count | Key Signal Groups |
|---|---|---|
| Equity | 25 | Momentum, volume, benchmark RS, SMC, daily bridge |
| Forex | 25 | Momentum, SMC (5m + HTF full), benchmark RS, session phase |
| Crypto | 19 | Momentum, SMC (5m + HTF full), daily bridge only |
| Metal | 23 | Momentum, SMC, benchmark RS, session phase |
| Commodity | 17 | Momentum, SMC, daily bridge, session phase |

---

## V3 vs V4B Separation

- **V3** computes ALL 35 features for ALL symbols (correct — factor engine is universal)
- **V4B** reads `config/vanguard_feature_profiles.json` and trains ONLY on curated features
- This separation is intentional — V3 is a compute stage, V4B is a modeling stage
- V5 uses the full feature matrix from V3 for strategy routing; only V4B ML training is curated
