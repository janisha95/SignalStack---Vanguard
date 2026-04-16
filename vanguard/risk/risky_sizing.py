from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from vanguard.config.runtime_config import get_model_config
from vanguard.risk.base import PolicyDecision, make_approved, make_blocked
from vanguard.risk.sizing import size_crypto_spread_aware, size_forex_pips


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


def _round_qty(qty: float) -> float:
    return round(float(qty), 6)


def _pip_size_for_symbol(symbol: str) -> float:
    sym = str(symbol or "").upper().replace("/", "").strip()
    return 0.01 if sym.endswith("JPY") else 0.0001


@lru_cache(maxsize=None)
def _active_model_meta(asset_class: str) -> dict[str, Any]:
    try:
        runtime_model_cfg = get_model_config(asset_class)
        model_dir = Path(str(runtime_model_cfg.get("model_dir") or "")).expanduser()
        meta_file = str(runtime_model_cfg.get("meta_file") or "").strip()
        if not meta_file:
            return {}
        meta_path = model_dir / meta_file
        if not meta_path.exists():
            return {}
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


def _horizon_bars_from_meta(meta: dict[str, Any]) -> int | None:
    direct_value = _safe_int(meta.get("model_horizon_bars"), 0)
    if direct_value > 0:
        return direct_value

    training_data = meta.get("training_data") or {}
    training_value = _safe_int(training_data.get("horizon_bars"), 0)
    if training_value > 0:
        return training_value

    note = str(training_data.get("target_note") or meta.get("notes") or "")
    match = re.search(r"horizon\s*=?\s*(\d+)\s*bars", note, re.IGNORECASE)
    if match:
        parsed_value = _safe_int(match.group(1), 0)
        if parsed_value > 0:
            return parsed_value
    return None


def _resolve_model_horizon_bars(asset_class: str, sizing_cfg: dict[str, Any]) -> int:
    meta_value = _horizon_bars_from_meta(_active_model_meta(asset_class))
    if meta_value and meta_value > 0:
        return meta_value
    return max(_safe_int(sizing_cfg.get("model_horizon_bars"), 12), 1)


def _move_abs_from_candidate(asset_class: str, symbol: str, entry: float, candidate: dict[str, Any]) -> tuple[float, float]:
    if asset_class == "forex":
        pip_size = _pip_size_for_symbol(symbol)
        return (
            _safe_float(candidate.get("predicted_move_pips")) * pip_size,
            _safe_float(candidate.get("after_cost_pips")) * pip_size,
        )
    return (
        entry * (_safe_float(candidate.get("predicted_move_bps")) / 10_000.0),
        entry * (_safe_float(candidate.get("after_cost_bps")) / 10_000.0),
    )


def _loss_used_ratio(current_pct: float, limit_pct: float) -> float:
    if limit_pct <= 0:
        return 0.0
    current_loss_pct = abs(min(current_pct, 0.0))
    return max(0.0, current_loss_pct / limit_pct)


def _headroom_result(
    *,
    free_margin: float,
    account_equity: float,
    account_state: dict[str, Any],
    policy: dict[str, Any],
    same_bucket_open_count: int,
    correlated_open_count: int,
) -> tuple[float, str | None, dict[str, Any]]:
    drawdown_rules = policy.get("drawdown_rules") or {}
    position_limits = policy.get("position_limits") or {}
    free_margin_ratio = (free_margin / account_equity) if account_equity > 0 else 0.0
    daily_used_ratio = _loss_used_ratio(
        _safe_float(account_state.get("daily_pnl_pct")),
        _safe_float(drawdown_rules.get("daily_loss_pause_pct")),
    )
    trailing_used_ratio = _loss_used_ratio(
        _safe_float(account_state.get("trailing_dd_pct")),
        _safe_float(drawdown_rules.get("trailing_drawdown_pause_pct")),
    )
    open_positions = list(account_state.get("open_positions") or [])
    open_position_count = len(open_positions)
    max_open_positions = max(_safe_int(position_limits.get("max_open_positions"), 0), 0)

    detail = {
        "free_margin_ratio": free_margin_ratio,
        "daily_drawdown_used_ratio": daily_used_ratio,
        "trailing_drawdown_used_ratio": trailing_used_ratio,
        "open_position_count": open_position_count,
        "max_open_positions": max_open_positions,
        "same_bucket_open_count": same_bucket_open_count,
        "correlated_open_count": correlated_open_count,
    }

    severe = (
        free_margin_ratio < 0.25
        or daily_used_ratio >= 0.9
        or trailing_used_ratio >= 0.9
        or (max_open_positions > 0 and open_position_count >= max_open_positions)
        or correlated_open_count >= 3
    )
    if severe:
        return 0.0, "BLOCKED_ACCOUNT_HEADROOM", detail

    elevated = (
        free_margin_ratio < 0.5
        or daily_used_ratio >= 0.75
        or trailing_used_ratio >= 0.75
        or correlated_open_count >= 2
        or same_bucket_open_count >= 2
        or (max_open_positions > 0 and open_position_count >= max(max_open_positions - 1, 1))
    )
    if elevated:
        return 0.5, None, detail

    moderate = (
        free_margin_ratio < 0.8
        or daily_used_ratio >= 0.5
        or trailing_used_ratio >= 0.5
        or correlated_open_count >= 1
        or same_bucket_open_count >= 1
    )
    if moderate:
        return 0.75, None, detail

    return 1.0, None, detail


