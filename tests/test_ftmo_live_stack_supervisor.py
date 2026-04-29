from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.ftmo_live_stack_supervisor import _status_gate_issues


def test_status_gate_issues_ignores_monitor_when_allowed() -> None:
    status = SimpleNamespace(payload={})

    def fake_classify(_status):
        return "DEGRADED", ["diagnostic service_drift monitor", "diagnostic portfolio_blocked slot1"]

    import scripts.ftmo_live_stack_supervisor as supervisor

    original = supervisor._classify_summary
    supervisor._classify_summary = fake_classify
    try:
        issues = _status_gate_issues(status, allow_monitor_missing=True)
        assert issues == ["diagnostic portfolio_blocked slot1"]
    finally:
        supervisor._classify_summary = original


def test_status_gate_issues_returns_empty_for_healthy_stack() -> None:
    status = SimpleNamespace(payload={})

    def fake_classify(_status):
        return "HEALTHY", []

    import scripts.ftmo_live_stack_supervisor as supervisor

    original = supervisor._classify_summary
    supervisor._classify_summary = fake_classify
    try:
        issues = _status_gate_issues(status, allow_monitor_missing=False)
        assert issues == []
    finally:
        supervisor._classify_summary = original
