
---

### Spec 2: Volume Profile + Order Flow Features (standalone spec for Sprint 2 / MTF)

```markdown
# Volume Profile + Order Flow Data Integration Spec for SignalStack

**Version:** 1.0  
**Date:** 2026-04-05  
**Purpose:** Define the exact Volume Profile and Order Flow features to add to the future-state SignalStack architecture (QAenv first, then Sprint 2).

---

## 1. Objective

Add high-signal timing features so SignalStack can better determine **when** to enter (not just direction).  
These features will be computed from bar and tick data and fed into the multi-timeframe models and meta-model.

---

## 2. Required Features

### 2.1 Volume Profile Features
- Session Volume Profile (current trading day/session)
- Visible Range Volume Profile
- Point of Control (POC)
- Value Area High (VAH)
- Value Area Low (VAL)
- High Volume Node (HVN) proximity
- Low Volume Node (LVN) proximity
- Volume Profile Imbalance (VAH–VAL skew)

### 2.2 Order Flow / Volume Delta Features
- Volume Delta per bar (buy volume – sell volume)
- Cumulative Volume Delta (running total)
- Delta Divergence (price vs cumulative delta)
- Aggressive vs Passive Volume Ratio
- Volume Imbalance at Price (per bar or at key levels)

---

## 3. Data Sources (priority order)

1. Existing 5m / 1m bars (current Twelve Data / IBKR / Alpaca) → for approximated Volume Profile
2. IBKR Tick Data (historical + real-time) → for accurate Volume Delta and Cumulative Delta
3. MT5 / MetaApi Tick History → secondary source for crypto/forex
4. Paid L2 / Footprint feeds (Bookmap, dxFeed, Rithmic, Sierra Chart) → only if the edge justifies the cost ($150–$400/month)

**Note:** True Level 2 / full footprint is expensive. Start with bar-based + IBKR tick data in QAenv.

---

## 4. Feature Engineering & Storage

- Compute in `vanguard/features/feature_computer.py`
- Store as additional columns in `vanguard_features` table
- Backfill script must recompute Volume Profile + Delta for historical training data
- Add new columns to the wide feature matrix used by models

**Example new feature names:**
- `volume_poc_distance_5m`
- `volume_vah_distance_5m`
- `volume_val_distance_5m`
- `cumulative_delta`
- `delta_divergence_5m`
- `volume_profile_imbalance`
- `high_volume_node_proximity`

---

## 5. Model Integration

- Retrain all models (crypto first) with the new features
- Add these features to the meta-model / ensemble layer (see Multi-Timeframe Architecture Doc)
- Expected benefit: significantly better timing and reduced false entries on low-conviction setups

---

## 6. Phased Rollout

**Phase 1 (QAenv only)**  
Add bar-based Volume Profile + basic Volume Delta using existing data.

**Phase 2**  
Add IBKR tick-based Cumulative Delta + full backfill.

**Phase 3 (optional, cost-dependent)**  
Integrate paid L2 / footprint feed via pluggable module (`vanguard/data_sources/level2_ingestor.py`).

---

## 7. Cost Summary (2026)

- Bar-based Volume Profile + Delta: **$0** (uses existing data)
- IBKR tick data: low additional cost
- Full Level 2 / footprint: **$150–$400/month**

---

## 8. Open Questions

- Exact weighting of new features in the meta-model
- How to handle missing tick data for some symbols
- On-the-fly vs pre-compute features
- UI visualization requirements for Volume Profile / Delta

---

**End of Spec**