def _opportunity_result(
    *,
    after_cost_abs: float,
    stop_distance: float,
    sizing_cfg: dict[str, Any],
) -> tuple[float, str | None, dict[str, Any]]:
    min_rr = max(_safe_float(sizing_cfg.get("min_automation_rr_multiple"), 1.0), 0.1)
    target_rr = max(_safe_float(sizing_cfg.get("target_rr_multiple"), 2.0), min_rr)
    mid_rr = max(_safe_float(sizing_cfg.get("mid_opportunity_rr_multiple"), 1.5), min_rr)
    floor_rr = max(_safe_float(sizing_cfg.get("floor_opportunity_rr_multiple"), min_rr), min_rr)

    if stop_distance <= 0:
        return 0.0, "BLOCKED_INVALID_STOP_DISTANCE", {"after_cost_rr_multiple": 0.0}

    after_cost_rr = after_cost_abs / stop_distance if after_cost_abs > 0 else 0.0
    detail = {
        "after_cost_rr_multiple": after_cost_rr,
        "min_automation_rr_multiple": min_rr,
        "target_rr_multiple": target_rr,
        "mid_opportunity_rr_multiple": mid_rr,
        "floor_opportunity_rr_multiple": floor_rr,
    }

    if after_cost_rr < min_rr:
        return 0.0, "BLOCKED_SUB_1R_AFTER_COST", detail
    if after_cost_rr >= target_rr:
        return 1.0, None, detail
    if after_cost_rr >= mid_rr:
        return 0.75, None, detail
    return 0.5, None, detail


def _resolve_notional_cap(
    *,
    entry_price: float,
    account_equity: float,
    contract_size: float,
    sizing_cfg: dict[str, Any],
    policy: dict[str, Any],
    asset_class: str,
) -> tuple[float, dict[str, Any]]:
    max_notional_pct = _safe_float(sizing_cfg.get("max_notional_pct"), 0.05)
    leverage_cfg = policy.get("leverage") or {}
    leverage = max(_safe_float(leverage_cfg.get(asset_class), 1.0), 1.0)
    apply_leverage = bool(sizing_cfg.get("apply_leverage_to_notional_cap", True))
    buying_power_multiplier = leverage if apply_leverage else 1.0
    max_notional = max_notional_pct * account_equity * buying_power_multiplier
    notional_qty = max_notional / (entry_price * contract_size) if entry_price > 0 and contract_size > 0 else 0.0
    detail = {
        "max_notional_pct": max_notional_pct,
        "leverage": leverage,
        "apply_leverage_to_notional_cap": apply_leverage,
        "max_notional": max_notional,
        "notional_qty_cap": notional_qty,
    }
    return notional_qty, detail


