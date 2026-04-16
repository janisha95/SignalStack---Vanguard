"""
auto_close.py — Auto-close positions that exceed max_holding_minutes (Phase 3f).

Hard rules:
  - QAenv paper/demo account ONLY. Never live GFT accounts.
  - Both Telegram messages fire: INITIATED + SUCCESS (or FAILED). Never silent.
  - In-flight lock prevents double-close of same position.
  - Pre-close sanity check: re-fetch broker state before sending close.
  - Max 3 closes per profile per minute (rate limiter).
  - Circuit breaker: 3 consecutive failures → pause all auto-close, alert.
  - Breaker resets via /tmp/autoclose_breaker_reset flag file (written by reset script).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AutoCloseBreakerTripped(Exception):
    """Raised if caller checks breaker status externally."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_utc(ts: str) -> Optional[datetime]:
    """Parse ISO-8601 UTC string to datetime. Returns None on failure."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ts, fmt) if "%" in fmt else None
            if dt is None:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            continue
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _holding_minutes(opened_at_utc: str, now_utc: str) -> float:
    """
    Returns minutes a position has been held.
    Returns 0.0 if either timestamp is unparseable.
    """
    opened = _parse_utc(opened_at_utc)
    now    = _parse_utc(now_utc)
    if opened is None or now is None:
        return 0.0
    delta = (now - opened).total_seconds()
    return max(0.0, delta / 60.0)


# ---------------------------------------------------------------------------
# AutoCloseManager
# ---------------------------------------------------------------------------

class AutoCloseManager:
    """
    Checks open positions against max_holding_minutes and closes overdue ones.

    Caller interface (from lifecycle_daemon._sync_profile):
        results = await self.auto_close_manager.check_and_close(
            profile, policy, broker_positions, now_utc
        )

    `policy` is the resolved policy dict for the profile (from policy_templates).
    It must have: policy["position_limits"]["auto_close_after_max_holding"] (bool)
                  policy["position_limits"]["max_holding_minutes"] (int)
    """

    _RATE_LIMIT_MAX  = 3     # closes per profile per rate window
    _RATE_LIMIT_SECS = 60    # rate window in seconds
    _BREAKER_THRESHOLD = 3   # consecutive failures before tripping

    def __init__(
        self,
        config: dict,
        db_path: str,
        telegram_fn: Any,          # callable(msg: str) or None
        metaapi_clients: dict,     # profile_id → MetaApiClient
    ) -> None:
        self.config          = config
        self.db_path         = db_path
        self.telegram_fn     = telegram_fn
        self.clients         = metaapi_clients             # mutable — daemon updates this

        self._in_flight: set[tuple[str, str]] = set()     # (profile_id, broker_position_id)
        self._recent_closes: dict[str, list[datetime]] = {}  # profile_id → timestamps
        self._consecutive_failures = 0
        self._breaker_tripped      = False

    # ── Public API ─────────────────────────────────────────────────────────

    async def check_and_close(
        self,
        profile: dict,
        policy: dict,
        broker_positions: list[dict],
        now_utc: str,
    ) -> list[tuple[str, dict]]:
        """
        Evaluate all broker_positions. Close any that exceed max_holding_minutes.

        Returns list of (broker_position_id, result_dict) for positions acted upon.
        Returns [] if breaker tripped, policy disabled, or no positions exceed threshold.
        """
        if self._breaker_tripped:
            logger.warning("[auto_close] Circuit breaker tripped — skipping all closes")
            return []

        limits = (policy.get("position_limits") or {})
        if not limits.get("auto_close_after_max_holding", False):
            return []

        max_minutes = int(limits.get("max_holding_minutes") or 0)
        if max_minutes <= 0:
            return []

        results: list[tuple[str, dict]] = []
        profile_id = profile["id"]

        for bp in broker_positions:
            held = _holding_minutes(bp.get("opened_at_utc", ""), now_utc)
            if held < max_minutes:
                continue

            key = (profile_id, str(bp.get("broker_position_id") or ""))
            if key in self._in_flight:
                logger.debug(
                    "[auto_close] position %s already in-flight — skip", key[1]
                )
                continue

            if not self._rate_limit_ok(profile_id, now_utc):
                logger.warning(
                    "[auto_close] rate limit hit for profile=%s — deferring close of %s",
                    profile_id, bp.get("broker_position_id"),
                )
                continue

            self._in_flight.add(key)
            try:
                result = await self._close_one(profile, policy, bp, held)
                results.append((str(bp.get("broker_position_id") or ""), result))
            except Exception as exc:
                # _close_one already handles telegram + failure counting
                logger.error(
                    "[auto_close] _close_one raised for profile=%s pos=%s: %s",
                    profile_id, bp.get("broker_position_id"), exc,
                )
                results.append((str(bp.get("broker_position_id") or ""), {"status": "ERROR", "error": str(exc)}))
            finally:
                self._in_flight.discard(key)

        return results

    def reset_breaker(self) -> None:
        """Reset circuit breaker and failure counter. Called by daemon on flag file."""
        self._consecutive_failures = 0
        self._breaker_tripped      = False
        logger.info("[auto_close] Circuit breaker reset")

    @property
    def breaker_tripped(self) -> bool:
        return self._breaker_tripped

    # ── Internal close logic ───────────────────────────────────────────────

    async def _close_one(
        self,
        profile: dict,
        policy: dict,
        broker_position: dict,
        holding_minutes: float,
    ) -> dict:
        """
        Execute a single close.

        Returns result dict with "status" key.
        Side effects: Telegram notifications, journal update.
        """
        from vanguard.execution.trade_journal import update_journal_closed_from_auto

        profile_id  = profile["id"]
        position_id = str(broker_position.get("broker_position_id") or "")
        symbol      = str(broker_position.get("symbol") or "")
        max_min     = int((policy.get("position_limits") or {}).get("max_holding_minutes") or 0)

        client = self.clients.get(profile_id)
        if client is None:
            logger.error("[auto_close] No MetaApiClient for profile=%s", profile_id)
            return {"status": "SKIPPED_NO_CLIENT", "position_id": position_id}

        # ── 1. Pre-close sanity: verify position still open ────────────────
        try:
            current_positions = await client.get_open_positions()
        except Exception as exc:
            logger.error(
                "[auto_close] pre-close re-fetch failed for %s: %s — aborting close",
                position_id, exc,
            )
            return {"status": "SKIPPED_PREFETCH_FAILED", "position_id": position_id, "error": str(exc)}

        still_open = any(
            str(p.get("broker_position_id") or "") == position_id
            for p in current_positions
        )
        if not still_open:
            logger.info(
                "[auto_close] position %s no longer open — skipping (already closed elsewhere)",
                position_id,
            )
            return {"status": "SKIPPED_ALREADY_CLOSED", "position_id": position_id}

        # ── 2. Telegram INITIATED ──────────────────────────────────────────
        self._send_telegram_safe(
            f"LIFECYCLE_AUTO_CLOSE_INITIATED\n"
            f"profile={profile_id} symbol={symbol} "
            f"held={holding_minutes:.1f}m max={max_min}m pos_id={position_id}"
        )

        # ── 3. Submit close to broker ──────────────────────────────────────
        try:
            close_result = await client.close_position(position_id, reason="AUTO_CLOSED_MAX_HOLD")
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error(
                "[auto_close] close_position raised for %s: %s (consecutive_failures=%d)",
                position_id, exc, self._consecutive_failures,
            )
            self._send_telegram_safe(
                f"LIFECYCLE_AUTO_CLOSE_FAILED\n"
                f"profile={profile_id} symbol={symbol} pos_id={position_id}\n"
                f"error={str(exc)[:200]}\n"
                f"MANUAL CLOSE REQUIRED"
            )
            if self._consecutive_failures >= self._BREAKER_THRESHOLD:
                self._breaker_tripped = True
                self._send_telegram_safe(
                    f"AUTO_CLOSE_CIRCUIT_BREAKER_TRIPPED — all auto-closes paused "
                    f"(consecutive_failures={self._consecutive_failures}). "
                    f"Run scripts/reset_autoclose_breaker.sh after investigation."
                )
            return {"status": "CLOSE_EXCEPTION", "position_id": position_id, "error": str(exc)}

        # ── 4. Check broker response ───────────────────────────────────────
        if close_result.get("status") != "closed":
            self._consecutive_failures += 1
            error_msg = str(close_result.get("error") or close_result)
            logger.warning(
                "[auto_close] close_position returned non-success for %s: %s (consecutive_failures=%d)",
                position_id, close_result.get("status"), self._consecutive_failures,
            )
            self._send_telegram_safe(
                f"LIFECYCLE_AUTO_CLOSE_FAILED\n"
                f"profile={profile_id} symbol={symbol} pos_id={position_id}\n"
                f"broker_status={close_result.get('status')}\n"
                f"error={error_msg[:200]}\n"
                f"MANUAL CLOSE REQUIRED"
            )
            if self._consecutive_failures >= self._BREAKER_THRESHOLD:
                self._breaker_tripped = True
                self._send_telegram_safe(
                    f"AUTO_CLOSE_CIRCUIT_BREAKER_TRIPPED — all auto-closes paused "
                    f"(consecutive_failures={self._consecutive_failures}). "
                    f"Run scripts/reset_autoclose_breaker.sh after investigation."
                )
            return {
                "status": "CLOSE_FAILED",
                "position_id": position_id,
                "broker_response": close_result,
            }

        # ── 5. Success path ────────────────────────────────────────────────
        self._consecutive_failures = 0
        self._record_close_time(profile_id)

        close_price  = close_result.get("close_price")
        close_time   = close_result.get("close_time")
        realized_pnl = close_result.get("realized_pnl")

        # Update journal
        try:
            update_journal_closed_from_auto(
                db_path=self.db_path,
                broker_position_id=position_id,
                close_price=close_price,
                closed_at_utc=close_time,
                close_reason="AUTO_CLOSED_MAX_HOLD",
                realized_pnl=realized_pnl,
                holding_minutes=int(holding_minutes),
            )
        except Exception as exc:
            # Journal update failure should NOT prevent the success Telegram — close already happened
            logger.error(
                "[auto_close] journal update failed for %s: %s (close was successful at broker)",
                position_id, exc,
            )

        # Telegram SUCCESS
        self._send_telegram_safe(
            f"LIFECYCLE_AUTO_CLOSE_SUCCESS\n"
            f"profile={profile_id} symbol={symbol} pos_id={position_id}\n"
            f"held={holding_minutes:.1f}m close_price={close_price} pnl={realized_pnl}"
        )

        logger.info(
            "[auto_close] closed position %s for profile=%s symbol=%s held=%.1fm",
            position_id, profile_id, symbol, holding_minutes,
        )

        return {
            "status":       "CLOSED",
            "position_id":  position_id,
            "close_price":  close_price,
            "close_time":   close_time,
            "realized_pnl": realized_pnl,
        }

    # ── Rate limiter ───────────────────────────────────────────────────────

    def _rate_limit_ok(self, profile_id: str, now_utc: str) -> bool:
        """Return True if another close is allowed for profile_id right now."""
        now = _parse_utc(now_utc) or datetime.now(timezone.utc)
        closes = self._recent_closes.get(profile_id, [])
        # Prune timestamps older than rate window
        cutoff = now.timestamp() - self._RATE_LIMIT_SECS
        closes = [ts for ts in closes if ts.timestamp() > cutoff]
        self._recent_closes[profile_id] = closes
        return len(closes) < self._RATE_LIMIT_MAX

    def _record_close_time(self, profile_id: str) -> None:
        """Record a successful close timestamp for rate limiting."""
        closes = self._recent_closes.get(profile_id, [])
        closes.append(datetime.now(timezone.utc))
        self._recent_closes[profile_id] = closes

    # ── Telegram helper ────────────────────────────────────────────────────

    def _send_telegram_safe(self, msg: str) -> None:
        """Send Telegram message without raising. Logs warning on failure."""
        if self.telegram_fn is None:
            logger.info("[auto_close] [telegram] %s", msg)
            return
        try:
            self.telegram_fn(msg)
        except Exception as exc:
            logger.warning(
                "[auto_close] Telegram send failed: %s. Message was: %s", exc, msg
            )
