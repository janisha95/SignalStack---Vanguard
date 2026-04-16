"""
vanguard_prefilter.py — V2 Vanguard Live Health Monitor.

Reads all symbols from vanguard_bars_5m, applies 5 health checks, and writes
per-symbol status to the vanguard_health table. Produces a list of ACTIVE
survivors for the V3 Factor Engine.

Health checks (first failure determines status):
  1. Staleness   — last bar within 10 min (market hours only)
  2. Volume      — relative_volume >= 0.3
  3. Spread      — (high - low) / close < 0.05
  4. Warm-up     — at least 50 bars in DB
  5. Session     — has a bar since today's session open (market hours only)

Statuses: ACTIVE, STALE, LOW_VOLUME, WIDE_SPREAD, WARM_UP, HALTED

CLI:
  python3 stages/vanguard_prefilter.py                    # all symbols
  python3 stages/vanguard_prefilter.py --symbols AAPL,SPY
  python3 stages/vanguard_prefilter.py --validate
  python3 stages/vanguard_prefilter.py --dry-run
  python3 stages/vanguard_prefilter.py --debug AAPL

Location: ~/SS/Vanguard/stages/vanguard_prefilter.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.config.runtime_config import (
    data_source_label_for_source_id,
    get_runtime_config,
    get_shadow_db_path,
    is_replay_from_source_db,
    resolve_market_data_source_id,
)
from vanguard.helpers.db import VanguardDB
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.helpers import universe_builder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_prefilter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = get_shadow_db_path()

STATUS_ACTIVE       = "ACTIVE"
STATUS_STALE        = "STALE"
STATUS_LOW_VOLUME   = "LOW_VOLUME"
STATUS_WIDE_SPREAD  = "WIDE_SPREAD"
STATUS_WARM_UP      = "WARM_UP"
STATUS_HALTED       = "HALTED"
STATUS_CLOSED       = "CLOSED"

ALL_STATUSES = (STATUS_ACTIVE, STATUS_STALE, STATUS_LOW_VOLUME,
                STATUS_WIDE_SPREAD, STATUS_WARM_UP, STATUS_HALTED, STATUS_CLOSED)

# ---------------------------------------------------------------------------
# Thresholds — defaults (equity); universe_builder provides per-universe values
# ---------------------------------------------------------------------------

# Fallback defaults if config is not available
_DEFAULT_THRESHOLDS = {
    "max_stale_minutes":  10.0,
    "min_rel_vol":         0.3,
    "max_spread_pct":      0.05,
    "min_bars_warmup":     50,
    "session_avg_window":  20,
}

# Module-level aliases retained for backwards compatibility and tests
MIN_BARS_WARMUP    = _DEFAULT_THRESHOLDS["min_bars_warmup"]
MAX_STALE_MINUTES  = _DEFAULT_THRESHOLDS["max_stale_minutes"]
MIN_REL_VOL        = _DEFAULT_THRESHOLDS["min_rel_vol"]
MAX_SPREAD_PCT     = _DEFAULT_THRESHOLDS["max_spread_pct"]
SESSION_AVG_WINDOW = _DEFAULT_THRESHOLDS["session_avg_window"]
BARS_TO_FETCH      = 200      # max bars fetched per symbol
BARS_TO_FETCH_1M   = 600      # enough for 120 synthetic 5m buckets

SESSION_DEFINITIONS = {
    "equity": {"start": "09:30", "end": "16:00", "tz": "America/New_York", "days": "Mon-Fri"},
    "forex": {"start": "17:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "crypto": {"start": None, "end": None, "tz": "UTC", "days": "Mon-Sun"},
    "index": {"start": "09:30", "end": "16:00", "tz": "America/New_York", "days": "Mon-Fri"},
    "metal": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "energy": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "commodity": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "agriculture": {"start": "19:00", "end": "13:20", "tz": "America/New_York", "days": "Mon-Fri"},
    "futures": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
}

STALE_THRESHOLDS_MINUTES = {
    "equity": 10.0,
    "forex": 5.0,
    "index": 5.0,
    "metal": 5.0,
    "energy": 5.0,
    "commodity": 5.0,
    "agriculture": 5.0,
    "futures": 5.0,
    "crypto": 15.0,
}


def _load_v2_prefilter_config() -> dict:
    cfg = get_runtime_config()
    return dict(cfg.get("v2_prefilter") or {})


def get_thresholds(universe_name: str = "ttp_equity") -> dict:
    """
    Return prefilter thresholds for the given universe.
    Falls back to equity defaults if universe is unknown.
    """
    try:
        thresholds = universe_builder.get_health_thresholds(universe_name)
    except Exception:
        thresholds = dict(_DEFAULT_THRESHOLDS)
    runtime_defaults = dict((_load_v2_prefilter_config().get("defaults") or {}))
    return {**_DEFAULT_THRESHOLDS, **thresholds, **runtime_defaults}


def _thresholds_for_asset_class(asset_class: str, base_thresholds: dict) -> dict:
    resolved = dict(base_thresholds or _DEFAULT_THRESHOLDS)
    prefilter_cfg = _load_v2_prefilter_config()
    asset_cfg = dict(((prefilter_cfg.get("asset_thresholds") or {}).get(asset_class) or {}))
    resolved.update(asset_cfg)

    if asset_class == "crypto":
        crypto_validation_cfg = prefilter_cfg.get("crypto_validation") or {}
        if bool(crypto_validation_cfg.get("enabled", False)):
            resolved.update(dict(crypto_validation_cfg.get("thresholds") or {}))
    return resolved


# ---------------------------------------------------------------------------
# UTC parse helper
# ---------------------------------------------------------------------------

def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _format_et(ts: str) -> str:
    return _parse_utc(ts).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _day_set(days: str) -> set[int]:
    mapping = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    if days == "Mon-Sun":
        return set(range(7))
    if days == "Mon-Fri":
        return {0, 1, 2, 3, 4}
    if days == "Sun-Fri":
        return {6, 0, 1, 2, 3, 4}
    return {mapping[token] for token in days.split(",") if token in mapping}


def _session_definition(
    asset_class: str,
    session_start: str | None = None,
    session_end: str | None = None,
    session_tz: str | None = None,
) -> dict:
    base = dict(SESSION_DEFINITIONS.get(asset_class, SESSION_DEFINITIONS["equity"]))
    if session_start is not None:
        base["start"] = session_start
    if session_end is not None:
        base["end"] = session_end
    if session_tz is not None:
        base["tz"] = session_tz
    return base


def is_session_active(
    asset_class: str,
    now: datetime,
    session_start: str | None = None,
    session_end: str | None = None,
    session_tz: str | None = None,
) -> bool:
    """Return True when the asset class session is currently active."""
    session = _session_definition(asset_class, session_start, session_end, session_tz)
    if session["start"] is None or session["end"] is None:
        return True

    tz = ZoneInfo(session["tz"])
    local_now = now.astimezone(tz)
    allowed_days = _day_set(session["days"])
    start_hm = _parse_hhmm(session["start"])
    end_hm = _parse_hhmm(session["end"])
    if start_hm is None or end_hm is None:
        return True

    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = end_hm[0] * 60 + end_hm[1]
    now_minutes = local_now.hour * 60 + local_now.minute
    overnight = start_minutes >= end_minutes

    if not overnight:
        return local_now.weekday() in allowed_days and start_minutes <= now_minutes < end_minutes

    if now_minutes >= start_minutes:
        return local_now.weekday() in allowed_days

    previous_day = (local_now.weekday() - 1) % 7
    return local_now.weekday() in allowed_days and previous_day in allowed_days


def session_start_utc(
    asset_class: str,
    now: datetime,
    session_start: str | None = None,
    session_end: str | None = None,
    session_tz: str | None = None,
) -> datetime | None:
    """Return the current session start in UTC for the asset class."""
    session = _session_definition(asset_class, session_start, session_end, session_tz)
    if session["start"] is None:
        return now - timedelta(hours=24)

    tz = ZoneInfo(session["tz"])
    local_now = now.astimezone(tz)
    start_hm = _parse_hhmm(session["start"])
    end_hm = _parse_hhmm(session["end"])
    if start_hm is None or end_hm is None:
        return now - timedelta(hours=24)

    start_minutes = start_hm[0] * 60 + start_hm[1]
    end_minutes = end_hm[0] * 60 + end_hm[1]
    now_minutes = local_now.hour * 60 + local_now.minute
    overnight = start_minutes >= end_minutes

    session_date = local_now.date()
    if overnight and now_minutes < end_minutes:
        session_date = (local_now - timedelta(days=1)).date()

    session_start_local = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        start_hm[0],
        start_hm[1],
        tzinfo=tz,
    )
    return session_start_local.astimezone(timezone.utc)


def _latest_bar_time(*bar_sets: list[dict]) -> datetime | None:
    latest: datetime | None = None
    for bars in bar_sets:
        if not bars:
            continue
        candidate = _parse_utc(bars[-1]["bar_ts_utc"])
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _freshest_bar(*bar_sets: list[dict]) -> dict | None:
    freshest: dict | None = None
    freshest_ts: datetime | None = None
    for bars in bar_sets:
        if not bars:
            continue
        candidate = bars[-1]
        candidate_ts = _parse_utc(candidate["bar_ts_utc"])
        if freshest_ts is None or candidate_ts > freshest_ts:
            freshest = candidate
            freshest_ts = candidate_ts
    return freshest


def _bucket_counts_from_1m(bars_1m: list[dict]) -> list[int]:
    if not bars_1m:
        return []
    buckets: dict[str, int] = {}
    for bar in bars_1m:
        bar_end = _parse_utc(bar["bar_ts_utc"])
        bar_open = bar_end - timedelta(minutes=1)
        bucket_minute = (bar_open.minute // 5) * 5
        bucket_start = bar_open.replace(minute=bucket_minute, second=0, microsecond=0)
        bucket_end = (bucket_start + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        buckets[bucket_end] = buckets.get(bucket_end, 0) + 1
    return [count for _, count in sorted(buckets.items())]


def get_v2_universe(
    db: VanguardDB,
    symbols: list[str] | None = None,
) -> list[dict]:
    """Get active symbols from the canonical universe bus.

    When explicit symbols are provided (e.g. from Phase 2a resolved universe),
    symbols not found in vanguard_universe_members receive a synthetic fallback
    row so that Phase 2a symbols are never silently dropped.
    """
    with db.connect() as conn:
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            rows = conn.execute(
                f"""
                SELECT symbol, asset_class, data_source, session_start, session_end, session_tz
                FROM vanguard_universe_members
                WHERE is_active = 1 AND symbol IN ({placeholders})
                ORDER BY asset_class, symbol
                """,
                [s.upper() for s in symbols],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT symbol, asset_class, data_source, session_start, session_end, session_tz
                FROM vanguard_universe_members
                WHERE is_active = 1
                ORDER BY asset_class, symbol
                """
            ).fetchall()

    result = [dict(r) for r in rows]

    # When an explicit symbol list is given, synthesize fallback rows for any
    # symbols not found in vanguard_universe_members so Phase 2a symbols are
    # never silently dropped.
    if symbols:
        from vanguard.helpers.universe_builder import classify_asset_class
        found = {row["symbol"].upper() for row in result}
        for sym in symbols:
            s = sym.upper()
            if s not in found:
                ac = classify_asset_class(s)
                result.append({
                    "symbol":       s,
                    "asset_class":  ac,
                    "data_source":  None,
                    "session_start": None,
                    "session_end":  None,
                    "session_tz":   None,
                })
                logger.debug("get_v2_universe: synthetic fallback row for %s (asset_class=%s)", s, ac)
        result.sort(key=lambda r: (r.get("asset_class") or "", r["symbol"]))

    return result


