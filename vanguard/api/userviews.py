"""
userviews.py — Userview CRUD for the SignalStack Unified API.

Stores named views (filter/sort/column presets) in vanguard_universe.db.
System views (is_system=1) are seeded on startup and cannot be deleted.

Location: ~/SS/Vanguard/vanguard/api/userviews.py
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

VANGUARD_DB = get_shadow_db_path()

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS userviews (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name            TEXT NOT NULL,
    source          TEXT NOT NULL,
    direction       TEXT,
    filters         TEXT,
    sorts           TEXT,
    grouping        TEXT,
    visible_columns TEXT,
    column_order    TEXT,
    display_mode    TEXT DEFAULT 'table',
    is_system       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
"""

# 4 canonical system views — stable IDs used for DELETE+reseed on every startup
SYSTEM_VIEWS: list[dict[str, Any]] = [
    {
        "id": "sys_s1_all",
        "name": "S1 All",
        "source": "s1",
        "direction": None,
        "filters": None,
        "sorts": json.dumps([{"field": "scorer_prob", "direction": "desc"}]),
        "visible_columns": json.dumps(["symbol", "side", "price", "tier", "p_tp", "nn_p_tp", "scorer_prob", "convergence_score", "volume_ratio", "regime"]),
        "display_mode": "table",
    },
    {
        "id": "sys_meridian_all",
        "name": "Meridian Latest Shortlist",
        "source": "meridian",
        "direction": None,
        "filters": None,
        "sorts": json.dumps([{"field": "final_score", "direction": "desc"}]),
        "visible_columns": json.dumps(["symbol", "side", "price", "tier", "tcn_long_score", "tcn_short_score", "tcn_score", "final_score", "rank", "sector", "regime"]),
        "display_mode": "table",
    },
    {
        "id": "sys_combined",
        "name": "Combined",
        "source": "combined",
        "direction": None,
        "filters": None,
        "sorts": json.dumps([{"field": "source", "direction": "asc"}, {"field": "final_score", "direction": "desc"}]),
        "visible_columns": json.dumps(["symbol", "source", "side", "price", "tier", "tcn_long_score", "tcn_short_score", "tcn_score", "final_score", "p_tp", "scorer_prob", "regime"]),
        "display_mode": "table",
    },
    {
        "id": "sys_vanguard",
        "name": "Vanguard",
        "source": "vanguard",
        "direction": None,
        "filters": None,
        "sorts": json.dumps([{"field": "consensus_count", "direction": "desc"}, {"field": "strategy_score", "direction": "desc"}]),
        "visible_columns": json.dumps(["symbol", "side", "tier", "strategy_score", "ml_prob", "edge_score", "consensus_count", "regime"]),
        "display_mode": "table",
    },
]


def _get_conn() -> sqlite3.Connection:
    return sqlite_conn(VANGUARD_DB)


def init_table() -> None:
    """Create the userviews table if it doesn't exist."""
    with _get_conn() as con:
        con.execute(CREATE_TABLE)
        con.commit()


def seed_system_views() -> None:
    """
    Delete all existing system views and re-insert the canonical 4.

    Custom (user-created) views are untouched (is_system = 0).
    Uses stable IDs so frontend bookmarks survive server restarts.
    """
    init_table()
    with _get_conn() as con:
        con.execute("DELETE FROM userviews WHERE is_system = 1")
        for sv in SYSTEM_VIEWS:
            con.execute(
                """
                INSERT INTO userviews
                    (id, name, source, direction, filters, sorts, grouping,
                     visible_columns, column_order, display_mode, is_system)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    sv["id"],
                    sv["name"],
                    sv["source"],
                    sv.get("direction"),
                    sv.get("filters"),
                    sv.get("sorts"),
                    sv.get("grouping"),
                    sv.get("visible_columns"),
                    sv.get("column_order"),
                    sv.get("display_mode", "table"),
                ),
            )
        con.commit()
    logger.info(f"Seeded {len(SYSTEM_VIEWS)} system views (delete+reseed)")


def list_views() -> list[dict[str, Any]]:
    """Return all userviews ordered by is_system desc, name asc."""
    with _get_conn() as con:
        rows = con.execute(
            "SELECT * FROM userviews ORDER BY is_system DESC, name ASC"
        ).fetchall()
    return [_serialize_view(r) for r in rows]


def get_view(view_id: str) -> dict[str, Any] | None:
    with _get_conn() as con:
        row = con.execute(
            "SELECT * FROM userviews WHERE id = ?", (view_id,)
        ).fetchone()
    return _serialize_view(row) if row else None


def create_view(data: dict[str, Any]) -> dict[str, Any]:
    """Insert a new user view. Returns the created row."""
    with _get_conn() as con:
        con.execute(
            """
            INSERT INTO userviews
                (name, source, direction, filters, sorts, grouping,
                 visible_columns, column_order, display_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["source"],
                data.get("direction"),
                _json_or_none(data.get("filters")),
                _json_or_none(data.get("sorts")),
                _json_or_none(data.get("grouping")),
                _json_or_none(data.get("visible_columns")),
                _json_or_none(data.get("column_order")),
                data.get("display_mode", "table"),
            ),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM userviews WHERE rowid = last_insert_rowid()"
        ).fetchone()
    return _serialize_view(row) if row else {}


def update_view(view_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Update an existing user view. Returns updated row or None if not found."""
    with _get_conn() as con:
        existing = con.execute(
            "SELECT is_system FROM userviews WHERE id = ?", (view_id,)
        ).fetchone()
        if not existing:
            return None
        fields_to_update = [
            "name", "source", "direction", "filters", "sorts",
            "grouping", "visible_columns", "column_order", "display_mode",
        ]
        set_clauses = ", ".join(f"{f} = ?" for f in fields_to_update if f in data)
        set_clauses += ", updated_at = datetime('now')"
        values = [_json_or_none(data[f]) if f in ("filters", "sorts", "grouping", "visible_columns", "column_order") else data[f]
                  for f in fields_to_update if f in data]
        values.append(view_id)
        con.execute(
            f"UPDATE userviews SET {set_clauses} WHERE id = ?", values
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM userviews WHERE id = ?", (view_id,)
        ).fetchone()
    return _serialize_view(row) if row else None


def delete_view(view_id: str) -> tuple[bool, str]:
    """
    Delete a view. Returns (success, error_message).
    System views cannot be deleted.
    """
    with _get_conn() as con:
        row = con.execute(
            "SELECT is_system FROM userviews WHERE id = ?", (view_id,)
        ).fetchone()
        if not row:
            return False, "View not found"
        if row["is_system"]:
            return False, "System views cannot be deleted"
        con.execute("DELETE FROM userviews WHERE id = ?", (view_id,))
        con.commit()
    return True, ""


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _parse_json_field(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (list, dict)) or fallback is None:
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    return fallback


def _serialize_view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "source": data["source"],
        "direction": data.get("direction"),
        "filters": _parse_json_field(data.get("filters"), []),
        "sorts": _parse_json_field(data.get("sorts"), []),
        "grouping": _parse_json_field(data.get("grouping"), None),
        "visible_columns": _parse_json_field(data.get("visible_columns"), []),
        "column_order": _parse_json_field(data.get("column_order"), []),
        "display_mode": data.get("display_mode", "table"),
        "is_system": bool(data.get("is_system", 0)),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }
