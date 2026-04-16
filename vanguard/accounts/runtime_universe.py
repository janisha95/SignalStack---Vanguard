"""
runtime_universe.py — Resolve the active symbol universe from JSON profiles.

The resolver is profile-driven: only symbols from profiles with
is_active=true and currently-open asset sessions should enter V1/V2.

Phase 2a additions:
  - ResolvedUniverse dataclass
  - resolve_universe_for_cycle() — uses the Phase 2a config dict
  - _flatten_scope_symbols() updated to handle dict-by-asset-class format
  - _is_utc_session_open() — UTC-based session check
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from vanguard.config.runtime_config import get_market_data_config, get_profiles_config, get_universes_config
from vanguard.helpers.universe_builder import classify_asset_class

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# Phase 2a: ResolvedUniverse dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedUniverse:
    """Immutable snapshot of the resolved symbol universe for one cycle."""
    cycle_ts_utc: str
    active_profile_ids: list[str]
    expected_asset_classes: list[str]   # asset classes in session right now
    in_scope_symbols: list[str]          # final union across active profiles
    excluded: dict[str, str]             # {symbol: reason}
    mode: str                            # "observe" | "enforce"


def _is_profile_active(profile: dict[str, Any]) -> bool:
    return str(profile.get("is_active", 0)).strip().lower() in {"1", "true", "yes", "on"}


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    hour, minute = str(value).split(":", 1)
    return int(hour), int(minute)


def _is_session_open(
    asset_class: str,
    *,
    now_et: datetime,
    force_assets: set[str] | None = None,
) -> bool:
    normalized = str(asset_class or "").strip().lower()
    if not normalized:
        return False
    if force_assets and normalized in force_assets:
        return True

    session_cfg = (get_market_data_config().get("sessions") or {}).get(normalized) or {}
    days = {int(day) for day in (session_cfg.get("days") or [])}
    start_hm = _parse_hhmm(session_cfg.get("start"))
    end_hm = _parse_hhmm(session_cfg.get("end"))

    if start_hm is None or end_hm is None:
        return now_et.weekday() in days if days else True

    now_minutes = now_et.hour * 60 + now_et.minute
    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = end_hm[0] * 60 + end_hm[1]
    overnight = start_minutes >= end_minutes

    if not overnight:
        return (not days or now_et.weekday() in days) and start_minutes <= now_minutes < end_minutes

    if now_minutes >= start_minutes:
        return not days or now_et.weekday() in days
    previous_day = (now_et.weekday() - 1) % 7
    return not days or previous_day in days


def _flatten_scope_symbols(scope_cfg: dict[str, Any]) -> list[str]:
    """Flatten a scope config's symbols to a plain list.

    Handles three shapes:
      symbols: ["A", "B", ...]                  — flat list (legacy)
      symbols: {"equity": [...], "forex": [...]} — dict by asset class (Phase 2a)
      instruments: [...]                         — flat list (legacy FTMO/TopStep shape)
      instruments: {"equity_cfd": [...]}         — dict of lists (legacy)
    """
    symbols = scope_cfg.get("symbols")
    if isinstance(symbols, list):
        return [str(sym).upper().strip() for sym in symbols if str(sym).strip()]
    # Phase 2a: dict by asset class {"crypto": [...], "forex": [...], "equity": [...]}
    if isinstance(symbols, dict):
        flattened: list[str] = []
        for items in symbols.values():
            if not isinstance(items, list):
                continue
            for sym in items:
                s = str(sym).upper().strip()
                if s:
                    flattened.append(s)
        return flattened

    instruments = scope_cfg.get("instruments")
    if isinstance(instruments, list):
        return [str(sym).upper().strip() for sym in instruments if str(sym).strip()]
    if isinstance(instruments, dict):
        flattened = []
        for items in instruments.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    symbol = str(item.get("symbol") or "").upper().strip()
                    if symbol:
                        flattened.append(symbol)
                elif str(item).strip():
                    flattened.append(str(item).upper().strip())
        return flattened

    return []


def _flatten_scope_symbols_by_class(scope_cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Return {asset_class: [symbols]} from a Phase 2a dict-style scope, or {}."""
    symbols = scope_cfg.get("symbols")
    if isinstance(symbols, dict):
        return {
            ac: [str(s).upper().strip() for s in syms if str(s).strip()]
            for ac, syms in symbols.items()
            if isinstance(syms, list)
        }
    return {}


