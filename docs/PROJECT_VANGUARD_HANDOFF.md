# Project Vanguard — Master Handoff
# Date: Mar 28, 2026
# Status: APPROVED — Ready for Stage V1 Speccing

---

## What is Vanguard?

Multi-asset, multi-prop-firm, ML-driven intraday trading system with Smart Money Concepts (SMC).
Runs parallel to daily Meridian equity system.

Meridian = daily equity swing picks (built, operational)
Vanguard = intraday/swing across forex, indices, metals, agriculture, energy, equity CFDs, crypto, futures (building now)

---

## Three Trading Lanes

| Lane | System | Prop Firm | Instruments | Status |
|---|---|---|---|---|
| 1. Daily Equities | Meridian | TTP Swing $20K | 3,000 US equities | Ready — demo this week, eval after Apr 2 |
| 2. Intraday CFDs | Vanguard | FTMO, FundedNext, The5ers, Blue Guardian | 165 CFDs (forex/indices/metals/agriculture/energy/equities/crypto) | Building now |
| 3. Intraday Futures | Vanguard | Topstep, Apex, TradeDay, Elite | 22 CME futures (ES/NQ/CL/GC + micros + grains + treasuries) | Same system, different data adapter |

---

## Universe: 187 Instruments

### Forex Majors & Minors (28)
EURUSD, GBPUSD, USDCHF, USDJPY, USDCAD, AUDUSD, NZDUSD, AUDNZD, AUDCAD, AUDCHF, AUDJPY, CHFJPY, EURGBP, EURAUD, EURCHF, EURJPY, EURNZD, EURCAD, GBPCHF, GBPJPY, GBPAUD, GBPCAD, GBPNZD, CADCHF, CADJPY, NZDCAD, NZDCHF, NZDJPY

### Forex Exotics (15)
EURCZK, EURHUF, EURNOK, EURPLN, USDCZK, USDHKD, USDHUF, USDILS, USDMXN, USDNOK, USDPLN, USDSEK, USDZAR, USDSGD, USDCNH

### Indices (16)
US30.cash, US100.cash, US500.cash, UK100.cash, GER40.cash, AUS200.cash, EU50.cash, FRA40.cash, HK50.cash, JP225.cash, N25.cash, SPN35.cash, US2000.cash, DXY.cash, UKOIL.cash, USOIL.cash

### Metals (9)
XAUUSD, XAUAUD, XAUEUR, XAGAUD, XAGEUR, XAGUSD, XPDUSD, XPTUSD, XCUUSD

### Agriculture (7)
COCOA.c, COFFEE.c, CORN.c, SOYBEAN.c, WHEAT.c, COTTON.c, SUGAR.c

### Energy/Commodities (2)
NATGAS.cash, HEATOIL.c

### US Equity CFDs (45)
AAPL, AMZN, BABA, BAC, GOOG, MSFT, NFLX, NVDA, PFE, RACE, T, TSLA, V, WMT, ZM, META, GE, BA, RTX, LMT, PLTR, AMD, INTC, QCOM, AVGO, CSCO, JNJ, MCD, SBUX, KO, MSTR, GME, NKE, CVX, FDX, JPM, DIS, GM, IBM, ARM, SNOW, ASML, AZN, BRK.B, XOM

### EU Equity CFDs (13)
AIRF, ALVG, BAYGn, DBKGn, IBE, LVMH, VOWG_p, TTE, SAN, ADSGn, BMW, MBG, SIEGn

### Crypto Major (11)
ADAUSD, BTCUSD, DASHUSD, DOTUSD, ETHUSD, LTCUSD, XRPUSD, BCHUSD, SOLUSD, AVAXUSD, ETCUSD

### Crypto Alt (16)
DOGEUSD, NEOUSD, XMRUSD, BNBUSD, SANUSD, LNKUSD, NERUSD, ALGUSD, ICPUSD, AAVEUSD, BARUSD, GALUSD, GRTUSD, IMXUSD, MANUSD, VECUSD

### Futures — Equity Index (8)
ES, MES, NQ, MNQ, YM, MYM, RTY, M2K

### Futures — Energy (3)
CL, MCL, NG

### Futures — Metals (5)
GC, MGC, SI, SIL, HG

### Futures — Grains (3)
ZC, ZS, ZW

### Futures — Treasuries (3)
ZB, ZN, ZF

---

## 31 Features

### Group 1: Price Location (4)
1. session_vwap_distance — price vs session VWAP (tick vol for forex)
2. session_opening_range_position — position within session opening range (session-aware per asset class)
3. gap_pct — overnight gap from previous close (0 for 24/7 crypto)
4. dist_from_session_high_low — position within current session range

### Group 2: Momentum (3)
5. momentum_3bar — 15-min momentum
6. momentum_12bar — 60-min momentum
7. momentum_acceleration — is momentum speeding up or slowing down?

