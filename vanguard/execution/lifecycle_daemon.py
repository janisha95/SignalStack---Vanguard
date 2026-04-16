"""
lifecycle_daemon.py — Scheduled background loop.

Every interval_s seconds, for each active execution profile:
  1. Fetch broker positions + account state
  2. Mirror broker/context truth into the DB
  3. Detect and resolve reconciliation divergences
  4. Apply configured automated close logic

Hard rules:
  - MetaApi outage: skip that profile this iteration, alert once.
  - Exception in one iteration: log + continue loop (never crash).
  - Shutdown: SIGTERM sets flag, loop exits cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT.parent / ".env")

from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lifecycle_daemon")
_ET_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Env helpers (same pattern as other scripts)
# ---------------------------------------------------------------------------

def _raw_env_extract(var: str) -> str:
    env_path = _REPO_ROOT.parent / ".env"
    if not env_path.exists():
        return ""
    content = env_path.read_text()
    matches = re.findall(rf'{re.escape(var)}=([^\s\n]+)', content)
    return matches[0] if matches else ""


def _resolve_env(value: str) -> str:
    if str(value or "").startswith("ENV:"):
        var = value[4:].strip()
        resolved = os.environ.get(var, "")
        if not resolved or len(resolved) < 20:
            resolved = _raw_env_extract(var)
        return resolved  # Return empty string — caller decides what to do
    return value


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_active_profile(profile: dict[str, Any]) -> bool:
    return str(profile.get("is_active") or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_active_mt5_source_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve active MT5/DWX source from runtime profiles before legacy fallback."""
    profiles = cfg.get("profiles") or []
    context_sources = cfg.get("context_sources") or {}

    for profile in profiles:
        if not _is_active_profile(profile):
            continue
        bridge = str(profile.get("execution_bridge") or "")
        source_id = str(profile.get("context_source_id") or "")
        if not bridge.startswith("mt5_local") or not source_id:
            continue
        source_cfg = dict(context_sources.get(source_id) or {})
        if not source_cfg or not bool(source_cfg.get("enabled", False)):
            continue
        source_cfg["_source_id"] = source_id
        source_cfg["_profile_id"] = str(profile.get("id") or "")
        return source_cfg

    for source_id, source_cfg in (context_sources or {}).items():
        source_cfg = dict(source_cfg or {})
        if not source_cfg or not bool(source_cfg.get("enabled", False)):
            continue
        if str(source_cfg.get("source_type") or "").lower() != "dwx_mt5":
            continue
        source_cfg["_source_id"] = str(source_id)
        return source_cfg

    return dict((cfg.get("data_sources") or {}).get("mt5_local") or {})


# ---------------------------------------------------------------------------
# LifecycleDaemon
# ---------------------------------------------------------------------------

