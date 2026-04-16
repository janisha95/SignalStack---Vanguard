"""
symbol_identity.py — Broker-neutral canonical symbol helpers.

Spec: CC_PHASE_2A_UNIVERSE_RESOLVER.md §1.5

Canonical form (internal everywhere):
  AAPL, MSFT       — equity
  EURUSD, GBPUSD   — forex (no slash, no suffix)
  BTCUSD, ETHUSD   — crypto (no slash, no suffix)

Broker-specific forms live ONLY at the execution boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class UnknownSymbolFormat(ValueError):
    """Raised by to_canonical() when the raw symbol cannot be normalized."""


class UnmappedSymbol(KeyError):
    """Raised by to_broker() when the canonical symbol has no mapping for the route."""


# ---------------------------------------------------------------------------
# Route config — driven by vanguard_runtime.json execution.routes
# (loaded lazily; falls back to hard-coded defaults if config not found)
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, dict[str, Any]] = {
    "metaapi_gft":    {"suffix_all": ".x"},
    "signalstack_ttp": {"suffix_all": ""},
    "alpaca_live":    {"suffix_all": ""},
}

_route_cfg: dict[str, dict[str, Any]] | None = None


def _get_route_cfg() -> dict[str, dict[str, Any]]:
    global _route_cfg
    if _route_cfg is not None:
        return _route_cfg
    try:
        cfg_path = Path(__file__).resolve().parents[2] / "config" / "vanguard_runtime.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text())
            routes = (
                data.get("execution", {}).get("routes")
                or {}
            )
            if routes:
                _route_cfg = {**_DEFAULTS, **routes}
                return _route_cfg
    except Exception:
        pass
    _route_cfg = dict(_DEFAULTS)
    return _route_cfg


# ---------------------------------------------------------------------------
# Source-ingest normalisation table
# ---------------------------------------------------------------------------

_SOURCE_RULES: dict[str, dict[str, Any]] = {
    "alpaca": {
        # Alpaca uses plain tickers: AAPL, MSFT, BTC/USD, etc.
        "slash_to_concat": True,
        "strip_suffix": None,
    },
    "twelve_data": {
        # TwelveData uses EUR/USD, BTC/USD
        "slash_to_concat": True,
        "strip_suffix": None,
    },
    "ibkr": {
        # IBKR uses EUR/USD
        "slash_to_concat": True,
        "strip_suffix": None,
    },
    "metaapi_gft": {
        # MetaAPI GFT uses EURUSD.x, AAPL.x
        "slash_to_concat": True,
        "strip_suffix": ".x",
    },
    "yfinance": {
        # yfinance uses BTC-USD, AAPL
        "slash_to_concat": False,
        "strip_suffix": None,
        "dash_to_concat": True,
    },
}


def to_canonical(source: str, raw_symbol: str) -> str:
    """
    Normalize a source-native symbol into canonical internal form.

    source: 'alpaca' | 'twelve_data' | 'ibkr' | 'metaapi_gft' | 'yfinance'

    Examples:
      to_canonical('alpaca', 'AAPL')         -> 'AAPL'
      to_canonical('twelve_data', 'BTC/USD') -> 'BTCUSD'
      to_canonical('ibkr', 'EUR/USD')        -> 'EURUSD'
      to_canonical('metaapi_gft', 'EURUSD.x')-> 'EURUSD'

    Raises UnknownSymbolFormat on unexpected input — never guesses.
    """
    src = str(source or "").strip().lower()
    sym = str(raw_symbol or "").strip().upper()

    if not sym:
        raise UnknownSymbolFormat(f"Empty symbol from source={source!r}")

    rules = _SOURCE_RULES.get(src)
    if rules is None:
        raise UnknownSymbolFormat(
            f"Unknown source {source!r}. Known sources: {sorted(_SOURCE_RULES)}"
        )

    # Strip broker suffix (e.g., ".x")
    suffix = rules.get("strip_suffix")
    if suffix and sym.endswith(suffix.upper()):
        sym = sym[: -len(suffix)]

    # Remove slash separator (EUR/USD → EURUSD, BTC/USD → BTCUSD)
    if rules.get("slash_to_concat") and "/" in sym:
        sym = sym.replace("/", "")

    # Remove dash separator (BTC-USD → BTCUSD) for yfinance
    if rules.get("dash_to_concat") and "-" in sym:
        parts = sym.split("-")
        if len(parts) == 2 and parts[1] in ("USD", "USDT", "BTC", "ETH"):
            sym = "".join(parts)

    if not sym:
        raise UnknownSymbolFormat(
            f"Symbol {raw_symbol!r} from {source!r} reduced to empty after normalization"
        )

    return sym


def to_broker(route: str, canonical: str) -> str:
    """
    Translate a canonical internal symbol to broker-native form.

    route: 'metaapi_gft' | 'signalstack_ttp' | 'alpaca_live'

    Examples:
      to_broker('metaapi_gft', 'EURUSD')  -> 'EURUSD.x'
      to_broker('metaapi_gft', 'AAPL')    -> 'AAPL.x'
      to_broker('metaapi_gft', 'BTCUSD')  -> 'BTCUSD.x'
      to_broker('signalstack_ttp', 'AAPL')-> 'AAPL'

    Raises UnmappedSymbol if canonical has no mapping for that route.
    """
    sym = str(canonical or "").strip().upper()
    if not sym:
        raise UnmappedSymbol(f"Empty canonical symbol for route={route!r}")

    cfg = _get_route_cfg()
    route_cfg = cfg.get(str(route or "").strip().lower())
    if route_cfg is None:
        raise UnmappedSymbol(
            f"Unknown route {route!r}. Known routes: {sorted(cfg)}"
        )

    suffix = route_cfg.get("suffix_all", "")
    return sym + suffix