# ---------------------------------------------------------------------------
# Core health check
# ---------------------------------------------------------------------------

def check_symbol(
    symbol_row: dict,
    bars: list[dict],
    bars_1m: list[dict],
    now: datetime,
    thresholds: dict | None = None,
) -> dict:
    """
    Apply 5 health checks to a symbol's bars.

    Parameters
    ----------
    symbol        : ticker
    bars          : list of bar dicts in chronological order (oldest first)
    now           : current UTC datetime (tz-aware)
    market_is_open: True if equity market is currently open
    thresholds    : per-universe threshold dict (from get_thresholds()).
                    If None, equity defaults are used.

    Returns
    -------
    Health result dict matching vanguard_health schema.
    """
    t = _thresholds_for_asset_class(
        symbol_row.get("asset_class") or "equity",
        thresholds or _DEFAULT_THRESHOLDS,
    )

    max_stale_minutes = t.get("max_stale_minutes", MAX_STALE_MINUTES)
    min_rel_vol       = t.get("min_rel_vol",       MIN_REL_VOL)
    max_spread_pct    = t.get("max_spread_pct",    MAX_SPREAD_PCT)
    min_bars_warmup   = t.get("min_bars_warmup",   MIN_BARS_WARMUP)
    session_avg_win   = t.get("session_avg_window", SESSION_AVG_WINDOW)

    symbol = symbol_row["symbol"]
    asset_class = symbol_row.get("asset_class") or "equity"
    data_source = symbol_row.get("data_source")
    # IBKR forex streaming seeds recent history, but the intraday 1m->5m aggregator
    # only materializes a short rolling window. A 12-bar 5m warmup is enough for V3.
    session_active = is_session_active(
        asset_class,
        now,
        session_start=symbol_row.get("session_start"),
        session_end=symbol_row.get("session_end"),
        session_tz=symbol_row.get("session_tz"),
    )

    last_bar = _freshest_bar(bars, bars_1m)
    last_bar_ts = _latest_bar_time(bars, bars_1m)

    base: dict = {
        "symbol":           symbol,
        "asset_class":      asset_class,
        "data_source":      data_source,
        "session_active":   1 if session_active else 0,
        "last_bar_age_seconds": None,
        "relative_volume":  None,
        "spread_bps":       None,
        "bars_available":   len(bars),
        "last_bar_ts_utc":  last_bar_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if last_bar_ts else None,
    }

    bucket_counts_1m = _bucket_counts_from_1m(bars_1m)
    synthetic_5m_bars = len(bucket_counts_1m)
    if synthetic_5m_bars > base["bars_available"]:
        base["bars_available"] = synthetic_5m_bars

    # No data at all during an active session is stale data flow, not an exchange halt.
    if not bars and not bars_1m:
        return {**base, "status": STATUS_STALE if session_active else STATUS_CLOSED}

    if last_bar_ts is None or last_bar is None:
        return {**base, "status": STATUS_STALE if session_active else STATUS_CLOSED}
    age_seconds = int((now - last_bar_ts).total_seconds())
    base["last_bar_age_seconds"] = age_seconds

    if not session_active:
        return {**base, "status": STATUS_CLOSED}

    # ------------------------------------------------------------------
    # Check 1: Staleness (asset-class aware)
    # ------------------------------------------------------------------
    max_stale_minutes = STALE_THRESHOLDS_MINUTES.get(asset_class, max_stale_minutes)
    age_min = age_seconds / 60.0
    if age_min > max_stale_minutes:
        return {**base, "status": STATUS_STALE}

    # ------------------------------------------------------------------
    # Check 2: Relative volume
    # ------------------------------------------------------------------
    window = bars[-session_avg_win:]
    vols = [float(b["volume"]) for b in window if b.get("volume") is not None]
    positive_vols = [v for v in vols if v > 0]
    rel_vol = None

    if positive_vols:
        avg_vol = sum(positive_vols) / len(positive_vols)
        current_vol = float(last_bar.get("volume") or 0)
        rel_vol = current_vol / avg_vol if avg_vol > 0 else 0.0
    elif data_source == "twelvedata" and bucket_counts_1m:
        proxy_window = bucket_counts_1m[-session_avg_win:]
        avg_proxy = sum(proxy_window) / len(proxy_window) if proxy_window else 0.0
        current_proxy = proxy_window[-1] if proxy_window else 0.0
        rel_vol = current_proxy / avg_proxy if avg_proxy > 0 else 0.0

    base["relative_volume"] = round(rel_vol, 4) if rel_vol is not None else None

    if rel_vol is not None and rel_vol < min_rel_vol:
        return {**base, "status": STATUS_LOW_VOLUME}

    # ------------------------------------------------------------------
    # Check 3: Spread  (high - low) / close
    # ------------------------------------------------------------------
    h  = float(last_bar.get("high")  or 0)
    lo = float(last_bar.get("low")   or 0)
    c  = float(last_bar.get("close") or 0)
    spread_pct = (h - lo) / c if c > 0 else 0.0

    base["spread_bps"] = round(spread_pct * 10_000, 2)

    if spread_pct >= max_spread_pct:
        return {**base, "status": STATUS_WIDE_SPREAD}

    # ------------------------------------------------------------------
    # Check 4: Warm-up (enough bars for rolling features)
    # ------------------------------------------------------------------
    if base["bars_available"] < min_bars_warmup:
        return {**base, "status": STATUS_WARM_UP}

    # ------------------------------------------------------------------
    # Check 5: Session check (has a bar since this asset's session start)
    # ------------------------------------------------------------------
    session_open = session_start_utc(
        asset_class,
        now,
        session_start=symbol_row.get("session_start"),
        session_end=symbol_row.get("session_end"),
        session_tz=symbol_row.get("session_tz"),
    )
    if session_open is not None:
        session_bars = list(bars or []) + list(bars_1m or [])
        has_session_bar = any(_parse_utc(b["bar_ts_utc"]) >= session_open for b in session_bars)
        if not has_session_bar:
            return {**base, "status": STATUS_STALE}

    return {**base, "status": STATUS_ACTIVE}


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(
    symbols: list[str] | None = None,
    dry_run: bool = False,
    debug_symbol: str | None = None,
    universe_name: str = "ttp_equity",
    cycle_ts: str | None = None,
    in_scope_symbols: list[str] | None = None,
) -> list[dict]:
    """
    Run health checks for all (or specified) symbols.

    Parameters
    ----------
    symbols          : list of tickers to check; None = all symbols in DB
    dry_run          : if True, compute but do not write to DB
    debug_symbol     : if set, print detailed trace for this symbol
    universe_name    : universe key for per-universe thresholds
    in_scope_symbols : Phase 2a enforce-mode filter. When provided, only
                       symbols in this list are processed. If both symbols and
                       in_scope_symbols are given, the intersection is used.

    Returns
    -------
    List of health result dicts.
    """
    # Phase 2a: resolve effective symbol list
    if in_scope_symbols is not None:
        scope_set = {s.upper() for s in in_scope_symbols}
        if symbols is not None:
            symbols = [s for s in symbols if s.upper() in scope_set]
        else:
            symbols = [s.upper() for s in in_scope_symbols]
    db      = VanguardDB(DB_PATH)
    now = now_utc()
    if is_replay_from_source_db():
        replay_now = db.latest_bar_ts_utc("vanguard_bars_5m") or db.latest_bar_ts_utc("vanguard_bars_1m")
        if replay_now:
            now = _parse_utc(replay_now)
            cycle_ts = cycle_ts or replay_now
    cycle_ts = cycle_ts or iso_utc(now)

    # Load per-universe thresholds from config
    thresholds = get_thresholds(universe_name)

    logger.info(
        "V2 Prefilter | cycle=%s | universe=%s | dry_run=%s",
        cycle_ts, universe_name, dry_run,
    )

    # Symbol list
    if symbols:
        targets = get_v2_universe(db, symbols=[s.upper() for s in symbols])
        logger.info("Checking %d specified symbols", len(targets))
    else:
        targets = get_v2_universe(db)
        logger.info("Checking all %d active universe members", len(targets))

    results: list[dict] = []
    conn = db.connect()

    try:
        for sym_row in targets:
            sym = sym_row["symbol"]
            asset_class = str(sym_row.get("asset_class") or "").lower()
            source_id = resolve_market_data_source_id(asset_class) if asset_class else ""
            preferred_data_source = (
                data_source_label_for_source_id(source_id) if source_id else sym_row.get("data_source")
            )
            effective_sym_row = dict(sym_row)
            if preferred_data_source:
                effective_sym_row["data_source"] = preferred_data_source
            bars_rows = conn.execute(
                "SELECT bar_ts_utc, open, high, low, close, volume "
                "FROM vanguard_bars_5m "
                "WHERE symbol = ? "
                + ("AND data_source = ? " if preferred_data_source else "")
                + "ORDER BY bar_ts_utc DESC "
                "LIMIT ?",
                ((sym, preferred_data_source, BARS_TO_FETCH) if preferred_data_source else (sym, BARS_TO_FETCH)),
            ).fetchall()
            bars = [dict(r) for r in reversed(bars_rows)]
            bars_1m_rows = conn.execute(
                "SELECT bar_ts_utc, open, high, low, close, volume, tick_volume "
                "FROM vanguard_bars_1m "
                "WHERE symbol = ? "
                + ("AND data_source = ? " if preferred_data_source else "")
                + "ORDER BY bar_ts_utc DESC "
                "LIMIT ?",
                ((sym, preferred_data_source, BARS_TO_FETCH_1M) if preferred_data_source else (sym, BARS_TO_FETCH_1M)),
            ).fetchall()
            bars_1m = [dict(r) for r in reversed(bars_1m_rows)]

            result = check_symbol(effective_sym_row, bars, bars_1m, now, thresholds=thresholds)
            result["cycle_ts_utc"] = cycle_ts
            results.append(result)

            if debug_symbol and sym.upper() == debug_symbol.upper():
                _print_debug(sym, bars if bars else bars_1m, result, now)
    finally:
        conn.close()

    # Status summary
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    logger.info(
        "V2 Health: %d/%d ACTIVE | %s",
        status_counts.get(STATUS_ACTIVE, 0),
        len(results),
        " | ".join(f"{s}:{status_counts.get(s,0)}" for s in ALL_STATUSES),
    )

    if not dry_run:
        written = db.upsert_health(results)
        logger.info("Wrote %d health rows to vanguard_health", written)
    else:
        logger.info("[DRY RUN] Would write %d rows (skipped)", len(results))

    return results


# ---------------------------------------------------------------------------
# Debug trace
# ---------------------------------------------------------------------------

def _print_debug(sym: str, bars: list[dict], result: dict, now: datetime) -> None:
    print(f"\n=== DEBUG: {sym} ===")
    print(f"  asset_class    : {result.get('asset_class')}")
    print(f"  session_active : {bool(result.get('session_active'))}")
    print(f"  bars_available : {result['bars_available']}")
    print(f"  last_bar_ts    : {result['last_bar_ts_utc']}")
    if bars:
        last = bars[-1]
        print(f"  last bar OHLCV : o={last.get('open')} h={last.get('high')} "
              f"l={last.get('low')} c={last.get('close')} v={last.get('volume')}")
        last_ts = _parse_utc(last["bar_ts_utc"])
        age_min = (now - last_ts).total_seconds() / 60.0
        print(f"  age (min)      : {age_min:.1f}")
    print(f"  relative_volume: {result['relative_volume']}")
    print(f"  spread_bps     : {result['spread_bps']}")
    print(f"  → STATUS       : {result['status']}")


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def validate(symbols: list[str] | None = None) -> bool:
    """
    Validate the most recent health cycle.
    Returns True if validation passes.
    """
    db = VanguardDB(DB_PATH)
    cycle_ts = db.get_latest_health_cycle()

    if not cycle_ts:
        print("[VALIDATE] FAIL — no health data in DB. Run prefilter first.")
        return False

    status_counts = db.count_health_by_status(cycle_ts)
    total = sum(status_counts.values())
    active = status_counts.get(STATUS_ACTIVE, 0)

    print(f"\n[VALIDATE] Most recent cycle: {_format_et(cycle_ts)}")
    print(f"  Total symbols  : {total}")
    for s in ALL_STATUSES:
        print(f"  {s:<12}: {status_counts.get(s, 0)}")

    # Check at least one ACTIVE symbol exists in latest cycle
    conn = db.connect()
    try:
        active_row = conn.execute(
            "SELECT COUNT(*) FROM vanguard_health "
            "WHERE cycle_ts_utc = ? AND status = 'ACTIVE'",
            (cycle_ts,),
        ).fetchone()
    finally:
        conn.close()

    if not active_row or active_row[0] == 0:
        print("\n[VALIDATE] FAIL — zero ACTIVE symbols in latest cycle")
        return False

    print(f"\n[VALIDATE] PASS — {active}/{total} ACTIVE")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V2 Vanguard Prefilter — live health monitor"
    )
    p.add_argument(
        "--symbols", default=None,
        help="Comma-separated list of symbols (default: all in DB)",
    )
    p.add_argument(
        "--validate", action="store_true",
        help="Validate most recent health cycle and exit",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute health checks but do not write to DB",
    )
    p.add_argument(
        "--debug", default=None, metavar="SYMBOL",
        help="Print detailed trace for one symbol",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    symbol_list: list[str] | None = None
    if args.symbols:
        symbol_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.validate and not symbol_list:
        # Validate-only mode: just check what's already in DB
        ok = validate()
        sys.exit(0 if ok else 1)

    # Run health checks
    run(
        symbols=symbol_list,
        dry_run=args.dry_run,
        debug_symbol=args.debug,
    )

    # Always validate after a run
    validate(symbol_list)


if __name__ == "__main__":
    main()
