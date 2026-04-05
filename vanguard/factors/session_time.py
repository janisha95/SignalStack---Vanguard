"""
session_time.py — Session Time factor module for Vanguard V3.

Computes 3 features from the last bar's timestamp:
  1. session_phase          — 0.0 (open) to 1.0 (close), NaN outside session
  2. time_in_session_pct    — minutes elapsed / 390 total session minutes
  3. bars_since_session_open — count of 5m bars from 9:30 ET to last bar

Session = NYSE regular hours: 09:30–16:00 ET = 390 minutes = 78 x 5m bars.
If the last bar is outside session hours (or no bars), all features are NaN.

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/session_time.py
"""
from __future__ import annotations

import math
from datetime import time as dtime, timedelta

import pandas as pd

FEATURE_NAMES = [
    "session_phase",
    "time_in_session_pct",
    "bars_since_session_open",
]

_NAN = float("nan")

# NYSE regular session: 09:30–16:00 ET
_SESSION_OPEN_HOUR   = 9
_SESSION_OPEN_MIN    = 30
_SESSION_CLOSE_HOUR  = 16
_SESSION_CLOSE_MIN   = 0
_SESSION_TOTAL_MINS  = 390   # 9:30 → 16:00
_BARS_PER_SESSION    = 78    # 390 / 5

_ET_OFFSET_STD  = timedelta(hours=-5)   # EST (UTC-5)
_ET_OFFSET_DST  = timedelta(hours=-4)   # EDT (UTC-4)


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def _et_offset_for(dt_utc: pd.Timestamp) -> timedelta:
    """
    Approximate DST: second Sunday in March → first Sunday in November.
    Using calendar-arithmetic rather than pytz to avoid an optional dep.
    """
    year  = dt_utc.year
    month = dt_utc.month
    day   = dt_utc.day

    # Second Sunday in March
    march_first_dow = pd.Timestamp(year, 3, 1).dayofweek   # Mon=0, Sun=6
    days_to_first_sun = (6 - march_first_dow) % 7
    dst_start = 8 + days_to_first_sun    # day-of-month of second Sunday

    # First Sunday in November
    nov_first_dow = pd.Timestamp(year, 11, 1).dayofweek
    days_to_first_sun_nov = (6 - nov_first_dow) % 7
    dst_end = 1 + days_to_first_sun_nov

    if month < 3 or month > 11:
        return _ET_OFFSET_STD
    if month == 3 and day < dst_start:
        return _ET_OFFSET_STD
    if month == 11 and day >= dst_end:
        return _ET_OFFSET_STD
    return _ET_OFFSET_DST


def _to_et(bar_ts_utc: str) -> pd.Timestamp | None:
    """
    Convert a bar_ts_utc ISO string to an ET naive timestamp.
    bar_ts_utc is the bar END time (already in UTC).
    """
    try:
        dt = pd.Timestamp(bar_ts_utc, tz="UTC")
    except Exception:
        return None
    offset = _et_offset_for(dt)
    return dt + offset


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 3 session-time features from the last bar's timestamp.

    Parameters
    ----------
    df_5m   : 5-minute bars DataFrame (bar_ts_utc, open, high, low, close, volume)
    df_1h   : 1-hour bars (not used)
    spy_df  : SPY bars (not used)

    Returns
    -------
    dict mapping feature_name → float (NaN if outside session or missing data)
    """
    if df_5m is None or len(df_5m) == 0:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    last_ts_str = str(df.iloc[-1]["bar_ts_utc"])
    et = _to_et(last_ts_str)
    if et is None:
        return _nan_result()

    bar_time = dtime(et.hour, et.minute)
    session_open  = dtime(_SESSION_OPEN_HOUR,  _SESSION_OPEN_MIN)
    session_close = dtime(_SESSION_CLOSE_HOUR, _SESSION_CLOSE_MIN)

    # Outside regular session → NaN
    if bar_time < session_open or bar_time > session_close:
        return _nan_result()

    # Minutes elapsed since 09:30
    open_minutes  = _SESSION_OPEN_HOUR * 60 + _SESSION_OPEN_MIN
    bar_minutes   = et.hour * 60 + et.minute
    elapsed_mins  = bar_minutes - open_minutes   # 0 at 09:30, 390 at 16:00

    # Clip to valid range
    elapsed_mins  = max(0, min(elapsed_mins, _SESSION_TOTAL_MINS))

    time_pct = elapsed_mins / _SESSION_TOTAL_MINS        # 0.0 → 1.0
    bars_cnt = math.floor(elapsed_mins / 5)              # whole 5m bars elapsed

    return {
        "session_phase":           round(time_pct, 6),
        "time_in_session_pct":     round(time_pct, 6),
        "bars_since_session_open": float(bars_cnt),
    }
