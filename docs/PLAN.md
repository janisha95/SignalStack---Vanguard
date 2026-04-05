# Vanguard Super-User Config Proposal

## Summary
Move Vanguard to one canonical runtime config file, `config/vanguard_runtime.json`, but do it in a **shadow-first rollout** so current production behavior is not flipped until the new config proves parity. The goal is simple: you edit one JSON, restart the orchestrator, and the next run uses that config as the source of truth for universes, account rules, routing, risk defaults, execution, and Telegram.

## Proposed Config Shape
Use one readable top-level structure:
- `runtime` = orchestrator cadence, mode, shortlist sizing, force-assets override
- `market_data` = session windows and source routing by asset class
- `universes` = dynamic TTP equity filters, static FTMO universe, explicit GFT universe
- `risk_defaults` = shared/GFT risk knobs currently hardcoded in V6
- `profiles` = all active account profiles and static account rules
- `execution` = SignalStack/MetaApi config and symbol mapping
- `telegram` = bot/chat env refs and message-size defaults

Example skeleton:
```json
{
  "runtime": {
    "execution_mode": "manual",
    "cycle_interval_seconds": 300,
    "force_assets": [],
    "shortlist_top_n_per_asset_class": 30
  },
  "market_data": {
    "sessions": {
      "equity": {"timezone": "America/Toronto", "days": ["MON","TUE","WED","THU","FRI"], "open": "09:30", "close": "16:00"},
      "forex": {"timezone": "America/Toronto", "weekly_open": "SUN 17:00", "weekly_close": "FRI 17:00"},
      "crypto": {"mode": "24x7"}
    },
    "sources": {
      "equity": {"primary": ["ibkr", "alpaca"], "dynamic_universe_source": "ibkr"},
      "forex": {"primary": "ibkr", "fallback": "twelvedata"},
      "crypto": {"primary": "twelvedata"}
    }
  },
  "universes": {
    "ttp_equity": {
      "mode": "dynamic",
      "asset_class": "equity",
      "filters": {
        "min_price": 2.0,
        "min_avg_volume": 500000,
        "lookback_days": 10,
        "excluded_exchanges": ["OTC"],
        "excluded_suffixes": [".WS", ".WT", ".U", ".R", ".A", ".B", ".W"],
        "exclude_leveraged_etfs": true
      }
    },
    "gft_universe": {
      "mode": "static_explicit",
      "symbols": {
        "forex": ["EURUSD", "GBPUSD", "USDJPY"],
        "crypto": ["BTC/USD", "ETH/USD", "SOL/USD"],
        "metal": ["XAUUSD", "XAGUSD"],
        "index": ["SPY", "QQQ", "DIA"],
        "cfd_equity": ["TSLA", "NVDA", "MSFT"]
      }
    }
  },
  "risk_defaults": {
    "gft": {
      "max_positions": 3,
      "risk_per_trade_pct": 0.005,
      "daily_loss_limit_pct": 0.04,
      "max_portfolio_heat_pct": 0.02,
      "min_sl_pips": 20.0,
      "min_tp_pips": 40.0
    }
  },
  "profiles": [
    {
      "id": "gft_10k",
      "name": "GFT 10K Pay Later",
      "prop_firm": "gft",
      "account_type": "challenge",
      "environment": "live",
      "instrument_scope": "gft_universe",
      "holding_style": "intraday",
      "execution_bridge": "mt5",
      "account_size": 10000,
      "max_positions": 3,
      "risk_per_trade_pct": 0.005,
      "daily_loss_limit": 400,
      "max_drawdown": 800,
      "must_close_eod": false,
      "stop_atr_multiple": 1.5,
      "tp_atr_multiple": 3.0,
      "is_active": true
    }
  ],
  "execution": {
    "mode": "manual",
    "signalstack": {
      "webhook_url": "ENV:SIGNALSTACK_WEBHOOK_URL",
      "timeout_seconds": 5
    },
    "metaapi_symbol_map": {
      "BTC/USD": "BTCUSD.x",
      "TSLA": "TSLAx"
    }
  },
  "telegram": {
    "enabled": true,
    "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
    "chat_id": "ENV:TELEGRAM_CHAT_ID",
    "max_message_chars": 3500
  }
}
```

## Why This Should Not Break Runtime
Do **not** switch prod to this config in one jump. Use a 3-step rollout:
- **Step 1: Shadow load only**
  - Add a loader for `vanguard_runtime.json`
  - Load and validate it at startup
  - Log the resolved accounts/universes/risk values
  - Keep the current live runtime path unchanged
- **Step 2: Mirror + compare**
  - Sync JSON `profiles` into `account_profiles` in a dev/staging DB or a copied DB first
  - Compare the generated account rows, GFT universe symbols, and V6 risk knobs against the current DB/hardcoded values
  - Run one manual single-cycle and one short manual loop on the copied DB
- **Step 3: Flip one subsystem at a time**
  - First use `vanguard_runtime.json` for GFT universe + GFT risk defaults
  - Then use it for execution/Telegram defaults
  - Then sync account profiles from JSON into DB on startup
  - Only after parity, stop manually editing the old scattered JSON files

## Direct Answer on TTP vs GFT Universe
- **TTP Equity**: yes, keep it dynamic. JSON stores source + filters only, not 12k tickers.
- **GFT**: yes, explicit list in JSON. If a CFD equity or crypto symbol is not listed there, V6 should reject it for GFT. Adding symbols there should not break runtime as long as symbol format matches what V1/V3/V5 produce and what the MetaApi map expects.
- **FTMO**: can also live in this JSON as a static universe block.

## Main Risk
The highest-risk part is **profile sync from JSON into `account_profiles`**, because V6/orchestrator currently trust DB rows. That’s why I would shadow-load first and only make JSON authoritative after a parity pass on a copied DB.

## My Recommendation
Sleep on it, but if we do it, do **shadow config first**, not direct prod authority on day one. That gives you the single super-user JSON without gambling the current working loop.
