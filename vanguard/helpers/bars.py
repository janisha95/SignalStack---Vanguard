"""
bars.py — Bar normalization and 5m→1h aggregation for Vanguard.

Handles Alpaca and MT5 raw bar dicts. All timestamps stored as UTC
bar END time (Vanguard convention).

Location: ~/SS/Vanguard/vanguard/helpers/bars.py
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 UTC string (with or without Z suffix)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def bar_end_iso(bar_open_iso: str, interval_min: int) -> str:
    """
    Convert a bar OPEN time (ISO-8601 UTC string) to bar END time.
    Vanguard stores bar_ts_utc = bar END time.
    """
    dt = parse_utc(bar_open_iso)
    dt_end = dt + timedelta(minutes=interval_min)
    return dt_end.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Alpaca normalization
# ---------------------------------------------------------------------------

def normalize_alpaca_bar(
    raw: dict,
    symbol: str,
    interval_min: int = 5,
    asset_class: str = "equity",
    data_source: str = "alpaca",
) -> dict:
    """
    Normalize a raw Alpaca bar dict to the vanguard_bars_5m schema.

    Alpaca bar keys: t (open time UTC), o, h, l, c, v, n, vw
    Converts t → bar END time (t + interval_min).
    """
    return {
        "symbol":      symbol.upper(),
        "bar_ts_utc":  bar_end_iso(raw["t"], interval_min),
        "open":        float(raw["o"]),
        "high":        float(raw["h"]),
        "low":         float(raw["l"]),
        "close":       float(raw["c"]),
        "volume":      int(raw.get("v", 0)),
        "asset_class": asset_class,
        "data_source": data_source,
    }


# ---------------------------------------------------------------------------
# MT5 normalization
# ---------------------------------------------------------------------------

def normalize_mt5_bar(
    raw: dict,
    symbol: str,
    interval_min: int = 5,
    asset_class: str = "equity",
) -> dict:
    """
    Normalize a raw MT5 bar dict to the vanguard_bars_5m schema.

    MT5 bar keys: time (Unix timestamp, bar OPEN time), open, high, low,
    close, tick_volume (or real_volume), spread.
    MT5 time is bar OPEN time — must add interval to get END time.
    """
    import datetime as _dt
    open_dt = _dt.datetime.fromtimestamp(raw["time"], tz=UTC)
    end_dt = open_dt + timedelta(minutes=interval_min)
    return {
        "symbol":      symbol.upper(),
        "bar_ts_utc":  end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open":        float(raw["open"]),
        "high":        float(raw["high"]),
        "low":         float(raw["low"]),
        "close":       float(raw["close"]),
        "volume":      int(raw.get("tick_volume", raw.get("real_volume", 0))),
        "asset_class": asset_class,
        "data_source": "mt5",
    }


# ---------------------------------------------------------------------------
# 1m → 5m aggregation
# ---------------------------------------------------------------------------

def aggregate_1m_to_timeframe(bars_1m: list[dict], timeframe_minutes: int) -> list[dict]:
    """
    Aggregate a list of 1m bars into a clock-aligned intraday timeframe.

    Grouping rule:
        bar_open_utc = bar_ts_utc (end time) - 1 min
        bucket_start = floor(bar_open_utc to timeframe_minutes)
        bar_ts_utc   = bucket_start + timeframe_minutes

    OHLCV:
        open   = first bar's open
        high   = max of highs
        low    = min of lows
        close  = last bar's close
        volume = sum of volumes

    Preserves asset_class and data_source from the last bar in each bucket.
    """
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for bar in bars_1m:
        bar_end  = parse_utc(bar["bar_ts_utc"])
        bar_open = bar_end - timedelta(minutes=1)
        bucket_minute = (bar_open.minute // timeframe_minutes) * timeframe_minutes
        bucket_start  = bar_open.replace(minute=bucket_minute, second=0, microsecond=0)
        bucket_end    = bucket_start + timedelta(minutes=timeframe_minutes)
        bucket_end_ts = bucket_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        groups[(bar["symbol"], bucket_end_ts)].append(bar)

    result = []
    for (symbol, bucket_end_ts), group in groups.items():
        group.sort(key=lambda b: b["bar_ts_utc"])
        last = group[-1]
        result.append({
            "symbol":      symbol,
            "bar_ts_utc":  bucket_end_ts,
            "open":        group[0]["open"],
            "high":        max(b["high"] for b in group),
            "low":         min(b["low"]  for b in group),
            "close":       last["close"],
            "volume":      sum(b["volume"] for b in group),
            "asset_class": last.get("asset_class", "unknown"),
            "data_source": last.get("data_source", "unknown"),
        })

    return result


def aggregate_1m_to_5m(bars_1m: list[dict]) -> list[dict]:
    """Backward-compatible 1m → 5m wrapper."""
    return aggregate_1m_to_timeframe(bars_1m, 5)


# ---------------------------------------------------------------------------
# 5m → 1h aggregation
# ---------------------------------------------------------------------------

def aggregate_5m_to_1h(bars_5m: list[dict]) -> list[dict]:
    """
    Aggregate a list of 5m bars into 1h bars.
    12 × 5m = 1h. Groups by clock-aligned UTC hour.

    Grouping rule:
        bar_open_utc = bar_ts_utc (end time) - 5 min
        hour_start   = floor(bar_open_utc, hour)
        1h bar_ts_utc = hour_start + 1h  (end-of-hour, Vanguard convention)

    OHLCV:
        open  = first bar's open  (chronological order)
        high  = max of highs
        low   = min of lows
        close = last bar's close
        volume = sum of volumes

    Returns list of dicts matching vanguard_bars_1h schema.
    """
    # Group by (symbol, hour_end_ts)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for bar in bars_5m:
        bar_end = parse_utc(bar["bar_ts_utc"])
        bar_open = bar_end - timedelta(minutes=5)
        hour_start = bar_open.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        hour_end_ts = hour_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        groups[(bar["symbol"], hour_end_ts)].append(bar)

    result = []
    for (symbol, hour_end_ts), group in groups.items():
        group.sort(key=lambda b: b["bar_ts_utc"])
        result.append({
            "symbol":     symbol,
            "bar_ts_utc": hour_end_ts,
            "open":       group[0]["open"],
            "high":       max(b["high"] for b in group),
            "low":        min(b["low"]  for b in group),
            "close":      group[-1]["close"],
            "volume":     sum(b["volume"] for b in group),
        })

    return result


# ---------------------------------------------------------------------------
# Daily bar helpers (used by prefilter)
# ---------------------------------------------------------------------------

def avg_volume_from_daily_bars(bars: list[dict]) -> float:
    """
    Compute average daily volume from a list of raw Alpaca daily bar dicts.
    Returns 0.0 if no bars.
    """
    if not bars:
        return 0.0
    volumes = [int(b.get("v", 0)) for b in bars]
    return sum(volumes) / len(volumes)


def last_close_from_daily_bars(bars: list[dict]) -> float:
    """
    Return the close price of the most recent daily bar.
    Returns 0.0 if no bars.
    """
    if not bars:
        return 0.0
    # Alpaca daily bars are returned in chronological order; last is most recent
    return float(bars[-1].get("c", 0.0))
