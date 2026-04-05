# CODEX — Switch V4A TBM Labels to ATR-Scaled Barriers

## READ FIRST

```bash
# 1. Find the TBM label generation code
grep -n "tp_pct\|sl_pct\|tp_long\|sl_long\|tp_short\|sl_short\|label_long\|label_short\|forward_scan\|barrier" ~/SS/Vanguard/stages/vanguard_training_backfill.py | head -30
grep -n "tp_pct\|sl_pct\|tp_long\|sl_long" ~/SS/Vanguard/stages/vanguard_training_backfill_fast.py 2>/dev/null | head -30

# 2. Find the TBM params config
cat ~/SS/Vanguard/config/vanguard_tbm_params.json 2>/dev/null

# 3. Check if ATR is already computed in training data
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_training_data)" | grep -i atr
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT AVG(atr_expansion), MIN(atr_expansion), MAX(atr_expansion) FROM vanguard_training_data WHERE asset_class='forex' LIMIT 1"

# 4. Check if raw ATR (not atr_expansion ratio) exists
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_bars_5m)" | grep -i atr
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT COUNT(*) FROM vanguard_bars_5m WHERE symbol='EUR/USD'" 

# 5. How does the backfill currently compute labels?
grep -n -A 20 "def.*label\|def.*tbm\|def.*barrier\|def.*forward" ~/SS/Vanguard/stages/vanguard_training_backfill.py | head -60
```

**Report ALL output before making changes.**

---

## WHAT TO CHANGE

The current V4A backfill uses FIXED percentage barriers for TBM labels:
```python
tp_pct = 0.0010  # forex: fixed 0.10%
sl_pct = 0.0005  # forex: fixed 0.05%
```

This is a known IC killer because forex volatility varies massively by session (London-NY overlap vs Asian session). A fixed 0.10% TP is nothing during London but aggressive during Tokyo.

**Replace with ATR-scaled barriers:**

```python
# NEW: Volatility-adjusted barriers
tp_mult = 2.0   # TP = 2 × ATR(14 bars on 5m)
sl_mult = 1.0   # SL = 1 × ATR(14 bars on 5m)
```

### Implementation

The backfill script reads 5m bars and walks forward to generate labels. Find where TP/SL prices are computed and change them:

**OLD pattern (find this):**
```python
tp_price = entry * (1 + tp_pct)
sl_price = entry * (1 - sl_pct)
```

**NEW pattern (replace with):**
```python
# Compute ATR(14) from the 5m bars at label time
# ATR should already be available from the bars or computed inline
atr_14 = compute_atr(highs[-14:], lows[-14:], closes[-14:])
tp_price = entry + (tp_mult * atr_14)
sl_price = entry - (sl_mult * atr_14)
```

If ATR is not directly available in the forward scan, compute it inline:

```python
import numpy as np

def compute_atr_14(highs, lows, closes, period=14):
    """Compute ATR from arrays of high, low, close."""
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    if len(tr) < period:
        return np.mean(tr) if len(tr) > 0 else 0.001  # fallback
    return np.mean(tr[-period:])
```

### Per-Asset Multipliers

Update the TBM params config (or inline):

```python
ATR_PARAMS = {
    "equity":    {"tp_mult": 2.0, "sl_mult": 1.0, "horizon_bars": 6},
    "forex":     {"tp_mult": 2.0, "sl_mult": 1.0, "horizon_bars": 12},
    "crypto":    {"tp_mult": 2.0, "sl_mult": 1.0, "horizon_bars": 6},
    "metal":     {"tp_mult": 2.0, "sl_mult": 1.0, "horizon_bars": 6},
    "commodity": {"tp_mult": 2.0, "sl_mult": 1.0, "horizon_bars": 6},
}
```

The 2:1 ratio stays the same (TP = 2×ATR, SL = 1×ATR). Only the scaling changes from fixed % to dynamic ATR.

### For SHORT labels

```python
# Short: TP when price drops, SL when price rises
tp_price_short = entry - (tp_mult * atr_14)
sl_price_short = entry + (sl_mult * atr_14)
```

---

## IMPORTANT: Only relabel forex first

Don't re-run the full 13.6M equity backfill. Just do forex:

```bash
cd ~/SS/Vanguard && python3 -u stages/vanguard_training_backfill.py --asset-class forex --relabel-only 2>&1 | tee logs/v4a_forex_atr_relabel.log
```

If `--relabel-only` doesn't exist, check if there's a way to recompute labels without re-downloading bars. The bars already exist in the DB — we just need to re-scan them with ATR barriers instead of fixed %.

If no incremental relabel exists, the backfill should still be fast for forex (282K rows, ~35 symbols).

---

## VERIFY

```bash
# Check label distribution changed
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class,
  COUNT(*) as total,
  SUM(label_long) as long_wins,
  ROUND(AVG(label_long), 3) as long_rate,
  SUM(label_short) as short_wins,
  ROUND(AVG(label_short), 3) as short_rate
FROM vanguard_training_data
WHERE asset_class = 'forex'
"

# Then retrain with purged CV
python3 -u stages/vanguard_model_trainer.py --asset-class forex 2>&1 | tee logs/v4b_forex_atr.log
grep "Mean IC" logs/v4b_forex_atr.log
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: ATR-scaled TBM barriers in V4A backfill (replaces fixed %)"
```
