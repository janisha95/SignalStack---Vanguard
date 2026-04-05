"""
mt5_data_adapter.py — MetaTrader5 data adapter for Vanguard.

Fetches 5m bars for FTMO instruments (forex, metals, indices, crypto).

NOTE: The MetaTrader5 Python package is Windows-only. On macOS/Linux this
module will load but all methods will return empty results with a clear log
message. It will be fully functional when running on a Windows machine with
MT5 terminal installed and running.

Usage:
    adapter = MT5DataAdapter()
    if adapter.available:
        adapter.connect()
        bars = adapter.get_bars_5m("EURUSD", start_dt, end_dt)

Location: ~/SS/Vanguard/vanguard/data_adapters/mt5_data_adapter.py
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform guard: MT5 package is Windows-only
# ---------------------------------------------------------------------------

try:
    import MetaTrader5 as _mt5
    _MT5_AVAILABLE = True
except ImportError:
    _mt5 = None  # type: ignore[assignment]
    _MT5_AVAILABLE = False
    logger.warning(
        "MetaTrader5 Python package not available on this platform "
        "(Windows-only). MT5 data adapter is in stub mode. "
        "Phase 5 (IBKR + MT5) requires a Windows environment."
    )

# ---------------------------------------------------------------------------
# Asset class mapping (symbol → Vanguard asset_class)
# ---------------------------------------------------------------------------

_ASSET_CLASS_MAP: dict[str, str] = {
    "EURUSD": "forex", "GBPUSD": "forex", "USDJPY": "forex",
    "USDCHF": "forex", "AUDUSD": "forex", "NZDUSD": "forex",
    "USDCAD": "forex", "EURGBP": "forex", "EURJPY": "forex",
    "XAUUSD": "metal", "XAGUSD": "metal", "XPTUSD": "metal",
    "BTCUSD": "crypto", "ETHUSD": "crypto", "LTCUSD": "crypto",
    "XRPUSD": "crypto", "BNBUSD": "crypto",
    "NAS100": "index", "US500": "index", "US30": "index",
    "GER40":  "index", "UK100": "index", "JPN225": "index",
}

_PREFIX_CLASS: list[tuple[str, str]] = [
    ("BTC", "crypto"), ("ETH", "crypto"), ("LTC", "crypto"),
    ("XRP", "crypto"), ("BNB", "crypto"),
    ("XAU", "metal"),  ("XAG", "metal"),  ("XPT", "metal"),
    ("NAS", "index"),  ("US5", "index"),  ("US3", "index"),
    ("GER", "index"),  ("UK1", "index"),  ("JPN", "index"),
    ("SPX", "index"),  ("DAX", "index"),
]


def _classify(symbol: str) -> str:
    sym = symbol.upper()
    # Strip broker suffixes for classification (.a, m, +, etc.)
    base = sym.rstrip(".").rstrip("+").rstrip("_")
    for sfx in (".A", "M", ".CASH"):
        if base.upper().endswith(sfx):
            base = base[: -len(sfx)]
    if base in _ASSET_CLASS_MAP:
        return _ASSET_CLASS_MAP[base]
    # Energy
    if any(base.startswith(p) for p in ("OIL", "USO", "UKO", "BRE", "WTI", "NGAS", "NATU")):
        return "energy"
    if sym in ("USOIL", "UKOIL", "NGAS"):
        return "energy"
    for prefix, cls in _PREFIX_CLASS:
        if base.startswith(prefix):
            return cls
    return "forex"


# ---------------------------------------------------------------------------
# MT5DataAdapter
# ---------------------------------------------------------------------------

class MT5DataAdapter:
    """
    MT5 market data adapter for FTMO instruments.
    Gracefully returns empty data on macOS/Linux (stub mode).
    """

    def __init__(self):
        self.available = _MT5_AVAILABLE
        self._connected = False
        if not self.available:
            logger.info(
                "MT5DataAdapter: running in stub mode (no MetaTrader5 package). "
                "Calls will return empty data."
            )

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialize the MT5 connection. Returns True if successful."""
        if not self.available:
            logger.debug("MT5 not available — skipping connect()")
            return False
        if _mt5.initialize():
            self._connected = True
            info = _mt5.terminal_info()
            logger.info(
                "MT5 connected: %s (build %s)",
                getattr(info, "name", "unknown"),
                getattr(info, "build", "?"),
            )
            return True
        err = _mt5.last_error()
        logger.warning(
            "MT5 terminal not running or initialize() failed: %s. "
            "Start MetaTrader5 terminal first.", err
        )
        return False

    def disconnect(self) -> None:
        if self.available and self._connected:
            _mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected")

    def ensure_connected(self) -> bool:
        if not self._connected:
            return self.connect()
        return True

    # ------------------------------------------------------------------
    # Symbol list
    # ------------------------------------------------------------------

    def get_symbols(self, group: str = "") -> list[str]:
        """Return list of visible MT5 symbols, optionally filtered by group."""
        if not self.available or not self.ensure_connected():
            return []
        raw = _mt5.symbols_get(group=group) if group else _mt5.symbols_get()
        if raw is None:
            return []
        return [s.name for s in raw if s.visible]

    # ------------------------------------------------------------------
    # 5-minute bars
    # ------------------------------------------------------------------

    def get_bars_5m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """
        Fetch 5m OHLCV bars for a single symbol between start and end.
        start / end are UTC tz-aware datetimes.
        Returns list of bar dicts: {time, open, high, low, close, tick_volume, ...}
        where 'time' is bar OPEN time as Unix timestamp.
        """
        if not self.available or not self.ensure_connected():
            return []
        # MT5 copy_rates_range expects naive UTC datetimes
        s = start.replace(tzinfo=None) if start.tzinfo else start
        e = end.replace(tzinfo=None)   if end.tzinfo   else end
        try:
            rates = _mt5.copy_rates_range(symbol.upper(), _mt5.TIMEFRAME_M5, s, e)
        except Exception as exc:
            logger.error("MT5 copy_rates_range(%s): %s", symbol, exc)
            return []
        if rates is None or len(rates) == 0:
            logger.debug("MT5: no 5m bars for %s (%s→%s)", symbol, start, end)
            return []
        return [
            {
                "time":        int(r["time"]),
                "open":        float(r["open"]),
                "high":        float(r["high"]),
                "low":         float(r["low"]),
                "close":       float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
                "spread":      int(r.get("spread", 0)),
                "real_volume": int(r.get("real_volume", 0)),
            }
            for r in rates
        ]

    def get_bars_5m_multiple(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """Fetch 5m bars for multiple symbols. Returns {symbol: [bars]}."""
        return {
            sym: bars
            for sym in symbols
            if (bars := self.get_bars_5m(sym, start, end))
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def asset_class(symbol: str) -> str:
        return _classify(symbol)

    def ping(self) -> bool:
        if not self.available:
            return False
        if not self._connected:
            return self.connect()
        info = _mt5.terminal_info()
        return info is not None and getattr(info, "connected", False)

    # ------------------------------------------------------------------
    # Symbol resolution (broker-specific naming variants)
    # ------------------------------------------------------------------

    def resolve_symbol(self, canonical: str) -> str | None:
        """
        Resolve a canonical symbol name to the broker's actual name.
        FTMO and other brokers may use variants like "EURUSD.a", "EURUSDm",
        "NAS100.cash", "XAUUSD.", etc.

        Tries the canonical name first, then common suffixes/variants.
        Returns the resolved name or None if not found.
        """
        if not self.available or not self.ensure_connected():
            return None
        variants = [
            canonical,
            canonical + ".a",
            canonical + "m",
            canonical + "+",
            canonical + ".",
            canonical + ".cash",
            canonical + "_",
            canonical.replace("100", "100.cash"),
            canonical.replace("500", "500.cash"),
            canonical.replace("30",  "30.cash"),
        ]
        for v in variants:
            info = _mt5.symbol_info(v)
            if info is not None:
                return v
        logger.warning("MT5: could not resolve symbol '%s' (tried %d variants)", canonical, len(variants))
        return None

    def resolve_symbols(self, canonicals: list[str]) -> dict[str, str | None]:
        """
        Resolve a list of canonical symbol names.
        Returns {canonical: resolved_name_or_None}
        """
        result = {}
        for sym in canonicals:
            resolved = self.resolve_symbol(sym)
            if resolved:
                logger.info("MT5 symbol resolved: %s → %s", sym, resolved)
            else:
                logger.warning("MT5 symbol NOT available: %s", sym)
            result[sym] = resolved
        return result

    def backfill_ftmo_priority(
        self,
        start: datetime,
        end: datetime,
        symbols: list[str] | None = None,
    ) -> dict[str, list[dict]]:
        """
        Download 5m bars for FTMO_PRIORITY instruments (or provided list).
        Resolves symbol names per broker and skips unavailable symbols.
        Returns {resolved_symbol: [bar_dicts]}
        """
        targets = symbols if symbols else FTMO_PRIORITY
        resolved = self.resolve_symbols(targets)
        available = {canon: res for canon, res in resolved.items() if res}

        print(f"\nMT5 symbol resolution ({len(targets)} requested):")
        for canon, res in sorted(resolved.items()):
            status = f"→ {res}" if res else "NOT AVAILABLE"
            print(f"  {canon:<12} {status}")

        logger.info(
            "MT5 backfill: %d/%d symbols available",
            len(available), len(targets),
        )

        result: dict[str, list[dict]] = {}
        for canon, resolved_sym in available.items():
            bars = self.get_bars_5m(resolved_sym, start, end)
            if bars:
                result[resolved_sym] = bars
                logger.info(
                    "MT5 %s: %d bars (%s→%s)",
                    resolved_sym, len(bars),
                    start.strftime("%Y-%m-%d"),
                    end.strftime("%Y-%m-%d"),
                )
        return result


# ---------------------------------------------------------------------------
# FTMO priority instrument list (canonical names)
# ---------------------------------------------------------------------------

FTMO_PRIORITY: list[str] = [
    # Forex majors
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
    # Indices
    "NAS100", "US500", "US30", "GER40",
    # Metals
    "XAUUSD", "XAGUSD",
    # Crypto
    "BTCUSD", "ETHUSD",
    # Energy
    "USOIL",
]

# Asset class overrides for FTMO symbols
_FTMO_ASSET_CLASS: dict[str, str] = {
    "USOIL": "energy",
    "UKOIL": "energy",
    "NGAS":  "energy",
}
