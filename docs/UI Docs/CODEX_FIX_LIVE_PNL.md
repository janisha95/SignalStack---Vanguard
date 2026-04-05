# CODEX — Fix Live P&L: yfinance live_price returning null
# URGENT — Portfolio tab shows 33 positions but all Live prices are "—"
# The backend /api/v1/portfolio/live returns live_price: null for every position

---

## DIAGNOSE FIRST

```bash
# 1. Check what the portfolio endpoint returns
curl -s http://localhost:8090/api/v1/portfolio/live | python3 -c "
import sys, json; d=json.load(sys.stdin)
for p in d['positions'][:3]:
    print(f'{p[\"symbol\"]}: live={p[\"live_price\"]}, entry={p[\"entry_price\"]}')
print(f'Total positions: {d[\"summary\"][\"count\"]}')
"

# 2. Check if yfinance works at all on this machine
python3 -c "
import yfinance as yf
t = yf.Ticker('JPM')
try:
    price = t.fast_info['last_price']
    print(f'fast_info last_price: {price}')
except Exception as e:
    print(f'fast_info FAILED: {e}')
try:
    price2 = t.info.get('regularMarketPrice')
    print(f'info regularMarketPrice: {price2}')
except Exception as e:
    print(f'info FAILED: {e}')
try:
    hist = t.history(period='1d')
    if not hist.empty:
        print(f'history close: {hist[\"Close\"].iloc[-1]}')
    else:
        print('history EMPTY')
except Exception as e:
    print(f'history FAILED: {e}')
"

# 3. Check if Alpaca can provide prices (already have API keys)
python3 -c "
import os
key = os.environ.get('ALPACA_KEY') or os.environ.get('APCA_API_KEY_ID')
print(f'Alpaca key present: {bool(key)}')
"

# 4. Read the actual portfolio endpoint code
grep -n "live_price\|fast_info\|yfinance\|last_price\|Ticker" \
  ~/SS/Vanguard/vanguard/api/trade_desk.py \
  ~/SS/Vanguard/vanguard/api/unified_api.py 2>/dev/null
```

Report ALL output.

---

## THE FIX

The portfolio endpoint needs to fetch live prices. yfinance `fast_info`
may be failing silently. The fix should try multiple methods in order:

### Option A: Fix yfinance call (most likely the issue)

```python
import yfinance as yf

def get_live_price(symbol: str) -> float | None:
    """Get live price with multiple fallbacks."""
    try:
        t = yf.Ticker(symbol)
        # Method 1: fast_info (fastest)
        price = t.fast_info.get("last_price") or t.fast_info.get("lastPrice")
        if price and price > 0:
            return round(price, 2)
    except Exception:
        pass

    try:
        # Method 2: info dict
        price = t.info.get("regularMarketPrice") or t.info.get("currentPrice")
        if price and price > 0:
            return round(price, 2)
    except Exception:
        pass

    try:
        # Method 3: latest history bar
        hist = t.history(period="1d")
        if not hist.empty:
            return round(hist["Close"].iloc[-1], 2)
    except Exception:
        pass

    return None
```

### Option B: Batch fetch with yf.download (more efficient for 33 symbols)

```python
def get_live_prices_batch(symbols: list[str]) -> dict[str, float]:
    """Batch fetch live prices for all portfolio symbols."""
    import yfinance as yf

    if not symbols:
        return {}

    try:
        # Download last 1 day for all symbols at once
        data = yf.download(symbols, period="1d", group_by="ticker", progress=False)
        prices = {}
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    close = data["Close"].iloc[-1]
                else:
                    close = data[sym]["Close"].iloc[-1]
                if close and close > 0:
                    prices[sym] = round(float(close), 2)
            except (KeyError, IndexError):
                pass
        return prices
    except Exception as e:
        print(f"[portfolio] yf.download failed: {e}", flush=True)
        return {}
```

### Option C: Use Alpaca API (if yfinance is unreliable)

```python
def get_live_prices_alpaca(symbols: list[str]) -> dict[str, float]:
    """Fetch latest trade prices from Alpaca."""
    import requests, os

    key = os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET") or os.environ.get("APCA_API_SECRET_KEY")
    if not key:
        return {}

    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }

    prices = {}
    # Alpaca multi-snapshot endpoint
    try:
        sym_str = ",".join(symbols)
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={sym_str}&feed=iex",
            headers=headers,
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            for sym, snap in data.items():
                trade = snap.get("latestTrade", {})
                price = trade.get("p", 0)
                if price > 0:
                    prices[sym] = round(price, 2)
    except Exception as e:
        print(f"[portfolio] Alpaca snapshot failed: {e}", flush=True)

    return prices
```

### Apply the fix in the portfolio endpoint

File: wherever `GET /api/v1/portfolio/live` is defined (likely `trade_desk.py` or `unified_api.py`)

Replace the per-symbol yfinance call with the batch approach:

```python
@app.get("/api/v1/portfolio/live")
async def get_live_portfolio():
    # ... fetch open positions from execution_log ...

    # Get all unique symbols
    symbols = list(set(row["symbol"] for row in rows))

    # Batch fetch live prices (try yf.download first, Alpaca fallback)
    prices = get_live_prices_batch(symbols)
    if not prices:
        prices = get_live_prices_alpaca(symbols)

    # Build response
    positions = []
    total_pnl = 0
    for row in rows:
        sym = row["symbol"]
        live = prices.get(sym)
        entry = row["entry_price"] or 0
        shares = row["shares"] or 0
        direction = row["direction"]

        if live and entry:
            pnl_per = (live - entry) if direction == "LONG" else (entry - live)
            pnl_dollars = round(pnl_per * shares, 2)
            pnl_pct = round(pnl_per / entry * 100, 2)
        else:
            pnl_dollars = None
            pnl_pct = None

        if pnl_dollars:
            total_pnl += pnl_dollars

        positions.append({
            "id": row["id"],
            "symbol": sym,
            "direction": direction,
            "shares": shares,
            "entry_price": entry,
            "live_price": live,
            "unrealized_pnl": pnl_dollars,
            "unrealized_pct": pnl_pct,
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "tier": row.get("tier"),
            "tags": row.get("tags"),
            "executed_at": row.get("executed_at"),
            "account": row.get("account"),
        })

    return {
        "positions": positions,
        "summary": {
            "count": len(positions),
            "total_unrealized_pnl": round(total_pnl, 2),
            "winners": len([p for p in positions if (p["unrealized_pnl"] or 0) > 0]),
            "losers": len([p for p in positions if (p["unrealized_pnl"] or 0) < 0]),
        },
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
```

---

## VERIFY

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 3

# Check live prices
curl -s http://localhost:8090/api/v1/portfolio/live | python3 -c "
import sys, json; d=json.load(sys.stdin)
non_null = [p for p in d['positions'] if p['live_price'] is not None]
print(f'Positions with live price: {len(non_null)} / {d[\"summary\"][\"count\"]}')
print(f'Total P&L: \${d[\"summary\"][\"total_unrealized_pnl\"]}')
for p in non_null[:5]:
    print(f'  {p[\"symbol\"]}: entry=\${p[\"entry_price\"]} live=\${p[\"live_price\"]} pnl=\${p[\"unrealized_pnl\"]} ({p[\"unrealized_pct\"]}%)')
"
```

Expected: all 33 positions should have live prices (market is open).
P&L should be non-zero.
