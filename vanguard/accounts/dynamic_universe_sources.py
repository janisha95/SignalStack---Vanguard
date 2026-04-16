"""
dynamic_universe_sources.py — Dynamic universe source loaders.

Spec: CC_PHASE_2A_UNIVERSE_RESOLVER.md §2.4
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Stub large-cap list returned when Alpaca is not configured.
_STUB_LARGE_CAP: list[str] = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK.B",
    "UNH", "LLY", "JPM", "V", "XOM", "AVGO", "PG", "MA", "HD", "COST",
    "MRK", "ABBV", "CVX", "KO", "AMD", "PEP", "ADBE", "WMT", "CRM",
    "ACN", "MCD", "NFLX", "BAC", "INTC", "LIN", "TMO", "NKE", "DIS",
    "IBM", "CSCO", "ORCL", "QCOM", "AMGN", "TXN", "HON", "PM", "UPS",
    "AMAT", "DE", "CAT", "SBUX", "GS",
]


def load_alpaca_tradable_equity(filters: dict) -> list[str]:
    """
    Return tradable US equities that meet the filter criteria.

    If Alpaca is not configured, returns a stub list of 50 large-cap tickers
    and logs a warning. Never returns an empty list silently.
    """
    try:
        from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter
        from vanguard.config.runtime_config import get_runtime_config

        market_cfg = get_runtime_config().get("market_data", {})
        sources_cfg = market_cfg.get("sources", {})
        if "alpaca" not in sources_cfg.get("equity", []):
            raise RuntimeError("Alpaca not in equity sources")

        adapter = AlpacaAdapter()
        min_price = float(filters.get("min_price", 5.0))
        min_vol = float(filters.get("min_avg_dollar_vol_30d", 5_000_000))
        symbols = adapter.get_tradable_symbols(
            min_price=min_price, min_avg_dollar_volume=min_vol
        )
        if not symbols:
            raise RuntimeError("Alpaca returned empty symbol list")
        logger.info("Alpaca dynamic universe: %d symbols", len(symbols))
        return [str(s).upper() for s in symbols]

    except Exception as exc:
        logger.warning(
            "load_alpaca_tradable_equity: Alpaca unavailable (%s) — "
            "returning %d-symbol stub large-cap list",
            exc,
            len(_STUB_LARGE_CAP),
        )
        return list(_STUB_LARGE_CAP)