### Group 3: Volume (3)
8. relative_volume — current bar volume vs session average (tick vol proxy for forex)
9. volume_burst_z — z-score of current bar volume
10. effort_vs_result — volume relative to price change

### Group 4: Market Context (3)
11. rs_vs_benchmark_intraday — return vs per-asset benchmark (SPY for equities, DXY for forex, BTCUSD for crypto)
12. benchmark_momentum_12bar — benchmark trend direction
13. cross_asset_correlation — correlation to asset class peers

### Group 5: Daily Bridge (3)
14. daily_atr_pct — daily volatility context
15. daily_conviction — 1 if Meridian/S1 daily pick, 0 otherwise (equities only)
16. daily_rs_vs_benchmark — daily relative strength vs per-asset benchmark

### Group 6: SMC — 5-Minute (6)
17. fvg_bullish_nearest — distance to nearest unfilled bullish FVG
18. fvg_bearish_nearest — distance to nearest unfilled bearish FVG
19. order_block_proximity — distance to nearest unmitigated order block
20. structure_break — BOS (+1 bullish, -1 bearish, 0 none)
21. liquidity_sweep — wick beyond swing high/low then close back (+1/-1/0)
22. premium_discount_zone — position within current swing range (>0.5 = premium, <0.5 = discount)

### Group 7: SMC — Higher Timeframe 1H (4)
23. htf_trend_direction — 1H trend (bullish/bearish/neutral)
24. htf_structure_break — 1H BOS or CHoCH
25. htf_fvg_nearest — distance to nearest 1H FVG
26. htf_order_block_proximity — distance to nearest 1H order block

### Group 8: Session/Time (3)
27. session_phase — which session (Asia=0, London=1, NY=2, overlap=3)
28. time_in_session_pct — how far through current session (0-100%)
29. bars_since_session_open — raw bar count since session open

### Group 9: Quality (1)
30. spread_proxy — current spread vs average

### Group 10: Meta (1)
31. asset_class — encoded integer (forex=0, index=1, metal=2, agriculture=3, energy=4, equity=5, crypto=6)

---

## TBM Labeling (Intraday)
- Bar interval: 5-minute
- Horizon: 6 bars (30 minutes)
- Take profit: +0.30%
- Stop loss: -0.15%
- Session boundary: label path cannot cross into next session
- Breakeven WR: 33.3%
- Configurable — tune after first training run

---

## Data Vendors

| Purpose | Vendor | Cost |
|---|---|---|
| Live CFD data | MT5 API (already running) | $0/mo |
| Live futures data | NinjaTrader/Tradovate (with prop firm account) | $0/mo |
| Historical backfill | Databento (one-time charge) | ~$50-100 one-time |
| Daily equity system | Alpaca IEX (unchanged) | $0/mo |
| **Monthly data cost** | | **$0** |

---

## Execution Bridge: TradersPost
- Cost: $49/mo base + $10/extra account
- System sends JSON webhook → TradersPost routes to all connected prop firm accounts
- Supports: FTMO (MT5), Topstep (Tradovate), Apex (Tradovate), and more
- One signal → multiple prop firm accounts simultaneously
- Mobile dashboard for 1-click approval or full auto-execution
- Alternative: PickMyTrade ($50/mo flat), direct MT5 API (free, FTMO-only)

---

## Key Technical Decisions

| Decision | Answer |
|---|---|
| Project name | Vanguard |
| DB architecture | SQLite WAL, DB reads only (no in-memory cache for v1) |
| SMC computation | Stage V3 (factor engine), using `smartmoneyconcepts` Python library |
| Multi-asset from day 1 | Yes — schema has asset_class column, per-asset session awareness |
| Bar storage | 1m canonical, 5m derived |
| ML from day 1 | Yes — historical download starts immediately |
| Session awareness | Per-asset-class (forex 24/5, crypto 24/7, equities US hours) |
| File naming | vanguard_cache.py, vanguard_prefilter.py, etc. |
| DB file | vanguard_universe.db |
| DB isolation | Separate from Meridian DB, never cross-writes |
| Higher timeframe | 1H bars for HTF SMC features |
| Warm-up period | Accept 30-60 min warm-up after session open |
| Volume proxy | Tick volume for forex/CFDs, real volume for equities/futures |
| Benchmark per asset | SPY for equities, DXY for forex, BTCUSD for crypto alts |
| Selection model | Probability-weighted E[r] with conviction scaling, direction=sign(residual_alpha) |
| Beta stripping | Bounded at ±3.0, weight=0.35 (lighter than daily) |
| Execution | TradersPost webhook ($49/mo) or direct MT5 API (free) |

---

## Pipeline Architecture (7 Stages)