# ---------------------------------------------------------------------------
# Phase 2a: UTC-based session checker
# ---------------------------------------------------------------------------

def _is_utc_session_open(
    asset_class: str,
    cycle_ts: datetime,
    session_windows_utc: dict[str, Any],
) -> bool:
    """
    Check whether an asset class's session is open at cycle_ts (UTC).

    Supports:
      close: "HH:MM"       — intraday session
      close: "HH:MM_next"  — overnight session (e.g., forex 22:00-22:00)
    """
    window = session_windows_utc.get(str(asset_class).lower())
    if not window:
        return False

    days: set[int] = set(int(d) for d in (window.get("days") or []))
    open_str: str  = str(window.get("open", "00:00"))
    close_str: str = str(window.get("close", "23:59"))

    wd = cycle_ts.weekday()   # 0=Mon … 6=Sun
    t  = cycle_ts.hour * 60 + cycle_ts.minute

    def _parse_hm(s: str) -> int:
        base = s.replace("_next", "")
        h, m = base.split(":")
        return int(h) * 60 + int(m)

    open_min  = _parse_hm(open_str)
    close_min = _parse_hm(close_str)

    if close_str.endswith("_next"):
        # Overnight session: open from `open_min` on a session day and remain
        # open until `close_min` on the following session day. For forex this
        # needs to treat Sunday 22:00 UTC as the weekly reopen, not as the
        # "last active day" just because 6 > 4 numerically.
        next_day = (wd + 1) % 7
        prev_day = (wd - 1) % 7

        if wd in days and t >= open_min:
            # If tomorrow is not a session day, today's post-close period is shut.
            if next_day not in days:
                return False
            return True

        if wd in days and t < close_min and prev_day in days:
            return True

        return False
    else:
        # Intraday session
        return (wd in days) and (open_min <= t < close_min)


# ---------------------------------------------------------------------------
# Phase 2a: resolve_universe_for_cycle
# ---------------------------------------------------------------------------

def resolve_universe_for_cycle(
    config: dict[str, Any],
    cycle_ts_utc: datetime,
) -> "ResolvedUniverse":
    """
    Compute the resolved symbol universe for a single cycle.

    Steps:
      1. Filter profiles where is_active=True.
      2. For each active profile, look up its instrument_scope → universe block.
      3. For static universes: take symbols whose asset class is in session.
      4. For dynamic universes: call the dynamic source loader.
      5. Union all symbols, dedupe.
      6. Return ResolvedUniverse.
    """
    from vanguard.accounts.dynamic_universe_sources import load_alpaca_tradable_equity

    # Ensure cycle_ts_utc is UTC-aware
    if cycle_ts_utc.tzinfo is None:
        cycle_ts_utc = cycle_ts_utc.replace(tzinfo=timezone.utc)

    cycle_ts_str = cycle_ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    mode: str = config.get("runtime", {}).get("resolved_universe_mode", "observe")
    session_windows: dict[str, Any] = config.get("session_windows_utc") or {}
    universes: dict[str, Any] = config.get("universes") or {}
    profiles: list[dict[str, Any]] = config.get("profiles") or []

    in_scope: dict[str, str] = {}    # symbol → asset_class
    excluded: dict[str, str] = {}    # symbol → reason
    active_ids: list[str] = []
    open_asset_classes: set[str] = set()

    for profile in profiles:
        if not _is_profile_active(profile):
            continue
        pid   = str(profile.get("id") or "")
        scope = str(profile.get("instrument_scope") or "")
        if not pid or not scope:
            continue

        scope_cfg = dict(universes.get(scope) or {})
        scope_type = str(scope_cfg.get("type") or scope_cfg.get("mode") or "static")

        if not pid in active_ids:
            active_ids.append(pid)

        # Dynamic universe
        if "dynamic" in scope_type:
            filters = scope_cfg.get("filters") or scope_cfg.get("filter_rules") or {}
            source  = scope_cfg.get("source", "alpaca_tradable")
            if source == "alpaca_tradable":
                dyn_syms = load_alpaca_tradable_equity(filters)
            else:
                logger.warning("Unknown dynamic source %r, using empty list", source)
                dyn_syms = []
            for sym in dyn_syms:
                # Dynamic equity is always "equity" asset class
                ac = "equity"
                if _is_utc_session_open(ac, cycle_ts_utc, session_windows):
                    open_asset_classes.add(ac)
                    in_scope[sym] = ac
                else:
                    excluded[sym] = "SESSION_CLOSED"
            continue

        # Static universe: use dict-by-class structure when available
        symbols_by_class = _flatten_scope_symbols_by_class(scope_cfg)
        if symbols_by_class:
            for ac, syms in symbols_by_class.items():
                if _is_utc_session_open(ac, cycle_ts_utc, session_windows):
                    open_asset_classes.add(ac)
                    for sym in syms:
                        in_scope[sym] = ac
                else:
                    for sym in syms:
                        if sym not in in_scope:
                            excluded[sym] = "SESSION_CLOSED"
        else:
            # Fallback: flat list, classify each symbol
            flat = _flatten_scope_symbols(scope_cfg)
            for sym in flat:
                ac = classify_asset_class(sym)
                if _is_utc_session_open(ac, cycle_ts_utc, session_windows):
                    open_asset_classes.add(ac)
                    in_scope[sym] = ac
                else:
                    if sym not in in_scope:
                        excluded[sym] = "SESSION_CLOSED"

    return ResolvedUniverse(
        cycle_ts_utc=cycle_ts_str,
        active_profile_ids=sorted(active_ids),
        expected_asset_classes=sorted(open_asset_classes),
        in_scope_symbols=sorted(in_scope),
        excluded=excluded,
        mode=mode,
    )


