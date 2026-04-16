# CC Prod Hotfix — Bug 6: classify_asset_class Misclassification

**Work directory:** `/Users/sjani008/SS/Vanguard/` (PROD — be surgical)  
**Context:** Bugs 1-5 are fixed. This is the remaining item from the hotfix report.

---

## BUG 6: classify_asset_class misclassifies forex crosses and crypto

**Symptom (from Bug 2 report):**
- 6-char USD pairs (BTCUSD, ETHUSD, SOLUSD, LTCUSD, BCHUSD, XRPUSD) are classified as `forex` instead of `crypto`
- Non-USD forex crosses (AUDCAD, GBPCAD, EURGBP, EURJPY, CHFJPY, AUDNZD, etc — 13 pairs) are classified as `equity` instead of `forex`
- The equity misclassification causes those 13 forex crosses to be CLOSED by V2's session gate (equity market is closed on weekends)
- Crypto works only by accident — ETHUSD/SOLUSD pass V2 as "forex" but V6 routes them correctly

**Root cause:** The `classify_asset_class` function (likely in `vanguard/helpers/` or `vanguard/data_adapters/` or `stages/vanguard_prefilter.py`) uses a naive heuristic based on symbol length or string patterns. It doesn't consult the runtime config's universe definition which already has the authoritative asset class for every symbol.

**Fix:** The runtime config's `universes.gft_universe.symbols` dict already maps every symbol to its asset class:

```json
{
  "equity": ["AAPL", "AMZN", ...],
  "forex": ["AUDCAD", "AUDCHF", ..., "USDJPY"],
  "crypto": ["BCHUSD", "BNBUSD", "BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD"],
  "index": ["AUS200", "GER40", ...],
  "commodity": ["BRENT", "WTI", "XAGUSD"]
}
```

The fix is to build a reverse lookup from this config and use it as the PRIMARY classifier:

```python
from vanguard.config.runtime_config import get_runtime_config

def _build_asset_class_map() -> dict[str, str]:
    """Build symbol → asset_class from runtime config universe."""
    cfg = get_runtime_config()
    universes = cfg.get("universes", {})
    mapping = {}
    for universe_def in universes.values():
        symbols_by_class = universe_def.get("symbols", {})
        for asset_class, symbol_list in symbols_by_class.items():
            for sym in symbol_list:
                mapping[sym.upper()] = asset_class
    return mapping

_ASSET_CLASS_MAP = None

def classify_asset_class(symbol: str) -> str:
    """Classify symbol using runtime config as authority, with heuristic fallback."""
    global _ASSET_CLASS_MAP
    if _ASSET_CLASS_MAP is None:
        _ASSET_CLASS_MAP = _build_asset_class_map()
    
    canonical = symbol.upper().replace("/", "").replace(".X", "").replace(".x", "")
    
    # Config is authoritative
    if canonical in _ASSET_CLASS_MAP:
        return _ASSET_CLASS_MAP[canonical]
    
    # Fallback heuristic for symbols not in config
    # (keep existing heuristic logic here for non-GFT universe symbols)
    ...
```

### Where to apply this fix:

1. **Find** `classify_asset_class` — search for it across the codebase:
   ```bash
   grep -rn "classify_asset_class\|asset_class_for\|get_asset_class" stages/ vanguard/ --include="*.py"
   ```

2. **Replace** the function body with the config-based lookup above, keeping the existing heuristic as a fallback for symbols not in any configured universe.

3. **Also check** if `vanguard_universe_members` table has an `asset_class` column that V2 reads. If V2 gets asset class from that table rather than the function, then the table needs to be populated with correct values too. Run:
   ```bash
   sqlite3 data/vanguard_universe.db "SELECT DISTINCT symbol, asset_class FROM vanguard_universe_members WHERE symbol IN ('BTCUSD','ETHUSD','AUDCAD','EURGBP','CHFJPY') LIMIT 10;"
   ```
   If the asset_class column is wrong there, update it:
   ```sql
   UPDATE vanguard_universe_members SET asset_class = 'crypto' WHERE symbol IN ('BTCUSD','ETHUSD','SOLUSD','LTCUSD','BCHUSD','XRPUSD','BNBUSD');
   UPDATE vanguard_universe_members SET asset_class = 'forex' WHERE symbol IN ('AUDCAD','AUDCHF','AUDJPY','AUDNZD','CADCHF','CADJPY','CHFJPY','EURAUD','EURCAD','EURCHF','EURGBP','EURJPY','EURNZD','GBPAUD','GBPCAD','GBPCHF','GBPJPY','GBPNZD','NZDCAD','NZDCHF');
   UPDATE vanguard_universe_members SET asset_class = 'index' WHERE symbol IN ('AUS200','GER40','JAP225','NAS100','SPX500','UK100','US30');
   UPDATE vanguard_universe_members SET asset_class = 'commodity' WHERE symbol IN ('BRENT','WTI','XAGUSD');
   ```

4. **Clear any cached asset class** — if there's a module-level cache of asset class lookups, invalidate it or make it rebuild on config reload.

---

## Verification Checklist

After fixing, restart the orchestrator and run one full cycle. Verify:

- [ ] V2 log shows `by_class={'forex': 27, 'crypto': 6}` (not `{'forex': 16}`)
- [ ] All 33 GFT universe symbols show ACTIVE in V2 (not 16 ACTIVE / 13 CLOSED)
- [ ] BTCUSD, ETHUSD etc appear in Telegram shortlist under CRYPTO section (not FOREX)
- [ ] AUDCAD, EURGBP, CHFJPY etc appear as FOREX (not EQUITY/CLOSED)
- [ ] V6 crypto rows use crypto sizing (not forex pip-based sizing)
- [ ] No "CLOSED" symbols that should be active during current session

## Report-back

Show:
1. V2 log line with full `by_class` breakdown
2. Telegram shortlist showing both FOREX and CRYPTO sections populated
3. `classify_asset_class` results for: BTCUSD, ETHUSD, AUDCAD, EURGBP, CHFJPY, AAPL, SPX500, XAGUSD