def _model_exit_geometry(
    *,
    asset_class: str,
    symbol: str,
    side: str,
    entry_price: float,
    atr: float,
    spread_abs: float,
    predicted_move_abs: float,
    after_cost_abs: float,
    sizing_cfg: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any], list[str]] | tuple[None, dict[str, Any], list[str]]:
    notes: list[str] = []
    flags: dict[str, Any] = {
        "use_model_horizon_exits": bool(sizing_cfg.get("use_model_horizon_exits", True)),
        "sub_2r_model_cap": False,
        "reason_codes": [],
    }
    if not flags["use_model_horizon_exits"]:
        notes.append("model_horizon_exits_disabled")
        return None, flags, notes
    if entry_price <= 0:
        notes.append("invalid_entry_price")
        return None, flags, notes

    target_rr_multiple = max(_safe_float(sizing_cfg.get("target_rr_multiple"), 2.0), 0.1)
    min_spread_multiple = max(_safe_float(sizing_cfg.get("min_spread_multiple"), 1.5), 0.0)
    min_atr_stop_multiple = max(_safe_float(sizing_cfg.get("min_atr_stop_multiple"), 0.25), 0.0)
    horizon_bars = _resolve_model_horizon_bars(asset_class, sizing_cfg)

    if asset_class == "forex":
        min_stop_floor_abs = _safe_float(sizing_cfg.get("min_stop_floor_pips"), 3.0) * _pip_size_for_symbol(symbol)
    else:
        min_stop_floor_abs = entry_price * (_safe_float(sizing_cfg.get("min_stop_floor_bps"), 8.0) / 10_000.0)

    stop_distance = max(
        min_stop_floor_abs,
        spread_abs * min_spread_multiple,
        float(atr) * min_atr_stop_multiple if atr > 0 else 0.0,
    )

    target_tp_abs = stop_distance * target_rr_multiple
    if asset_class == "forex":
        tp_distance = target_tp_abs
    else:
        tp_capture_fraction = max(_safe_float(sizing_cfg.get("tp_capture_fraction"), 0.9), 0.1)
        tp_cap_abs = after_cost_abs if after_cost_abs > 0 else predicted_move_abs * tp_capture_fraction
        tp_cap_abs = min(tp_cap_abs, predicted_move_abs * tp_capture_fraction)
        tp_distance = min(target_tp_abs, tp_cap_abs) if tp_cap_abs > 0 else target_tp_abs

    if tp_distance <= 0 or stop_distance <= 0:
        notes.append("non_positive_exit_geometry")
        return None, flags, notes

    achieved_rr = tp_distance / stop_distance if stop_distance > 0 else 0.0
    if asset_class != "forex" and tp_distance < target_tp_abs:
        flags["sub_2r_model_cap"] = True
        flags["reason_codes"].append("SUB_2R_MODEL_CAP")
    min_automation_rr = max(_safe_float(sizing_cfg.get("min_automation_rr_multiple"), 1.0), 0.1)
    flags["min_automation_rr_multiple"] = min_automation_rr
    flags["achieved_rr_multiple"] = achieved_rr
    flags["model_horizon_bars"] = horizon_bars

    tp_vs_predicted_move_ratio = 0.0
    if predicted_move_abs > 0:
        tp_vs_predicted_move_ratio = tp_distance / predicted_move_abs
    flags["tp_vs_predicted_move_ratio"] = tp_vs_predicted_move_ratio

    if side == "LONG":
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + tp_distance
    else:
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - tp_distance

    notes.extend(
        [
            f"model_horizon_bars={horizon_bars}",
            f"predicted_move_abs={predicted_move_abs:.6f}",
            f"after_cost_abs={after_cost_abs:.6f}",
            f"spread_abs={spread_abs:.6f}",
            f"stop_distance={stop_distance:.6f}",
            f"tp_distance={tp_distance:.6f}",
            f"target_rr_multiple={target_rr_multiple:.3f}",
            f"achieved_rr_multiple={achieved_rr:.3f}",
        ]
    )
    if predicted_move_abs > 0:
        notes.append(f"tp_vs_predicted_move_ratio={tp_vs_predicted_move_ratio:.4f}")
        if asset_class == "forex" and tp_vs_predicted_move_ratio < 0.1:
            notes.append("tp_vs_predicted_move_ratio_low")
    return {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "stop_distance": stop_distance,
        "tp_distance": tp_distance,
        "achieved_rr_multiple": achieved_rr,
    }, flags, notes


def _build_blocked(
    reason: str,
    side: str,
    notes: list[str],
    checks: dict[str, Any],
    sizing_inputs: dict[str, Any],
    sizing_outputs: dict[str, Any] | None = None,
) -> PolicyDecision:
    return replace(
        make_blocked(reason, notes, side),
        checks_json=checks,
        sizing_inputs_json=sizing_inputs,
        sizing_outputs_json=sizing_outputs or {},
    )


def _build_approved(
    qty: float,
    sl: float,
    tp: float,
    side: str,
    method: str,
    notes: list[str],
    checks: dict[str, Any],
    sizing_inputs: dict[str, Any],
    sizing_outputs: dict[str, Any],
) -> PolicyDecision:
    return replace(
        make_approved(qty, sl, tp, side, method, notes),
        checks_json=checks,
        sizing_inputs_json=sizing_inputs,
        sizing_outputs_json=sizing_outputs,
    )