def resolve_active_account_universe(
    *,
    now_et: datetime | None = None,
    force_assets: set[str] | None = None,
) -> dict[str, Any]:
    """
    Resolve symbols in scope for currently active profiles and open sessions.

    Returns a JSON-serializable snapshot used by V1/V2 and the UI.
    """
    now_et = now_et or datetime.now(_ET)
    universes = get_universes_config()
    profiles = [dict(p) for p in get_profiles_config() if _is_profile_active(dict(p))]

    symbols_by_asset: dict[str, set[str]] = {}
    active_profiles: list[dict[str, Any]] = []
    inactive_scopes: list[dict[str, Any]] = []
    dynamic_scopes: list[dict[str, Any]] = []

    for profile in profiles:
        profile_id = str(profile.get("id") or "").strip()
        scope_name = str(profile.get("instrument_scope") or "").strip()
        if not profile_id or not scope_name:
            continue

        scope_cfg = dict(universes.get(scope_name) or {})
        scope_symbols = _flatten_scope_symbols(scope_cfg)
        active_profiles.append(
            {
                "id": profile_id,
                "name": profile.get("name"),
                "prop_firm": profile.get("prop_firm"),
                "instrument_scope": scope_name,
                "policy_id": profile.get("policy_id"),
                "symbol_count": len(scope_symbols),
                "dynamic": bool(scope_cfg.get("dynamic") or scope_cfg.get("mode") == "dynamic"),
            }
        )

        if scope_cfg.get("dynamic") or scope_cfg.get("mode") == "dynamic":
            dynamic_scopes.append(
                {
                    "profile_id": profile_id,
                    "instrument_scope": scope_name,
                    "asset_class": scope_cfg.get("asset_class", "equity"),
                    "source": scope_cfg.get("source"),
                    "filter_rules": scope_cfg.get("filter_rules") or scope_cfg.get("filters") or {},
                }
            )

        for symbol in scope_symbols:
            asset_class = classify_asset_class(symbol)
            if not _is_session_open(asset_class, now_et=now_et, force_assets=force_assets):
                inactive_scopes.append(
                    {
                        "profile_id": profile_id,
                        "instrument_scope": scope_name,
                        "symbol": symbol,
                        "asset_class": asset_class,
                        "reason": "SESSION_CLOSED",
                    }
                )
                continue
            symbols_by_asset.setdefault(asset_class, set()).add(symbol)

    sorted_symbols_by_asset = {
        asset_class: sorted(symbols)
        for asset_class, symbols in sorted(symbols_by_asset.items())
    }
    symbols = sorted({symbol for group in sorted_symbols_by_asset.values() for symbol in group})
    open_asset_classes = sorted(sorted_symbols_by_asset.keys())

    return {
        "as_of_et": now_et.isoformat(),
        "active_profiles": active_profiles,
        "open_asset_classes": open_asset_classes,
        "symbols_by_asset_class": sorted_symbols_by_asset,
        "symbols": symbols,
        "symbol_count": len(symbols),
        "dynamic_scopes": dynamic_scopes,
        "inactive_scope_symbols": inactive_scopes,
    }
