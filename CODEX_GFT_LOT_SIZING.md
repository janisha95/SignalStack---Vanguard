# CODEX — Fix Forex Lot Sizing in V6 for GFT Accounts (CRITICAL)

## The Bug
V6 calculates `shares_or_lots` using equity logic: `risk_dollars / price`.
For EURUSD at 1.154, this gives 831,527 "lots" — that's $831 BILLION.
Forex needs lot sizing: `risk_dollars / (pip_distance × pip_value_per_lot)`.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-lot-sizing-fix"
```

## Step 0: Find the sizing code
```bash
grep -n "shares_or_lots\|position_size\|lot_size\|calculate.*size\|risk.*dollars\|pip" \
  ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -20
```

## The Fix

Find the position sizing function and add forex lot sizing.

### Forex lot sizing formula:
```
pip_distance = abs(entry_price - stop_loss)
pip_value_per_standard_lot = 10.0  (for USD-denominated pairs)

For XXX/USD pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD):
  pip_value = $10 per standard lot per pip (0.0001)
  lot_size = risk_dollars / (pip_distance / 0.0001 * 10.0)

For XXX/JPY pairs (USDJPY, EURJPY, GBPJPY, AUDJPY, CHFJPY):
  pip_value ≈ $6.50-7.50 per standard lot per pip (0.01)
  lot_size = risk_dollars / (pip_distance / 0.01 * 7.0)

For cross pairs (EURGBP, EURCHF, EURCAD, EURAUD):
  pip_value ≈ $8-12 per standard lot per pip (0.0001)
  lot_size = risk_dollars / (pip_distance / 0.0001 * 10.0)
```

### Add this to the position sizing code:

```python
def calculate_forex_lots(risk_dollars: float, entry: float, stop_loss: float,
                         symbol: str, account_size: float) -> float:
    """
    Calculate forex lot size based on risk dollars and pip distance.
    
    GFT 5K: 0.5% risk = $25 per trade
    GFT 10K: 0.5% risk = $50 per trade
    """
    pip_distance = abs(entry - stop_loss)
    if pip_distance <= 0:
        return 0.01  # minimum lot
    
    # Determine pip size and value
    is_jpy = symbol.upper().endswith("JPY")
    if is_jpy:
        pip_size = 0.01
        pip_value_per_lot = 7.0  # approximate for JPY pairs
    else:
        pip_size = 0.0001
        pip_value_per_lot = 10.0  # standard for USD pairs
    
    pips = pip_distance / pip_size
    lot_size = risk_dollars / (pips * pip_value_per_lot)
    
    # Round to 2 decimal places (broker minimum)
    lot_size = round(lot_size, 2)
    
    # Safety caps for GFT
    lot_size = max(0.01, lot_size)       # minimum 0.01 (micro lot)
    lot_size = min(lot_size, 0.50)       # maximum 0.50 for safety
    
    # Additional cap based on account size
    max_lot_by_account = account_size / 10000  # 1 lot per $10K
    lot_size = min(lot_size, max_lot_by_account)
    
    return lot_size
```

### For equity CFDs (AAPL.x, NVDA.x, etc.):
```python
def calculate_cfd_shares(risk_dollars: float, entry: float, stop_loss: float) -> float:
    """
    Calculate CFD share count based on risk.
    GFT equity CFDs trade in fractional shares.
    """
    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        return 1  # minimum
    
    shares = risk_dollars / risk_per_share
    shares = round(shares, 2)  # fractional OK for CFDs
    
    # Safety caps
    shares = max(0.1, shares)
    shares = min(shares, 100)  # reasonable max for GFT
    
    return shares
```

### Wire into the main sizing function:

Find where `shares_or_lots` is computed (search for it):
```bash
grep -n "shares_or_lots" ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -10
```

Replace/patch the sizing logic:
```python
# Determine asset class and calculate accordingly
asset_class = candidate.get("asset_class", "equity")
account_size = float(profile.get("account_size", 10000))
risk_pct = float(profile.get("risk_per_trade_pct", 0.005))
risk_dollars = account_size * risk_pct  # 0.5% of account

entry = float(candidate.get("entry_price") or 0)
sl = float(candidate.get("stop_price") or 0)

if asset_class == "forex":
    candidate["shares_or_lots"] = calculate_forex_lots(
        risk_dollars, entry, sl, candidate["symbol"], account_size
    )
    candidate["risk_dollars"] = risk_dollars
elif asset_class == "equity":
    # For GFT equity CFDs
    if entry > 0 and sl > 0:
        risk_per_share = abs(entry - sl)
        if risk_per_share > 0:
            shares = risk_dollars / risk_per_share
            shares = round(min(max(shares, 0.1), 100), 2)
            candidate["shares_or_lots"] = shares
        else:
            candidate["shares_or_lots"] = 1
    candidate["risk_dollars"] = risk_dollars
else:
    # Default: use existing logic
    pass
```

### Also fix the risk_per_trade_pct for GFT accounts:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
UPDATE account_profiles SET risk_per_trade_pct = 0.005 WHERE id = 'gft_5k';
UPDATE account_profiles SET risk_per_trade_pct = 0.005 WHERE id = 'gft_10k';
"
```

## Verification

```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -20

# Check lot sizes are sane
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, symbol, direction, shares_or_lots, risk_dollars,
    entry_price, stop_price, tp_price
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
AND account_id LIKE 'gft%' AND status = 'APPROVED'
LIMIT 10
"

# Expected: forex lots between 0.01 and 0.50
# Expected: equity shares between 0.1 and 100
# NOT 831,527 or 1,373,074
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: forex lot sizing for GFT accounts (was calculating as equity shares)"
```