def evaluate_risky_sizing(
    *,
    candidate: dict[str, Any],
    profile: dict[str, Any],
    policy: dict[str, Any],
    account_state: dict[str, Any],
    sizing_cfg: dict[str, Any],
) -> PolicyDecision:
    asset_class = str(candidate.get("asset_class") or "").lower().strip()
    side = str(candidate.get("direction") or "").upper().strip()
    symbol = str(candidate.get("symbol") or "").upper().strip()
    entry = _safe_float(candidate.get("entry_price") or candidate.get("entry"))
    atr = _safe_float(candidate.get("intraday_atr") or candidate.get("atr"))
    economics_state = str(candidate.get("economics_state") or "").upper().strip()

    live_equity = _safe_float(candidate.get("live_account_equity"))
    live_free_margin = _safe_float(candidate.get("live_free_margin"))
    live_leverage = _safe_float(candidate.get("live_leverage"))
    fallback_equity = _safe_float(account_state.get("equity"))
    account_equity = live_equity or fallback_equity
    free_margin = live_free_margin or fallback_equity

    checks: dict[str, Any] = {
        "asset_class": asset_class,
        "symbol": symbol,
        "economics_state": economics_state,
        "account_truth_source": "context_account_latest" if live_equity > 0 else "vanguard_account_state",
        "free_margin_truth_source": "context_account_latest" if live_free_margin > 0 else "vanguard_account_state",
    }
    sizing_inputs: dict[str, Any] = {
        "entry_price": entry,
        "atr": atr,
        "live_account_equity": live_equity,
        "live_free_margin": live_free_margin,
        "live_leverage": live_leverage,
        "account_state_equity_fallback": fallback_equity,
        "effective_account_equity": account_equity,
        "effective_free_margin": free_margin,
        "economics_state": economics_state,
        "after_cost_pips": candidate.get("after_cost_pips"),
        "after_cost_bps": candidate.get("after_cost_bps"),
        "predicted_move_pips": candidate.get("predicted_move_pips"),
        "predicted_move_bps": candidate.get("predicted_move_bps"),
        "risk_profile_id": policy.get("_risk_profile_id"),
    }

    if economics_state == "FAIL":
        return _build_blocked(
            "BLOCKED_ECONOMICS_FAIL",
            side,
            ["economics_state=FAIL"],
            checks,
            sizing_inputs,
        )
    if economics_state == "WEAK":
        return _build_blocked(
            "BLOCKED_ECONOMICS_WEAK",
            side,
            ["economics_state=WEAK"],
            checks,
            sizing_inputs,
        )
    if economics_state != "PASS":
        return _build_blocked(
            "BLOCKED_ECONOMICS_UNKNOWN",
            side,
            [f"economics_state={economics_state or 'missing'}"],
            checks,
            sizing_inputs,
        )

    if account_equity <= 0 or free_margin <= 0:
        return _build_blocked(
            "BLOCKED_ACCOUNT_TRUTH_MISSING",
            side,
            [
                f"live_account_equity={live_equity}",
                f"live_free_margin={live_free_margin}",
                f"account_state_equity_fallback={fallback_equity}",
            ],
            checks,
            sizing_inputs,
        )

    if entry <= 0:
        return _build_blocked(
            "BLOCKED_INVALID_PRICE_OR_ATR",
            side,
            [f"entry={entry}"],
            checks,
            sizing_inputs,
        )

    if asset_class == "forex":
        return _evaluate_forex(
            candidate=candidate,
            profile=profile,
            policy=policy,
            sizing_cfg=sizing_cfg,
            account_equity=account_equity,
            free_margin=free_margin,
            account_state=account_state,
            checks=checks,
            sizing_inputs=sizing_inputs,
        )

    if asset_class == "crypto":
        return _evaluate_crypto(
            candidate=candidate,
            profile=profile,
            policy=policy,
            sizing_cfg=sizing_cfg,
            account_equity=account_equity,
            free_margin=free_margin,
            account_state=account_state,
            checks=checks,
            sizing_inputs=sizing_inputs,
        )

    return _build_blocked(
        "BLOCKED_SCOPE",
        side,
        [f"unsupported asset_class={asset_class}"],
        checks,
        sizing_inputs,
    )