```
Stage V1: Data Pipeline (vanguard_cache.py)
  └─ MT5 API → 1m bars → vanguard_universe.db → 5m derived bars
  └─ Per-asset-class session awareness
  └─ NinjaTrader/Tradovate adapter for futures
  └─ Health monitoring, reconnect, heartbeat

Stage V2: Prefilter (vanguard_prefilter.py)
  └─ Morning pass + periodic refresh
  └─ Liquidity, spread, halt detection
  └─ ~150-187 instruments (mostly pass, universe is pre-curated)

Stage V3: Factor Engine (vanguard_factor_engine.py)
  └─ 31 features across 10 groups
  └─ SMC features via smartmoneyconcepts library
  └─ HTF features from 1H bars
  └─ Asset-class-aware computation

Stage V4A: Training Backfill (vanguard_training_backfill.py)
  └─ Databento historical 1m bars (one-time)
  └─ Replay factor computation on historical bars
  └─ TBM label generation (+0.30% / -0.15% / 30-min horizon)

Stage V4B: Model Trainer (vanguard_model_trainer.py)
  └─ Walk-forward validation by session blocks
  └─ LGBM baseline + TCN/LSTM challenger
  └─ Target IC > 0.03

Stage V5: Selection (vanguard_selection.py)
  └─ Probability-weighted E[r] with conviction scaling
  └─ Direction = sign(residual_alpha)
  └─ Bounded beta stripping (weight=0.35)
  └─ Per-asset-class benchmark

Stage V6: Risk Filters (vanguard_risk.py)
  └─ Per-prop-firm risk rules (FTMO, Topstep, Apex, TTP)
  └─ ATR-based sizing
  └─ EOD flatten enforcement (where required)
  └─ Sector/correlation caps

Stage V7: Orchestrator (vanguard_orchestrator.py)
  └─ Per-asset-class session loop
  └─ Every 5 min during active sessions
  └─ Publishes to API + TradersPost webhook
  └─ Health monitoring, Telegram alerts
```

---

## Prop Firm Matrix

| Type | Firms | Platform | Instruments |
|---|---|---|---|
| CFD/Forex | FTMO, FundedNext, The5ers, Blue Guardian | MT4/MT5/cTrader | Forex, Indices, Metals, Commodities, Equities, Crypto |
| Futures | Topstep, Apex, TradeDay, Elite, MyFundedFutures | NinjaTrader/Tradovate | ES, NQ, CL, GC + micros + grains + treasuries |
| US Equities Swing | Trade The Pool | Trader Evolution | 12,000+ US equities |

---

## TTP Decision (Pending Apr 2)
- Demo both Swing and Intraday this week
- Forward tracker resolves Apr 2 (120 picks, first real win/loss data)
- If overnight holds win → TTP Swing $20K ($670)
- If same-day closes competitive → TTP Intraday $50K ($337)
- Start FTMO when Vanguard system validated

---

## Session Hours by Asset Class
| Asset Class | Trading Hours (ET) | Session Open |
|---|---|---|
| Forex | Sun 17:00 → Fri 17:00 (24/5) | NY session: 8:00 AM ET |
| US Indices | Near 24h with breaks | 9:30 AM ET |
| EU Indices | ~02:00 → 16:00 | 3:00 AM ET |
| Asia Indices | ~19:00 → 03:00 | 7:00 PM ET |
| Metals | Sun 18:00 → Fri 17:00 with break | 6:00 PM ET |
| Agriculture | Limited hours, varies | Varies |
| Energy | Sun 18:00 → Fri 17:00 with break | 6:00 PM ET |
| Equity CFDs | US: 09:30–16:00, EU: 02:00–11:30 | 9:30 AM / 3:00 AM ET |
| Crypto | 24/7 | 00:00 UTC |
| Futures | Sun 18:00 → Fri 17:00 with break | 6:00 PM ET |

---

## Key Notion Pages
- START HERE: 32ab2399fcac8122984fe294e072f300
- Vanguard handoff: 331b2399fcac819bb87ac91db381adad
- FTMO Universe: 331b2399fcac81769073e07b1e078800
- Meridian handoff: 330b2399fcac81b38cd5c519c0a4f4c7
- Meridian final: 331b2399fcac81aaa0a8c5e2e605bc2d
- DRAFT build plan (not approved): 331b2399fcac8132a5b6d4d03dfd81c1

---

## Monthly Costs
| Item | Cost |
|---|---|
| Data (MT5 + Alpaca) | $0 |
| TradersPost execution | $49 USD (~$66 CAD) |
| Prop firm evals | Varies ($155-$670 per eval) |
| Databento backfill | ~$50-100 one-time |
| **Monthly recurring** | **~$66 CAD** |

---

## Next Step
Spec Stage V1 (Data Pipeline) — MT5-based cache service for 187 instruments
across multiple asset classes with per-session awareness.