class LifecycleDaemon:
    """
    Scheduled reconciliation loop — wraps 3a + 3c + 3d + 3f into a single per-profile cycle.
    """

    _BREAKER_RESET_FILE = Path("/tmp/autoclose_breaker_reset")

    def __init__(
        self,
        config: dict,
        db_path: str,
        interval_s: int = 60,
        telegram_fn: Optional[Any] = None,
    ) -> None:
        from vanguard.execution.auto_close import AutoCloseManager

        self.config      = config
        self.db_path     = db_path
        self.interval_s  = interval_s
        self.telegram_fn = telegram_fn
        self._shutdown   = False
        self._clients: dict[str, Any] = {}         # profile_id → MetaApiClient
        self._outage_flags: dict[str, bool] = {}   # profile_id → True if currently in outage
        self._iteration  = 0
        self._position_action_service = None
        self.auto_close_manager = AutoCloseManager(
            config=config,
            db_path=db_path,
            telegram_fn=telegram_fn,
            metaapi_clients=self._clients,  # shared reference — populated in run()
        )

        # DWX adapter — shared across all active mt5_local profiles.
        self._mt5: Optional[MT5DWXAdapter] = None
        try:
            mt5_cfg = _resolve_active_mt5_source_cfg(self.config)
            if mt5_cfg.get("enabled"):
                self._mt5 = MT5DWXAdapter(
                    dwx_files_path=mt5_cfg["dwx_files_path"],
                    db_path=db_path,
                    symbol_suffix=str(mt5_cfg.get("symbol_suffix") or ""),
                )
                if self._mt5.connect():
                    logger.info(
                        "[lifecycle_daemon] MT5 DWX adapter connected source=%s profile=%s",
                        mt5_cfg.get("_source_id", "mt5_local"),
                        mt5_cfg.get("_profile_id", ""),
                    )
                else:
                    logger.warning("[lifecycle_daemon] MT5 DWX adapter failed to connect")
                    self._mt5 = None
        except Exception as exc:
            logger.warning("[lifecycle_daemon] MT5 DWX init failed: %s", exc)
            self._mt5 = None

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop. Runs until self._shutdown is set."""
        profiles = self._active_profiles()
        if not profiles:
            logger.warning("[lifecycle_daemon] No active execution profiles found — exiting")
            return

        for profile in profiles:
            self._clients[profile["id"]] = self._build_client(profile)
            self._outage_flags[profile["id"]] = False

        self._send_telegram("LIFECYCLE_DAEMON_STARTED")
        logger.info(
            "[lifecycle_daemon] Started. profiles=%s interval=%ds db=%s",
            [p["id"] for p in profiles], self.interval_s, self.db_path,
        )

        while not self._shutdown:
            self._iteration += 1
            cycle_start = datetime.now(timezone.utc)
            logger.info("[lifecycle_daemon] Iteration %d begin", self._iteration)

            # Check for manual breaker reset flag file
            self._check_breaker_reset()

            for profile in self._active_profiles():
                pid = profile["id"]
                try:
                    await self._sync_profile(profile)
                    self._outage_flags[pid] = False
                except Exception as exc:
                    self._on_error(pid, exc)

            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            logger.info(
                "[lifecycle_daemon] Iteration %d complete — elapsed=%.2fs",
                self._iteration, elapsed,
            )
            sleep_for = max(0, self.interval_s - elapsed)
            if not self._shutdown:
                await asyncio.sleep(sleep_for)

        self._send_telegram("LIFECYCLE_DAEMON_STOPPED")
        logger.info("[lifecycle_daemon] Stopped cleanly.")

    def handle_shutdown(self, signum: int, frame: Any) -> None:
        """SIGTERM handler."""
        logger.info("[lifecycle_daemon] Received signal %d — shutting down", signum)
        self._shutdown = True

    # ── Per-profile sync ──────────────────────────────────────────────────

    async def _sync_profile(self, profile: dict) -> None:
        from vanguard.execution.open_positions_writer import upsert_open_positions
        from vanguard.execution.reconciler import (
            detect_divergences,
            load_journal_open,
            load_open_positions,
            resolve_divergences,
        )

        pid         = profile["id"]
        bridge_type = profile.get("bridge_type", "metaapi")
        now_utc_str = _iso_now()

        # ── 1. Fetch broker state ──────────────────────────────────────────
        if bridge_type == "mt5_local":
            # DWX path — sync calls, no async needed.
            # NOTE: only mt5_local_* bridges use DWX; metaapi_* bridges always use
            # the MetaApi HTTP path below, even when DWX happens to be connected.
            if not (self._mt5 and self._mt5.is_connected()):
                logger.warning("[lifecycle_daemon] DWX bridge profile=%s but adapter not connected — skipping", pid)
                return
            broker_positions = self._dwx_positions_to_metaapi_format(self._mt5.get_open_positions())
            broker_state     = self._mt5.get_account_info()
        else:
            # MetaApi path — async HTTP calls.
            from vanguard.execution.metaapi_client import MetaApiUnavailable  # noqa: PLC0415
            client = self._clients.get(pid)
            if client is None:
                client = self._build_client(profile)
                self._clients[pid] = client
            try:
                broker_positions = await client.get_open_positions()
                broker_state     = await client.get_account_state()
            except Exception as exc:
                if isinstance(exc, MetaApiUnavailable) or "MetaApi" in type(exc).__name__:
                    if not self._outage_flags.get(pid):
                        self._send_telegram(
                            f"LIFECYCLE_METAAPI_UNAVAILABLE profile={pid}: {exc}"
                        )
                        self._outage_flags[pid] = True
                    logger.error(
                        "[lifecycle_daemon] MetaApi unavailable for %s (iteration %d): %s",
                        pid, self._iteration, exc,
                    )
                    return  # Skip this profile this iteration
                raise

        # ── 2. Mirror open_positions ───────────────────────────────────────
        upsert_open_positions(
            db_path=self.db_path,
            profile_id=pid,
            positions=broker_positions,
            synced_at_utc=now_utc_str,
        )

        # ── 3. Update account state from broker equity ─────────────────────
        self._update_account_state(pid, broker_state, now_utc_str)

        # ── 4. Detect divergences ──────────────────────────────────────────
        db_open = load_open_positions(self.db_path, pid)
        db_journal_open = load_journal_open(self.db_path, pid)
        divergences = detect_divergences(
            db_path=self.db_path,
            profile_id=pid,
            broker_positions=broker_positions,
            db_open_positions=db_open,
            db_journal_open=db_journal_open,
        )

        # ── 5. Resolve divergences ─────────────────────────────────────────
        if divergences:
            logger.info(
                "[lifecycle_daemon] profile=%s found %d divergence(s) — resolving",
                pid, len(divergences),
            )
            counts = resolve_divergences(
                db_path=self.db_path,
                divergences=divergences,
                broker_positions=broker_positions,
                telegram_fn=self.telegram_fn,
            )
            logger.info(
                "[lifecycle_daemon] profile=%s resolved=%d failed=%d",
                pid, counts["resolved"], counts["failed"],
            )
        else:
            logger.debug("[lifecycle_daemon] profile=%s no divergences", pid)

        timeout_policy_enabled = self._timeout_policy_auto_close_enabled(profile)

        # ── 6. Timeout-policy auto-close (operator-action path) ─────────────
        if timeout_policy_enabled:
            timeout_results = self._process_timeout_policy_closes(profile)
            if timeout_results:
                logger.info(
                    "[lifecycle_daemon] profile=%s timeout_policy results: %s",
                    pid,
                    timeout_results,
                )

        flatten_status = self._process_scheduled_flatten(profile)
        if flatten_status:
            logger.info(
                "[lifecycle_daemon] profile=%s scheduled_flatten status=%s",
                pid,
                flatten_status,
            )

        # ── 7. Legacy max-hold auto-close (skip when timeout policy owns exit) ──
        policy = self._get_policy_for_profile(profile) if not timeout_policy_enabled else None
        if policy:
            ac_results = await self.auto_close_manager.check_and_close(
                profile=profile,
                policy=policy,
                broker_positions=broker_positions,
                now_utc=now_utc_str,
            )
            if ac_results:
                logger.info(
                    "[lifecycle_daemon] profile=%s auto_close results: %s",
                    pid,
                    [(pos_id, r.get("status")) for pos_id, r in ac_results],
                )

        # ── 8. Signal-driven exit rules (skip when timeout policy owns exit) ──
        if timeout_policy_enabled:
            logger.debug(
                "[lifecycle_daemon] profile=%s timeout policy auto-close enabled — skipping legacy signal exits",
                pid,
            )
        elif bridge_type == "mt5_local" and self._mt5 and self._mt5.is_connected():
            dwx_positions = self._mt5.get_open_positions()
            for ticket, pos in dwx_positions.items():
                current_signal = self.get_current_signal(pos["symbol"], pid)
                reason = self.check_exit_rules(pos, current_signal)
                if reason:
                    self.execute_exit(pos, reason)
        else:
            # Log only — no execution capability without DWX
            for pos in broker_positions:
                sym = pos.get("symbol", "")
                current_signal = self.get_current_signal(sym, pid)
                reason = self.check_exit_rules(
                    {
                        "ticket": pos.get("broker_position_id", ""),
                        "symbol": sym,
                        "type": "buy" if pos.get("side") == "LONG" else "sell",
                        "open_price": pos.get("entry_price", 0),
                        "pnl": pos.get("unrealized_pnl", 0),
                        "sl": pos.get("current_sl") or 0,
                        "open_time": None,
                    },
                    current_signal,
                )
                if reason:
                    logger.info(
                        "[lifecycle_daemon] EXIT SIGNAL (no DWX exec): profile=%s "
                        "symbol=%s reason=%s — no action taken",
                        pid, sym, reason,
                    )

        logger.info(
            "[lifecycle_daemon] profile=%s sync OK — broker_positions=%d equity=%.2f",
            pid, len(broker_positions), broker_state.get("equity", 0.0),
        )

    def _get_position_action_service(self):
        from vanguard.execution.operator_actions import PositionActionService
        from vanguard.executors.dwx_executor import DWXExecutor

        if self._position_action_service is None:
            def _executor_factory(profile_id: str, profile: dict[str, Any], db_path: str):
                bridge = str(profile.get("execution_bridge") or "").lower()
                if bridge.startswith("mt5_local") and self._mt5 and self._mt5.is_connected():
                    return DWXExecutor(mt5_adapter=self._mt5, profile_id=profile_id, db_path=db_path)
                raise ValueError(f"profile_id={profile_id!r} cannot be executed without a connected DWX adapter")

            self._position_action_service = PositionActionService(
                db_path=self.db_path,
                runtime_config=self.config,
                executor_factory=_executor_factory,
            )
        return self._position_action_service

    def _scheduled_flatten_config_for_profile(self, profile: dict[str, Any]) -> dict[str, Any] | None:
        cfg = ((self.config.get("position_manager") or {}).get("scheduled_flatten") or {})
        if not bool(cfg.get("enabled", False)):
            return None
        if str(profile.get("bridge_type") or "") != "mt5_local":
            return None
        eligible = {
            str(profile_id).strip()
            for profile_id in (cfg.get("eligible_profiles") or [])
            if str(profile_id).strip()
        }
        profile_id = str(profile.get("id") or "")
        if eligible and profile_id not in eligible:
            return None
        start_et = str(cfg.get("start_et") or "").strip()
        end_et = str(cfg.get("end_et") or "").strip()
        if not start_et or not end_et:
            return None
        return cfg

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        hour_str, minute_str = str(value).split(":", 1)
        return (
            max(0, min(23, int(hour_str))),
            max(0, min(59, int(minute_str))),
        )

    def _scheduled_flatten_window_key(self, profile: dict[str, Any], now_et: datetime, cfg: dict[str, Any]) -> str:
        return f"{profile.get('id')}|{now_et.strftime('%Y-%m-%d')}|{cfg.get('start_et')}|{cfg.get('end_et')}"

    def _scheduled_flatten_active(
        self,
        profile: dict[str, Any],
        now_et: datetime | None = None,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        cfg = self._scheduled_flatten_config_for_profile(profile)
        if cfg is None:
            return False, None, None
        now_local = now_et or datetime.now(_ET_TZ)
        start_h, start_m = self._parse_hhmm(str(cfg.get("start_et")))
        end_h, end_m = self._parse_hhmm(str(cfg.get("end_et")))
        current_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        if current_minutes < start_minutes or current_minutes > end_minutes:
            return False, None, cfg
        return True, self._scheduled_flatten_window_key(profile, now_local, cfg), cfg

    def _process_scheduled_flatten(
        self,
        profile: dict[str, Any],
        now_et: datetime | None = None,
    ) -> str | None:
        active, window_key, cfg = self._scheduled_flatten_active(profile, now_et=now_et)
        if not active or window_key is None or cfg is None:
            return None
        fired = getattr(self, "_scheduled_flatten_fired", set())
        if window_key in fired:
            return None
        service = self._get_position_action_service()
        profile_id = str(profile.get("id") or "")
        try:
            snapshot = service.get_position_snapshot(profile_id)
        except Exception as exc:
            logger.warning(
                "[lifecycle_daemon] profile=%s scheduled flatten snapshot failed: %s",
                profile_id,
                exc,
            )
            return None
        if not list(snapshot.get("positions") or []):
            fired.add(window_key)
            self._scheduled_flatten_fired = fired
            return None
        request_id = service.queue_flatten_profile(
            profile_id=profile_id,
            requested_by="lifecycle_daemon",
        )
        result = service.process_request(request_id, dry_run=False)
        try:
            refreshed = service.get_position_snapshot(profile_id)
            remaining = list(refreshed.get("positions") or [])
        except Exception as exc:
            logger.warning(
                "[lifecycle_daemon] profile=%s scheduled flatten recheck failed: %s",
                profile_id,
                exc,
            )
            remaining = [{"broker_position_id": "unknown"}]
        if result.status == "RECONCILED" and not remaining:
            fired.add(window_key)
            self._scheduled_flatten_fired = fired
            return "RECONCILED"
        return f"PARTIAL_REMAINING_{len(remaining)}"

    def _timeout_policy_auto_close_enabled(self, profile: dict) -> bool:
        cfg = ((self.config.get("position_manager") or {}).get("timeout_auto_close") or {})
        if not bool(cfg.get("enabled", False)):
            return False
        if str(profile.get("bridge_type") or "") != "mt5_local":
            return False
        eligible = {
            str(profile_id).strip()
            for profile_id in (cfg.get("eligible_profiles") or [])
            if str(profile_id).strip()
        }
        if eligible and str(profile.get("id") or "") not in eligible:
            return False
        return True

    def _process_timeout_policy_closes(self, profile: dict) -> list[tuple[str, str]]:
        service = self._get_position_action_service()
        profile_id = str(profile.get("id") or "")
        try:
            snapshot = service.get_position_snapshot(profile_id)
        except Exception as exc:
            logger.warning(
                "[lifecycle_daemon] profile=%s timeout snapshot failed: %s",
                profile_id,
                exc,
            )
            return []

        results: list[tuple[str, str]] = []
        for row in snapshot.get("positions") or []:
            timeout_minutes = row.get("policy_timeout_minutes")
            minutes_left = row.get("minutes_to_timeout")
            broker_position_id = str(row.get("broker_position_id") or "")
            if timeout_minutes in (None, "") or minutes_left is None or broker_position_id == "":
                continue
            if float(minutes_left) > 0:
                continue
            request_id = service.queue_close_at_timeout(
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                requested_by="lifecycle_daemon",
            )
            result = service.process_request(request_id, dry_run=False)
            results.append((broker_position_id, str(result.status)))
        return results

    # ── Account state update ──────────────────────────────────────────────

    def _update_account_state(self, profile_id: str, broker_state: dict, now_utc: str) -> None:
        """
        UPDATE vanguard_account_state SET equity=..., updated_at_utc=... WHERE profile_id=?

        Note: daily_pnl_pct and trailing_dd_pct are NOT updated here (Phase 2b seeded zeros
        are preserved — those computations are deferred).
        """
        equity = float(broker_state.get("equity") or 0.0)
        try:
            con = sqlite3.connect(self.db_path)
            updated = con.execute(
                "UPDATE vanguard_account_state SET equity = ?, updated_at_utc = ? WHERE profile_id = ?",
                (equity, now_utc, profile_id),
            ).rowcount
            if updated == 0:
                # Row doesn't exist yet — insert
                con.execute(
                    """
                    INSERT OR IGNORE INTO vanguard_account_state
                        (profile_id, equity, starting_equity_today, daily_pnl_pct,
                         trailing_dd_pct, open_positions_json, updated_at_utc)
                    VALUES (?, ?, ?, 0.0, 0.0, '[]', ?)
                    """,
                    (profile_id, equity, equity, now_utc),
                )
            con.commit()
            con.close()
        except Exception as exc:
            logger.warning(
                "[lifecycle_daemon] _update_account_state failed for %s: %s", profile_id, exc
            )

    # ── Config helpers ────────────────────────────────────────────────────

    def _active_profiles(self) -> list[dict]:
        """
        Return active runtime profiles that have a supported execution bridge defined.
        Profile dict has: {"id": ..., "bridge_key": ..., "bridge_cfg": ..., "bridge_type": ...}
        """
        bridges = self.config.get("execution", {}).get("bridges", {})
        active = []
        for profile in self.config.get("profiles") or []:
            if not _is_active_profile(profile):
                continue
            profile_id = str(profile.get("id") or "").strip()
            bridge_key = str(profile.get("execution_bridge") or "").strip()
            if not profile_id or not bridge_key:
                continue
            bridge_cfg = dict(bridges.get(bridge_key) or {})
            if not bridge_cfg:
                continue
            bridge_type = ""
            if bridge_key.startswith("metaapi_"):
                bridge_type = "metaapi"
            elif bridge_key.startswith("mt5_local_"):
                bridge_type = "mt5_local"
            if not bridge_type:
                continue
            active.append({
                "id": profile_id,
                "bridge_key": bridge_key,
                "bridge_cfg": bridge_cfg,
                "bridge_type": bridge_type,
                "context_source_id": str(profile.get("context_source_id") or ""),
            })
        return active

    def _build_client(self, profile: dict) -> Any:
        """Instantiate a MetaApiClient for the given profile."""
        from vanguard.execution.metaapi_client import MetaApiClient
        cfg       = profile["bridge_cfg"]
        account_id = _resolve_env(cfg.get("account_id", ""))
        api_token  = _resolve_env(cfg.get("api_token", ""))
        base_url   = _resolve_env(cfg.get("base_url", ""))
        timeout_s  = int(cfg.get("timeout_s", 30))
        return MetaApiClient(
            account_id=account_id,
            api_token=api_token,
            base_url=base_url,
            timeout_s=timeout_s,
        )

    def _get_policy_for_profile(self, profile: dict) -> Optional[dict]:
        """
        Return the resolved policy dict for a bridge profile.

        Reads bridge_cfg["policy_id"] and looks it up in config["policy_templates"].
        Returns None if not found (auto-close will be skipped for this profile).
        """
        bridge_cfg = profile.get("bridge_cfg") or {}
        policy_id  = str(bridge_cfg.get("policy_id") or "").strip()
        if not policy_id:
            logger.debug(
                "[lifecycle_daemon] profile=%s has no policy_id in bridge_cfg — skipping auto-close",
                profile["id"],
            )
            return None
        templates = self.config.get("policy_templates") or {}
        policy = templates.get(policy_id)
        if policy is None:
            logger.warning(
                "[lifecycle_daemon] policy_templates[%r] not found — skipping auto-close for %s",
                policy_id, profile["id"],
            )
            return None
        return dict(policy)

    # ── DWX helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _dwx_positions_to_metaapi_format(dwx_positions: dict) -> list[dict]:
        """
        Translate DWX open_orders dict to MetaApi positions list format.

        MetaApi format: broker_position_id, symbol, side, qty, entry_price,
                        current_sl, current_tp, opened_at_utc, unrealized_pnl
        """
        result = []
        for ticket, pos in dwx_positions.items():
            side = "LONG" if str(pos.get("type", "buy")).lower() == "buy" else "SHORT"
            open_time = pos.get("open_time")
            opened_at = ""
            if open_time is not None:
                try:
                    opened_at = open_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                except AttributeError:
                    opened_at = str(open_time)
            result.append({
                "broker_position_id": str(ticket),
                "symbol":             str(pos.get("symbol", "")),
                "side":               side,
                "qty":                float(pos.get("lots") or 0),
                "entry_price":        float(pos.get("open_price") or 0),
                "current_sl":         float(pos["sl"]) if pos.get("sl") else None,
                "current_tp":         float(pos["tp"]) if pos.get("tp") else None,
                "opened_at_utc":      opened_at,
                "unrealized_pnl":     float(pos.get("pnl") or 0),
            })
        return result

    # ── Signal-driven exit rules ──────────────────────────────────────────

    def check_exit_rules(self, position: dict, current_signal: dict) -> Optional[str]:
        """
        Evaluate signal-driven exit rules for a single open position.

        Args:
            position: dict with keys: ticket, symbol, type (buy/sell),
                      open_price, pnl, sl, open_time (datetime | None)
            current_signal: dict with keys: direction, streak, edge_score

        Returns:
            None = keep position
            'CLOSE_SIGNAL_FLIP' = model direction flipped
            'CLOSE_MAX_HOLD'    = exceeded max hold time
            'MODIFY_SL_BREAKEVEN' = streak dropped; move SL to entry (only when in profit)
        """
        from vanguard.config.runtime_config import get_runtime_config  # noqa: PLC0415
        rules = get_runtime_config().get("exit_rules", {})
        if not rules.get("enabled", False):
            return None

        min_hold    = int(rules.get("min_hold_minutes", 120))
        max_hold    = int(rules.get("max_hold_minutes", 240))
        min_streak  = int(rules.get("min_streak_for_hold", 3))

        open_time = position.get("open_time")
        hold_minutes: float = 9999.0
        if open_time is not None:
            try:
                now = datetime.now(timezone.utc)
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                hold_minutes = (now - open_time).total_seconds() / 60.0
            except Exception:
                pass

        # Rule 1: Never exit before min hold time.
        if hold_minutes < min_hold:
            return None

        # Rule 2: Close on signal flip.
        if rules.get("close_on_signal_flip", False):
            pos_dir = "LONG" if str(position.get("type", "buy")).lower() == "buy" else "SHORT"
            sig_dir = str(current_signal.get("direction") or pos_dir).upper()
            if sig_dir != pos_dir and sig_dir in ("LONG", "SHORT"):
                return "CLOSE_SIGNAL_FLIP"

        # Rule 3: Move SL to breakeven when streak drops (only when in profit).
        if rules.get("move_sl_to_breakeven_on_streak_drop", False):
            streak     = int(current_signal.get("streak") or 99)
            pnl        = float(position.get("pnl") or 0)
            current_sl = float(position.get("sl") or 0)
            entry      = float(position.get("open_price") or 0)
            # Only act if: streak below threshold, position profitable, SL not already at BE
            if streak < min_streak and pnl > 0 and abs(current_sl - entry) > 1e-5:
                return "MODIFY_SL_BREAKEVEN"

        # Rule 4: Close after max hold time.
        if hold_minutes > max_hold:
            return "CLOSE_MAX_HOLD"

        return None

    def execute_exit(self, position: dict, reason: str) -> None:
        """
        Execute the exit decision via DWX adapter.

        CLOSE_SIGNAL_FLIP / CLOSE_MAX_HOLD → close full position.
        MODIFY_SL_BREAKEVEN                 → set SL to entry price.
        """
        if self._mt5 is None or not self._mt5.is_connected():
            logger.warning("[EXIT] DWX not available — cannot execute exit for %s", position.get("symbol"))
            return

        ticket = str(position.get("ticket", ""))
        symbol = str(position.get("symbol", ""))
        pnl    = float(position.get("pnl") or 0)

        if reason in ("CLOSE_SIGNAL_FLIP", "CLOSE_MAX_HOLD"):
            success = self._mt5.close_order(ticket)
            logger.info(
                "[EXIT] %s ticket=%s symbol=%s pnl=%.2f success=%s",
                reason, ticket, symbol, pnl, success,
            )
            self._send_telegram(
                f"EXIT {reason} ticket={ticket} symbol={symbol} pnl={pnl:.2f} ok={success}"
            )

        elif reason == "MODIFY_SL_BREAKEVEN":
            entry  = float(position.get("open_price") or 0)
            new_sl = round(entry, 5)
            success = self._mt5.modify_order(ticket, sl=new_sl)
            logger.info(
                "[SL→BE] ticket=%s symbol=%s entry=%.5f new_sl=%.5f success=%s",
                ticket, symbol, entry, new_sl, success,
            )
            self._send_telegram(
                f"SL→BE ticket={ticket} symbol={symbol} new_sl={new_sl:.5f} ok={success}"
            )

    def get_current_signal(self, symbol: str, profile_id: str = "") -> dict:
        """
        Read the latest V5 selection entry for symbol/profile.

        Returns dict with keys: direction, edge_score, streak (defaults if not found).
        """
        try:
            con = sqlite3.connect(self.db_path)
            if profile_id:
                row = con.execute(
                    """
                    SELECT direction,
                           COALESCE(json_extract(metrics_json, '$.normalized_edge'), 0.5) AS edge_score,
                           COALESCE(direction_streak, 0) AS streak
                    FROM vanguard_v5_selection
                    WHERE symbol = ? AND profile_id = ?
                    ORDER BY cycle_ts_utc DESC
                    LIMIT 1
                    """,
                    (symbol, profile_id),
                ).fetchone()
            else:
                row = con.execute(
                    """
                    SELECT direction,
                           COALESCE(json_extract(metrics_json, '$.normalized_edge'), 0.5) AS edge_score,
                           COALESCE(direction_streak, 0) AS streak
                    FROM vanguard_v5_selection
                    WHERE symbol = ?
                    ORDER BY cycle_ts_utc DESC
                    LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()
            con.close()
            if row:
                return {
                    "direction":  str(row[0] or ""),
                    "edge_score": float(row[1] or 0.5),
                    "streak":     int(row[2] or 0),
                }
        except Exception as exc:
            logger.debug("[lifecycle_daemon] get_current_signal(%s) failed: %s", symbol, exc)
        return {"direction": "", "edge_score": 0.5, "streak": 99}

    def _check_breaker_reset(self) -> None:
        """Check for the flag file written by reset_autoclose_breaker.sh and reset if present."""
        if self._BREAKER_RESET_FILE.exists():
            try:
                self._BREAKER_RESET_FILE.unlink()
            except OSError:
                pass
            self.auto_close_manager.reset_breaker()
            self._send_telegram("AUTO_CLOSE_CIRCUIT_BREAKER_RESET by operator flag file")
            logger.info("[lifecycle_daemon] Auto-close breaker reset via flag file")

    # ── Telegram helper ───────────────────────────────────────────────────

    def _send_telegram(self, message: str) -> None:
        if self.telegram_fn:
            try:
                self.telegram_fn(message)
            except Exception as exc:
                logger.warning("[lifecycle_daemon] Telegram alert failed: %s", exc)
        else:
            logger.info("[lifecycle_daemon] [telegram] %s", message)

    # ── Error handling ────────────────────────────────────────────────────

    def _on_error(self, profile_id: str, exc: Exception) -> None:
        """Handle unexpected exception in _sync_profile — log and continue."""
        logger.error(
            "[lifecycle_daemon] profile=%s iteration=%d exception: %s\n%s",
            profile_id, self._iteration, exc, traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Entrypoint (python3 -m vanguard.execution.lifecycle_daemon)
# ---------------------------------------------------------------------------

async def _run(interval_s: int, db_path: str) -> None:
    from vanguard.config.runtime_config import get_runtime_config
    config = get_runtime_config()
    daemon = LifecycleDaemon(config=config, db_path=db_path, interval_s=interval_s)
    signal.signal(signal.SIGTERM, daemon.handle_shutdown)
    signal.signal(signal.SIGINT,  daemon.handle_shutdown)
    await daemon.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Vanguard lifecycle daemon (Phase 3e).")
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Sync interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--db-path", type=str, default="",
        help="Path to SQLite DB (default: from VANGUARD_SOURCE_DB env or config)",
    )
    args = parser.parse_args()

    db_path = args.db_path or os.environ.get("VANGUARD_SOURCE_DB", "")
    if not db_path:
        db_path = str(_REPO_ROOT / "data" / "vanguard_universe.db")

    asyncio.run(_run(interval_s=args.interval, db_path=db_path))


if __name__ == "__main__":
    main()