def _evaluate_forex(
    *,
    candidate: dict[str, Any],
    profile: dict[str, Any],
    policy: dict[str, Any],
    sizing_cfg: dict[str, Any],
    account_equity: float,
    free_margin: float,
    account_state: dict[str, Any],
    checks: dict[str, Any],
    sizing_inputs: dict[str, Any],
) -> PolicyDecision:
    symbol = str(candidate.get("symbol") or "").upper().strip()
    side = str(candidate.get("direction") or "").upper().strip()
    entry = _safe_float(candidate.get("entry_price") or candidate.get("entry"))
    atr = _safe_float(candidate.get("intraday_atr") or candidate.get("atr"))
    spread_pips = candidate.get("spread_pips")
    bid = candidate.get("bid")
    ask = candidate.get("ask")
    bid = _safe_float(bid) if bid is not None else None
    ask = _safe_float(ask) if ask is not None else None
    trade_allowed = candidate.get("spec_trade_allowed")
    min_lot = _safe_float(candidate.get("spec_min_lot"), 0.01)
    max_lot = _safe_float(candidate.get("spec_max_lot"), 0.0)
    lot_step = _safe_float(candidate.get("spec_lot_step"), 0.01)
    same_currency_open_count = _safe_int(candidate.get("same_currency_open_count"))
    correlated_open_count = _safe_int(candidate.get("correlated_open_count"))

    checks.update(
        {
            "spread_pips_present": spread_pips is not None,
            "spec_trade_allowed": trade_allowed,
            "same_currency_open_count": same_currency_open_count,
            "correlated_open_count": correlated_open_count,
        }
    )
    sizing_inputs.update(
        {
            "spread_pips": spread_pips,
            "bid": bid,
            "ask": ask,
            "min_lot": min_lot,
            "max_lot": max_lot,
            "lot_step": lot_step,
            "same_currency_open_count": same_currency_open_count,
            "correlated_open_count": correlated_open_count,
        }
    )

    if spread_pips is None:
        return _build_blocked(
            "BLOCKED_SPREAD_MISSING",
            side,
            ["live spread_pips missing"],
            checks,
            sizing_inputs,
        )
    if trade_allowed in (0, False):
        return _build_blocked(
            "BLOCKED_SYMBOL_NOT_TRADEABLE",
            side,
            ["spec trade_allowed=0"],
            checks,
            sizing_inputs,
        )

    entry_price = ask if side == "LONG" and ask is not None else bid if side == "SHORT" and bid is not None else entry
    predicted_move_abs, after_cost_abs = _move_abs_from_candidate("forex", symbol, entry_price, candidate)
    spread_abs = _safe_float(spread_pips) * _pip_size_for_symbol(symbol)
    geometry, flags, geom_notes = _model_exit_geometry(
        asset_class="forex",
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        atr=atr,
        spread_abs=spread_abs,
        predicted_move_abs=predicted_move_abs,
        after_cost_abs=after_cost_abs,
        sizing_cfg=sizing_cfg,
    )
    if geometry is None:
        result = size_forex_pips(
            entry=entry_price,
            atr=atr,
            side=side,
            policy_forex=sizing_cfg,
            symbol=symbol,
            account_equity=account_equity,
        )
        if not result["approved"]:
            return _build_blocked(
                result["reject_reason"] or "BLOCKED_NO_SIZING_RULE",
                side,
                list(result["notes"]) + geom_notes,
                checks,
                sizing_inputs,
            )
        stop_loss = _safe_float(result["sl_price"])
        take_profit = _safe_float(result["tp_price"])
        stop_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        rr_multiple = tp_distance / stop_distance if stop_distance > 0 else 0.0
        base_notes = list(result["notes"]) + geom_notes
        checks["used_legacy_exit_geometry"] = True
    else:
        stop_loss = _safe_float(geometry["stop_loss"])
        take_profit = _safe_float(geometry["take_profit"])
        stop_distance = _safe_float(geometry["stop_distance"])
        tp_distance = _safe_float(geometry["tp_distance"])
        rr_multiple = _safe_float(geometry["achieved_rr_multiple"])
        base_notes = geom_notes
        checks["used_legacy_exit_geometry"] = False
    checks["sub_2r_model_cap"] = bool(flags.get("sub_2r_model_cap"))
    checks["model_horizon_bars"] = _safe_int(flags.get("model_horizon_bars"), 12)
    checks["tp_vs_predicted_move_ratio"] = _safe_float(flags.get("tp_vs_predicted_move_ratio"), 0.0)
    if flags.get("reason_codes"):
        checks["reason_codes"] = list(flags["reason_codes"])
    if rr_multiple < max(_safe_float(flags.get("min_automation_rr_multiple"), 1.0), 0.1):
        notes = base_notes + [f"achieved_rr_multiple={rr_multiple:.4f} < automation_floor"]
        return _build_blocked(
            "BLOCKED_SUB_1R_AFTER_COST",
            side,
            notes,
            {**checks, "after_cost_rr_multiple": rr_multiple},
            sizing_inputs,
            {
                "entry_ref_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "stop_distance": stop_distance,
                "tp_distance": tp_distance,
                "rr_multiple": rr_multiple,
                "tp_vs_predicted_move_ratio": _safe_float(flags.get("tp_vs_predicted_move_ratio"), 0.0),
            },
        )

    headroom_multiplier, headroom_reason, headroom_detail = _headroom_result(
        free_margin=free_margin,
        account_equity=account_equity,
        account_state=account_state,
        policy=policy,
        same_bucket_open_count=same_currency_open_count,
        correlated_open_count=correlated_open_count,
    )
    checks["headroom_detail"] = headroom_detail
    if headroom_reason:
        return _build_blocked(
            headroom_reason,
            side,
            base_notes + [f"headroom_block={headroom_reason}"],
            checks,
            sizing_inputs,
        )

    opportunity_multiplier, opportunity_reason, opportunity_detail = _opportunity_result(
        after_cost_abs=after_cost_abs,
        stop_distance=stop_distance,
        sizing_cfg=sizing_cfg,
    )
    checks["opportunity_detail"] = opportunity_detail
    if opportunity_reason:
        return _build_blocked(
            opportunity_reason,
            side,
            base_notes + [f"opportunity_block={opportunity_reason}"],
            checks,
            sizing_inputs,
            {
                "entry_ref_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "stop_distance": stop_distance,
                "tp_distance": tp_distance,
                "rr_multiple": rr_multiple,
                "tp_vs_predicted_move_ratio": _safe_float(flags.get("tp_vs_predicted_move_ratio"), 0.0),
            },
        )

    pip_values = sizing_cfg.get("pip_value_usd_per_standard_lot") or {}
    pip_value = _safe_float(pip_values.get(symbol) or pip_values.get("default"), 10.0)
    stop_pips = stop_distance / _pip_size_for_symbol(symbol) if stop_distance > 0 else 0.0
    base_risk_dollars = account_equity * _safe_float(sizing_cfg.get("risk_per_trade_pct"), 0.005)
    effective_risk_dollars = base_risk_dollars * headroom_multiplier * opportunity_multiplier
    checks["headroom_multiplier"] = headroom_multiplier
    checks["opportunity_multiplier"] = opportunity_multiplier
    qty = effective_risk_dollars / (stop_pips * pip_value) if stop_pips > 0 and pip_value > 0 else 0.0
    if min_lot > 0 and 0 < qty < min_lot:
        return _build_blocked(
            "BLOCKED_SIZE_BELOW_MIN_LOT",
            side,
            [f"raw_qty={qty:.6f} min_lot={min_lot}"],
            checks,
            sizing_inputs,
            {"base_qty": qty, "effective_risk_dollars": effective_risk_dollars},
        )
    if max_lot > 0:
        qty = min(qty, max_lot)
    qty = _floor_to_step(qty, lot_step)
    qty = _round_qty(qty)

    if qty <= 0 or qty < min_lot:
        return _build_blocked(
            "BLOCKED_SIZE_INVALID",
            side,
            [f"qty={qty} min_lot={min_lot}"],
            checks,
            sizing_inputs,
            {"base_qty": qty},
        )

    risk_dollars = qty * stop_pips * pip_value
    risk_pct = risk_dollars / account_equity if account_equity > 0 else 0.0
    contract_size = _safe_float(candidate.get("spec_contract_size"), 100000.0)
    position_notional = qty * contract_size * entry_price

    sizing_outputs = {
        "base_qty": qty,
        "final_qty": qty,
        "base_risk_dollars": base_risk_dollars,
        "effective_risk_dollars": effective_risk_dollars,
        "entry_ref_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "stop_distance": stop_distance,
        "tp_distance": tp_distance,
        "rr_multiple": rr_multiple,
        "risk_dollars": risk_dollars,
        "risk_pct": risk_pct,
        "position_notional": position_notional,
        "tp_vs_predicted_move_ratio": _safe_float(flags.get("tp_vs_predicted_move_ratio"), 0.0),
    }
    notes = base_notes + [
        f"lot_clip min={min_lot} max={max_lot} step={lot_step}",
        f"stop_pips={stop_pips:.4f}",
        f"pip_value={pip_value:.4f}",
        f"headroom_multiplier={headroom_multiplier:.2f}",
        f"opportunity_multiplier={opportunity_multiplier:.2f}",
        f"risk_dollars={risk_dollars:.4f}",
    ]
    return _build_approved(
        qty,
        stop_loss,
        take_profit,
        side,
        "risky_forex_context_v1",
        notes,
        checks,
        sizing_inputs,
        sizing_outputs,
    )


