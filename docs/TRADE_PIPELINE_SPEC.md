# SIGNALSTACK TRADE PIPELINE SPEC — Apr 2, 2026

## Overview

The trade pipeline combines picks from S1 (daily swing) and Meridian (daily swing),
applies V6 risk filters, and writes approved trades to a queue for execution.

## Signal Paths

### S1 Daily Swing (US Equities)
```
Evening scan (8000+ tickers)
→ RF gate (threshold 0.55, validated 58.9% WR)
→ NN gate (threshold 0.90, effectively disabled)
→ OR logic: if RF passes OR NN passes → PASS
→ PASS rows written to evening_pass table
→ ML Scorer (LightGBM, 19 features) → scorer_prob
→ s1_daily_shortlist.py:
    LONG: scorer_prob >= 0.60, max 5
    SHORT: scorer_prob >= 0.50, max 5
→ daily_shortlist table in signalstack_results.db
```

### Meridian Daily Swing (US Equities)
```
Stage 1: Cache warm (Alpaca + YF data)
Stage 2: Prefilter (bond ETF blocklist + min ATR 0.5%)
Stage 3: Factor engine (19 features per ticker)
Stage 4: Dual TCN scoring (LONG IC +0.105, SHORT IC +0.392)
Stage 5: Top 5 LONG + 5 SHORT by TCN score
→ meridian_daily_shortlist.py extracts top 5+5
→ meridian_shortlist_final table in v2_universe.db
```

### Combined Pipeline
```
signalstack_trade_pipeline.py:
  1. Load S1 daily_shortlist (up to 5L + 5S)
  2. Load Meridian meridian_shortlist_final (up to 5L + 5S)
  3. Separate lane allocation:
     - S1 gets 5L + 5S max independently
     - Meridian gets 5L + 5S max independently
     - If same ticker in both → mark source="both" (higher conviction)
  4. V6 risk filter: check against account profile
  5. Write to trade_queue in signalstack_trades.db
```

## Files

| File | Location | Purpose |
|---|---|---|
| s1_daily_shortlist.py | ~/SS/Advance/ | S1 scorer-filtered top 5+5 |
| meridian_daily_shortlist.py | ~/SS/Meridian/ | Meridian TCN-ranked top 5+5 |
| signalstack_trade_pipeline.py | ~/SS/ | Combine + V6 filter + trade_queue |
| ultra.sh | ~/SS/ | Master runner (servers + orchestrators + shortlists + pipeline) |
| signalstack_mcp_server.py | ~/SS/ | MCP server v3 with trade_queue tool |

## DB Tables

### signalstack_results.db
- `daily_shortlist`: S1 final picks (ticker, direction, scorer_prob, p_tp, strategy, rank)

### v2_universe.db
- `meridian_shortlist_final`: Meridian final picks (ticker, direction, tcn_score, final_score, rank)

### signalstack_trades.db
- `trade_queue`: Combined approved trades (ticker, direction, source, scorer_prob, tcn_score, account, v6_status)

## V6 Account Profiles

| Profile | Max Positions | Daily Loss | Max Drawdown | Holding | Asset Scope |
|---|---|---|---|---|---|
| TTP_20K_SWING | 10 | $500 | $2,000 | Overnight OK | US equity |

## ultra.sh Modes

```bash
~/SS/ultra.sh              # Full: health + servers + FUC + Meridian + S1 + scorer + shortlists + pipeline
~/SS/ultra.sh --status     # Health check only
~/SS/ultra.sh --shortlist  # Shortlists + pipeline only (skip orchestrators)
```

## MCP Tools for Trade Data

| Tool | Reads From | Returns |
|---|---|---|
| daily_shortlist | signalstack_results.db.daily_shortlist | S1 final 5L+5S with scorer_prob |
| trade_queue | signalstack_trades.db.trade_queue | Combined approved trades for account |
| todays_picks | All shortlist tables | S1 + Meridian + trade_queue combined |
| meridian_shortlist | v2_universe.db | Full 30+30 and final 5+5 |
