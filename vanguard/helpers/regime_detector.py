"""
regime_detector.py — 5-feature regime gate for V5 Strategy Router.

Classifies the current cycle as:
  ACTIVE  — trade normally
  CAUTION — raise edge threshold (high volatility)
  DEAD    — skip cycle (lunch hour + low volume)
  CLOSED  — skip cycle (outside session hours)

V3 Features used:
  atr_expansion, relative_volume, volume_burst_z,
  session_phase, bars_since_session_open

Location: ~/SS/Vanguard/vanguard/helpers/regime_detector.py
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from vanguard.helpers.clock import now_utc, utc_to_et, is_market_open

logger = logging.getLogger(__name__)

# Thresholds (override via vanguard_selection_config.json)
ATR_CAUTION_THRESHOLD = 2.0      # median atr_expansion above this → CAUTION
VOLUME_DEAD_THRESHOLD = 0.5      # median relative_volume below this → dead zone
LUNCH_HOUR_START = (12, 0)       # ET hour/minute
LUNCH_HOUR_END   = (13, 0)       # ET hour/minute


def detect_regime(
    features_df: pd.DataFrame,
    dt_utc: datetime | None = None,
    atr_caution_threshold: float = ATR_CAUTION_THRESHOLD,
    volume_dead_threshold: float = VOLUME_DEAD_THRESHOLD,
) -> str:
    """
    Determine the trading regime for the current cycle.

    Parameters
    ----------
    features_df : DataFrame with V3 features (one row per symbol).
    dt_utc      : evaluation time (defaults to now_utc()).
    atr_caution_threshold : median ATR expansion above which → CAUTION.
    volume_dead_threshold : median relative_volume below which (+ lunch) → DEAD.

    Returns
    -------
    'ACTIVE' | 'CAUTION' | 'DEAD' | 'CLOSED'
    """
    if dt_utc is None:
        dt_utc = now_utc()

    # ── CLOSED: outside equity session hours ─────────────────────────────────
    if not is_market_open(dt_utc):
        logger.info("Regime → CLOSED (outside session hours)")
        return "CLOSED"

    # ── Feature medians (NaN-safe) ───────────────────────────────────────────
    med_atr = features_df["atr_expansion"].median() if "atr_expansion" in features_df.columns else 1.0
    med_vol = features_df["relative_volume"].median() if "relative_volume" in features_df.columns else 1.0

    # ── CAUTION: high volatility expansion ───────────────────────────────────
    if med_atr > atr_caution_threshold:
        logger.info(f"Regime → CAUTION (median atr_expansion={med_atr:.2f} > {atr_caution_threshold})")
        return "CAUTION"

    # ── DEAD: lunch hour + low volume ────────────────────────────────────────
    dt_et = utc_to_et(dt_utc)
    in_lunch = (
        (dt_et.hour, dt_et.minute) >= LUNCH_HOUR_START
        and (dt_et.hour, dt_et.minute) < LUNCH_HOUR_END
    )
    if in_lunch and med_vol < volume_dead_threshold:
        logger.info(
            f"Regime → DEAD (lunch hour, median relative_volume={med_vol:.2f} < {volume_dead_threshold})"
        )
        return "DEAD"

    logger.info(f"Regime → ACTIVE (atr={med_atr:.2f}, vol={med_vol:.2f})")
    return "ACTIVE"
