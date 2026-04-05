"""
clock.py — UTC/ET time helpers and market session detection for Vanguard.

Location: ~/SS/Vanguard/vanguard/helpers/clock.py
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Regular market hours (Eastern Time)
_MARKET_OPEN_H, _MARKET_OPEN_M = 9, 30
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 16, 0

# Prefilter refresh times (ET) — 9:35, 12:00, 15:00
PREFILTER_TIMES_ET: list[tuple[int, int]] = [(9, 35), (12, 0), (15, 0)]


# ---------------------------------------------------------------------------
# Basic now helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    """Current time in UTC (tz-aware)."""
    return datetime.now(UTC)


def now_et() -> datetime:
    """Current time in America/New_York (tz-aware)."""
    return datetime.now(ET)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def utc_to_et(dt: datetime) -> datetime:
    """Convert a UTC (or naive UTC) datetime to ET."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def et_to_utc(dt: datetime) -> datetime:
    """Convert an ET (or naive ET) datetime to UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(UTC)


def iso_utc(dt: datetime) -> str:
    """Format a datetime as ISO-8601 UTC string (e.g. '2026-03-28T14:30:00Z')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Market session helpers
# ---------------------------------------------------------------------------

def is_market_open(dt_utc: datetime | None = None) -> bool:
    """
    True if dt_utc falls within regular equity market hours
    (9:30–16:00 ET, Monday–Friday).
    """
    if dt_utc is None:
        dt_utc = now_utc()
    dt_et = utc_to_et(dt_utc)
    if dt_et.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    open_et = dt_et.replace(
        hour=_MARKET_OPEN_H, minute=_MARKET_OPEN_M, second=0, microsecond=0
    )
    close_et = dt_et.replace(
        hour=_MARKET_CLOSE_H, minute=_MARKET_CLOSE_M, second=0, microsecond=0
    )
    return open_et <= dt_et < close_et


def market_open_utc(ref_et: datetime | None = None) -> datetime:
    """9:30 AM ET for ref_et's date, expressed in UTC."""
    if ref_et is None:
        ref_et = now_et()
    open_et = ref_et.replace(
        hour=_MARKET_OPEN_H, minute=_MARKET_OPEN_M, second=0, microsecond=0
    )
    return et_to_utc(open_et)


def market_close_utc(ref_et: datetime | None = None) -> datetime:
    """4:00 PM ET for ref_et's date, expressed in UTC."""
    if ref_et is None:
        ref_et = now_et()
    close_et = ref_et.replace(
        hour=_MARKET_CLOSE_H, minute=_MARKET_CLOSE_M, second=0, microsecond=0
    )
    return et_to_utc(close_et)


def last_market_open_utc() -> datetime:
    """
    Return the most recent 9:30 AM ET open time in UTC.
    If today's open is in the future (weekend / pre-market), walk back to
    the most recent weekday.
    """
    et_now = now_et()
    # Walk back up to 7 days to find a weekday
    for days_back in range(7):
        candidate = et_now - timedelta(days=days_back)
        if candidate.weekday() < 5:         # Mon–Fri
            open_et = candidate.replace(
                hour=_MARKET_OPEN_H, minute=_MARKET_OPEN_M,
                second=0, microsecond=0,
            )
            if days_back == 0 and et_now < open_et:
                continue                    # today's open hasn't happened yet
            return et_to_utc(open_et)
    # Fallback: today 9:30 ET
    return market_open_utc()


# ---------------------------------------------------------------------------
# Bar-slot rounding
# ---------------------------------------------------------------------------

def round_down_5m(dt: datetime) -> datetime:
    """Floor dt to the nearest 5-minute boundary (bar open time)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    minutes = (dt.minute // 5) * 5
    return dt.replace(minute=minutes, second=0, microsecond=0)


def round_down_1h(dt: datetime) -> datetime:
    """Floor dt to the nearest hour boundary."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Prefilter schedule
# ---------------------------------------------------------------------------

def is_prefilter_due(last_refresh_et: datetime | None) -> bool:
    """
    True if a prefilter refresh is scheduled since last_refresh_et.
    Compares against PREFILTER_TIMES_ET on the current ET date.
    """
    now = now_et()
    if last_refresh_et is None:
        return True
    for hour, minute in PREFILTER_TIMES_ET:
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if last_refresh_et < scheduled <= now:
            return True
    return False


# ---------------------------------------------------------------------------
# Date helpers for API calls
# ---------------------------------------------------------------------------

def trading_days_ago(n: int, ref_utc: datetime | None = None) -> datetime:
    """
    Return a UTC datetime approximately n calendar days ago from ref_utc.
    Used as a 'start' anchor for daily bar fetches.
    Not accounting for holidays — conservative over-fetch is fine.
    """
    if ref_utc is None:
        ref_utc = now_utc()
    # Use 2× calendar days to ensure we get n trading days worth of data
    return ref_utc - timedelta(days=n * 2)
