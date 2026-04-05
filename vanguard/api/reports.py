"""
reports.py — Report config CRUD + generation + Telegram delivery.

Block types:
  tier_block         — picks from one tier, formatted for Telegram
  overlap_block      — cross-source tickers appearing in multiple sources
  summary_block      — aggregate stats
  ai_brief_block     — AI-generated brief (requires anthropic package)
  forward_track_block — historical performance
  text_block         — static text

Location: ~/SS/Vanguard/vanguard/api/reports.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date as date_type
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VANGUARD_ROOT = Path(__file__).resolve().parent.parent.parent
VANGUARD_DB   = VANGUARD_ROOT / "data" / "vanguard_universe.db"

CREATE_REPORT_CONFIGS = """
CREATE TABLE IF NOT EXISTS report_configs (
    id                  TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name                TEXT NOT NULL,
    enabled             INTEGER DEFAULT 1,
    schedule            TEXT NOT NULL,
    delivery_channel    TEXT DEFAULT 'telegram',
    blocks              TEXT NOT NULL,
    telegram_bot_token  TEXT,
    telegram_chat_id    TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
"""


def init_table() -> None:
    with sqlite3.connect(str(VANGUARD_DB)) as con:
        con.execute(CREATE_REPORT_CONFIGS)
        con.commit()


def _get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(VANGUARD_DB))
    con.row_factory = sqlite3.Row
    return con


# ── CRUD ──────────────────────────────────────────────────────────────────────

def list_reports() -> list[dict[str, Any]]:
    with _get_conn() as con:
        rows = con.execute(
            "SELECT * FROM report_configs ORDER BY name ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_report(report_id: str) -> dict[str, Any] | None:
    with _get_conn() as con:
        row = con.execute(
            "SELECT * FROM report_configs WHERE id = ?", (report_id,)
        ).fetchone()
    return dict(row) if row else None


def create_report(data: dict[str, Any]) -> dict[str, Any]:
    blocks = data.get("blocks", [])
    blocks_json = json.dumps(blocks) if not isinstance(blocks, str) else blocks

    with _get_conn() as con:
        con.execute(
            """
            INSERT INTO report_configs
                (name, enabled, schedule, delivery_channel, blocks,
                 telegram_bot_token, telegram_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                int(data.get("enabled", 1)),
                data["schedule"],
                data.get("delivery_channel", "telegram"),
                blocks_json,
                data.get("telegram_bot_token") or _env_telegram_token(),
                data.get("telegram_chat_id") or _env_telegram_chat_id(),
            ),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM report_configs WHERE rowid = last_insert_rowid()"
        ).fetchone()
    return dict(row) if row else {}