def _evaluate_crypto(
    *,
    candidate: dict[str, Any],
    profile: dict[str, Any],
    policy: dict[str, Any],
    sizing_cfg: dict[str, Any],
    account_equity: float,
    free_margin: float,
    account_state: dict[str, Any],
    checks: dict[str, Any],
    sizing_inputs: dict[str, Any],
) -> PolicyDecision:
    symbol = str(candidate.get("symbol") or "").upper().strip()
    side = str(candidate.get("direction") or "").upper().strip()
    bid = candidate.get("bid")
    ask = candidate.get("ask")
    bid = _safe_float(bid) if bid is not None else None
    ask = _safe_float(ask) if ask is not None else None
    entry = _safe_float(candidate.get("entry_price") or candidate.get("entry"))
    atr = _safe_float(candidate.get("intraday_atr") or candidate.get("atr"))
    spread_bps = candidate.get("spread_bps")
    trade_allowed = candidate.get("trade_allowed")
    min_qty = _safe_float(candidate.get("min_qty") or candidate.get("spec_min_lot"), 0.0)
    max_qty = _safe_float(candidate.get("max_qty") or candidate.get("spec_max_lot"), 0.0)
    qty_step = _safe_float(candidate.get("qty_step") or candidate.get("spec_lot_step"), 0.0)
    same_symbol_open_count = _safe_int(candidate.get("same_symbol_open_count"))
    correlated_open_count = _safe_int(candidate.get("correlated_open_count"))

    checks.update(
        {
            "spread_bps_present": spread_bps is not None,
            "trade_allowed": trade_allowed,
            "same_symbol_open_count": same_symbol_open_count,
            "correlated_open_count": correlated_open_count,
        }
    )
    sizing_inputs.update(
        {
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "min_qty": min_qty,
            "max_qty": max_qty,
            "qty_step": qty_step,
            "same_symbol_open_count": same_symbol_open_count,
            "correlated_open_count": correlated_open_count,
        }
    )

    if bid is None or ask is None or spread_bps is None:
        return _build_blocked(
            "BLOCKED_SPREAD_MISSING",
            side,
            ["live bid/ask or spread_bps missing"],
            checks,
            sizing_inputs,
        )
    if trade_allowed in (0, False):
        return _build_blocked(
            "BLOCKED_SYMBOL_NOT_TRADEABLE",
            side,
            ["symbol_state trade_allowed=0"],
            checks,
            sizing_inputs,
        )

    entry_price = ask if side == "LONG" else bid
    predicted_move_abs, after_cost_abs = _move_abs_from_candidate("crypto", symbol, entry_price, candidate)
    spread_abs = abs(ask - bid)
    geometry, flags, geom_notes = _model_exit_geometry(
        asset_class="crypto",
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        atr=atr,
        spread_abs=spread_abs,
        predicted_move_abs=predicted_move_abs,
        after_cost_abs=after_cost_abs,
        sizing_cfg=sizing_cfg,
    )
    if geometry is None:
        result = size_crypto_spread_aware(
            mid_price=entry,
            bid=bid,
            ask=ask,
            atr=atr,
            side=side,
            account_equity=account_equity,
            policy_crypto=sizing_cfg,
            crypto_spread_config=policy.get("crypto_spread_config") or {},
            symbol=symbol,
        )
        if not result["approved"]:
            return _build_blocked(
                result["reject_reason"] or "BLOCKED_NO_SIZING_RULE",
                side,
                list(result["notes"]) + geom_notes,
                checks,
                sizing_inputs,
            )
        stop_loss = _safe_float(result["sl_price"])
        take_profit = _safe_float(result["tp_price"])
        stop_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        rr_multiple = tp_distance / stop_distance if stop_distance > 0 else 0.0
        base_notes = list(result["notes"]) + geom_notes
        checks["used_legacy_exit_geometry"] = True
    else:
        stop_loss = _safe_float(geometry["stop_loss"])
        take_profit = _safe_float(geometry["take_profit"])
        stop_distance = _safe_float(geometry["stop_distance"])
        tp_distance = _safe_float(geometry["tp_distance"])
        rr_multiple = _safe_float(geometry["achieved_rr_multiple"])
        base_notes = geom_notes
        checks["used_legacy_exit_geometry"] = False
    checks["sub_2r_model_cap"] = bool(flags.get("sub_2r_model_cap"))
    if flags.get("reason_codes"):
        checks["reason_codes"] = list(flags["reason_codes"])
    if rr_multiple < max(_safe_float(flags.get("min_automation_rr_multiple"), 1.0), 0.1):
        notes = base_notes + [f"achieved_rr_multiple={rr_multiple:.4f} < automation_floor"]
        return _build_blocked(
            "BLOCKED_SUB_1R_AFTER_COST",
            side,
            notes,
            {**checks, "after_cost_rr_multiple": rr_multiple},
            sizing_inputs,
            {
                "entry_ref_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "stop_distance": stop_distance,
                "tp_distance": tp_distance,
                "rr_multiple": rr_multiple,
            },
        )

    headroom_multiplier, headroom_reason, headroom_detail = _headroom_result(
        free_margin=free_margin,
        account_equity=account_equity,
        account_state=account_state,
        policy=policy,
        same_bucket_open_count=same_symbol_open_count,
        correlated_open_count=correlated_open_count,
    )
    checks["headroom_detail"] = headroom_detail
    if headroom_reason:
        return _build_blocked(
            headroom_reason,
            side,
            base_notes + [f"headroom_block={headroom_reason}"],
            checks,
            sizing_inputs,
        )

    opportunity_multiplier, opportunity_reason, opportunity_detail = _opportunity_result(
        after_cost_abs=after_cost_abs,
        stop_distance=stop_distance,
        sizing_cfg=sizing_cfg,
    )
    checks["opportunity_detail"] = opportunity_detail
    if opportunity_reason:
        return _build_blocked(
            opportunity_reason,
            side,
            base_notes + [f"opportunity_block={opportunity_reason}"],
            checks,
            sizing_inputs,
            {
                "entry_ref_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "stop_distance": stop_distance,
                "tp_distance": tp_distance,
                "rr_multiple": rr_multiple,
            },
        )

    contract_size = _safe_float(
        candidate.get("spec_contract_size")
        or (sizing_cfg.get("contract_sizes") or {}).get(symbol)
        or (sizing_cfg.get("contract_sizes") or {}).get("default")
        or 1.0,
        1.0,
    )
    base_risk_dollars = account_equity * _safe_float(sizing_cfg.get("risk_per_trade_pct"), 0.005)
    effective_risk_dollars = base_risk_dollars * headroom_multiplier * opportunity_multiplier
    checks["headroom_multiplier"] = headroom_multiplier
    checks["opportunity_multiplier"] = opportunity_multiplier
    risk_qty = effective_risk_dollars / (stop_distance * contract_size) if stop_distance > 0 and contract_size > 0 else 0.0
    notional_qty, notional_detail = _resolve_notional_cap(
        entry_price=entry_price,
        account_equity=account_equity,
        contract_size=contract_size,
        sizing_cfg=sizing_cfg,
        policy=policy,
        asset_class="crypto",
    )
    checks["notional_cap_detail"] = notional_detail
    qty = min(risk_qty, notional_qty)
    if min_qty > 0 and 0 < qty < min_qty:
        return _build_blocked(
            "BLOCKED_SIZE_BELOW_MIN_QTY",
            side,
            [f"raw_qty={qty:.6f} min_qty={min_qty}"],
            checks,
            sizing_inputs,
            {
                "base_qty": qty,
                "risk_qty": risk_qty,
                "notional_qty": notional_qty,
                "effective_risk_dollars": effective_risk_dollars,
            },
        )
    if max_qty > 0:
        qty = min(qty, max_qty)
    qty = _floor_to_step(qty, qty_step)
    qty = _round_qty(qty)

    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        return _build_blocked(
            "BLOCKED_SIZE_INVALID",
            side,
            [f"qty={qty} min_qty={min_qty}"],
            checks,
            sizing_inputs,
            {"base_qty": qty},
        )

    risk_dollars = qty * stop_distance * contract_size
    risk_pct = risk_dollars / account_equity if account_equity > 0 else 0.0
    position_notional = qty * entry_price * contract_size

    sizing_outputs = {
        "base_qty": qty,
        "final_qty": qty,
        "base_risk_dollars": base_risk_dollars,
        "effective_risk_dollars": effective_risk_dollars,
        "entry_ref_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "stop_distance": stop_distance,
        "tp_distance": tp_distance,
        "rr_multiple": rr_multiple,
        "risk_dollars": risk_dollars,
        "risk_pct": risk_pct,
        "position_notional": position_notional,
    }
    notes = base_notes + [
        f"qty_clip min={min_qty} max={max_qty} step={qty_step}",
        f"headroom_multiplier={headroom_multiplier:.2f}",
        f"opportunity_multiplier={opportunity_multiplier:.2f}",
        f"risk_dollars={risk_dollars:.4f}",
    ]
    return _build_approved(
        qty,
        stop_loss,
        take_profit,
        side,
        "risky_crypto_context_v1",
        notes,
        checks,
        sizing_inputs,
        sizing_outputs,
    )