def update_report(report_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    with _get_conn() as con:
        if not con.execute(
            "SELECT id FROM report_configs WHERE id = ?", (report_id,)
        ).fetchone():
            return None

        updatable = [
            "name", "enabled", "schedule", "delivery_channel", "blocks",
            "telegram_bot_token", "telegram_chat_id",
        ]
        set_parts: list[str] = []
        values: list[Any] = []
        for field in updatable:
            if field in data:
                set_parts.append(f"{field} = ?")
                val = data[field]
                if field == "blocks" and not isinstance(val, str):
                    val = json.dumps(val)
                values.append(val)
        if not set_parts:
            return get_report(report_id)

        set_parts.append("updated_at = datetime('now')")
        values.append(report_id)
        con.execute(
            f"UPDATE report_configs SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM report_configs WHERE id = ?", (report_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_report(report_id: str) -> tuple[bool, str]:
    with _get_conn() as con:
        if not con.execute(
            "SELECT id FROM report_configs WHERE id = ?", (report_id,)
        ).fetchone():
            return False, "Report not found"
        con.execute("DELETE FROM report_configs WHERE id = ?", (report_id,))
        con.commit()
    return True, ""


# ── Report generation ─────────────────────────────────────────────────────────

def _env_telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        env_file = VANGUARD_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    return token


def _env_telegram_chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        env_file = VANGUARD_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TELEGRAM_CHAT_ID="):
                    chat_id = line.split("=", 1)[1].strip()
                    break
    return chat_id


def _render_tier_block(block: dict, trade_date: str) -> str:
    """Render a tier_block as a Telegram HTML message."""
    from vanguard.api.trade_desk import get_picks_today
    source   = block.get("source", "s1")
    tier     = block.get("tier", "tier_dual")
    max_picks = int(block.get("max_picks", 5))
    sort_by  = block.get("sort_by", "scorer_prob")

    picks_data = get_picks_today(trade_date)
    tier_entry = next(
        (t for t in picks_data["tiers"] if t["tier"] == tier), None
    )
    if not tier_entry:
        return f"<b>{tier}</b>: no data"

    picks = tier_entry["picks"]
    if sort_by and picks:
        picks = sorted(picks, key=lambda p: -float(p.get("scores", {}).get(sort_by, 0)))
    picks = picks[:max_picks]

    if not picks:
        return f"<b>{tier_entry['label']}</b>: no picks today"

    lines = [f"<b>{tier_entry['label']}</b> ({source.upper()}) — {trade_date}"]
    for p in picks:
        direction_emoji = "🟢" if p["direction"] == "LONG" else "🔴"
        scores_str = " | ".join(
            f"{k.upper()}={v:.3f}"
            for k, v in (p.get("scores") or {}).items()
        )
        lines.append(f"  {direction_emoji} <code>{p['symbol']:6s}</code> ${p['price']:.2f}  {scores_str}")
    return "\n".join(lines)


def _render_overlap_block(block: dict, trade_date: str) -> str:
    """Find tickers appearing in both Meridian and S1 picks."""
    from vanguard.api.adapters.meridian_adapter import get_candidates as mer_get
    from vanguard.api.adapters.s1_adapter import get_candidates as s1_get

    mer_rows = mer_get(trade_date=trade_date)
    s1_rows  = s1_get(run_date=trade_date)

    mer_symbols = {r["symbol"] for r in mer_rows}
    s1_symbols  = {r["symbol"] for r in s1_rows}
    overlap = sorted(mer_symbols & s1_symbols)

    label = block.get("label", "Cross-System Overlap")
    if not overlap:
        return f"<b>{label}</b>: no overlap"

    syms_str = ", ".join(f"<code>{s}</code>" for s in overlap)
    return f"<b>{label}</b> ({len(overlap)} symbols):\n  {syms_str}"


def _render_summary_block(block: dict, trade_date: str) -> str:
    """Aggregate stats across all tiers."""
    from vanguard.api.trade_desk import get_picks_today
    data = get_picks_today(trade_date)
    stats = block.get("stats", ["total_picks", "long_short_split"])

    lines = [f"<b>Summary — {trade_date}</b>"]
    if "total_picks" in stats:
        lines.append(f"  Total picks: {data['total_picks']}")
    if "long_short_split" in stats:
        longs  = sum(len([p for p in t["picks"] if p["direction"] == "LONG"]) for t in data["tiers"])
        shorts = sum(len([p for p in t["picks"] if p["direction"] == "SHORT"]) for t in data["tiers"])
        lines.append(f"  Longs: {longs}  Shorts: {shorts}")
    if "top_sectors" in stats:
        from vanguard.api.adapters.meridian_adapter import get_candidates as mer_get
        from vanguard.api.adapters.s1_adapter import get_candidates as s1_get
        all_rows = mer_get(trade_date=trade_date) + s1_get(run_date=trade_date)
        sector_counts: dict[str, int] = {}
        for r in all_rows:
            sec = r.get("sector") or "UNKNOWN"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        top = sorted(sector_counts.items(), key=lambda x: -x[1])[:3]
        lines.append("  Top sectors: " + ", ".join(f"{s}({n})" for s, n in top))
    return "\n".join(lines)


def _render_ai_brief_block(block: dict, picks_context: str) -> str:
    """Generate AI brief using Anthropic Haiku (if available)."""
    try:
        import anthropic  # optional dependency
    except ImportError:
        return "<b>AI Brief</b>: anthropic package not installed"

    model   = block.get("model", "haiku")
    prompt  = block.get("prompt", "Generate a 2-sentence trading recommendation for these picks.")
    model_id = "claude-haiku-4-5-20251001" if "haiku" in model.lower() else "claude-sonnet-4-6"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "<b>AI Brief</b>: ANTHROPIC_API_KEY not set"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model_id,
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"{prompt}\n\nPicks:\n{picks_context}",
            }],
        )
        text = message.content[0].text if message.content else ""
        return f"<b>AI Brief</b>:\n{text}"
    except Exception as exc:
        logger.error(f"AI brief failed: {exc}")
        return f"<b>AI Brief</b>: error — {exc}"


def _render_forward_track_block(block: dict, trade_date: str) -> str:
    """Show historical performance from execution_log."""
    lookback = int(block.get("lookback_days", 5))
    metrics  = block.get("metrics", ["win_rate"])

    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM   execution_log
                WHERE  DATE(executed_at) >= DATE(?, ?)
                GROUP  BY status
                """,
                (trade_date, f"-{lookback} days"),
            ).fetchall()
    except Exception as exc:
        return f"<b>Forward Track</b>: DB error — {exc}"

    total = sum(r["cnt"] for r in rows)
    if total == 0:
        return "<b>Forward Track</b>: no execution data"

    status_map = {r["status"]: r["cnt"] for r in rows}
    submitted  = status_map.get("SUBMITTED", 0)
    lines = [f"<b>Forward Track ({lookback}d)</b>"]
    lines.append(f"  Total orders: {total}")
    lines.append(f"  Submitted: {submitted}")
    return "\n".join(lines)


def generate_report(
    report: dict[str, Any],
    trade_date: str | None = None,
) -> str:
    """
    Generate full report text from a report config dict.

    Returns the complete Telegram-formatted HTML string.
    """
    date_str = trade_date or str(date_type.today())
    blocks_raw = report.get("blocks") or "[]"
    if isinstance(blocks_raw, str):
        try:
            blocks = json.loads(blocks_raw)
        except json.JSONDecodeError:
            blocks = []
    else:
        blocks = blocks_raw

    # Pre-compute picks context for AI block
    picks_context_lines: list[str] = []

    sections: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        try:
            if btype == "tier_block":
                sections.append(_render_tier_block(block, date_str))
            elif btype == "overlap_block":
                sections.append(_render_overlap_block(block, date_str))
            elif btype == "summary_block":
                sections.append(_render_summary_block(block, date_str))
            elif btype == "ai_brief_block":
                ctx = "\n".join(picks_context_lines) or "No picks available."
                sections.append(_render_ai_brief_block(block, ctx))
            elif btype == "forward_track_block":
                sections.append(_render_forward_track_block(block, date_str))
            elif btype == "text_block":
                sections.append(block.get("content", ""))
            else:
                logger.warning(f"Unknown block type: {btype!r}")
        except Exception as exc:
            logger.error(f"Block render error ({btype}): {exc}")
            sections.append(f"<i>[{btype} render error: {exc}]</i>")

        # Accumulate context for AI block
        if btype == "tier_block" and sections:
            picks_context_lines.append(sections[-1])

    return "\n\n".join(s for s in sections if s)


def send_report(
    report: dict[str, Any],
    trade_date: str | None = None,
) -> dict[str, Any]:
    """
    Generate and send a report to Telegram.

    Returns {"success": bool, "text_length": int, "error": str|None}
    """
    from vanguard.execution.telegram_alerts import TelegramAlerts

    bot_token = report.get("telegram_bot_token") or _env_telegram_token()
    chat_id   = report.get("telegram_chat_id")   or _env_telegram_chat_id()

    if not bot_token or not chat_id:
        return {"success": False, "text_length": 0, "error": "Telegram not configured"}

    text = generate_report(report, trade_date)
    if not text:
        return {"success": False, "text_length": 0, "error": "Report generated empty"}

    tg = TelegramAlerts(bot_token=bot_token, chat_id=str(chat_id))
    success = tg.send(text, parse_mode="HTML")
    return {
        "success":     success,
        "text_length": len(text),
        "error":       None if success else "Telegram send failed",
    }
