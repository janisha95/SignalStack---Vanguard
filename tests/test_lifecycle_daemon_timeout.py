from __future__ import annotations

from types import SimpleNamespace

from vanguard.execution.lifecycle_daemon import LifecycleDaemon


class _FakeService:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.queued: list[tuple[str, str, str]] = []
        self.processed: list[int] = []
        self.flattened: list[tuple[str, str]] = []

    def get_position_snapshot(self, profile_id: str) -> dict:
        assert profile_id == "ftmo_demo_100k"
        return self.snapshot

    def queue_close_at_timeout(self, *, profile_id: str, broker_position_id: str, requested_by: str) -> int:
        self.queued.append((profile_id, broker_position_id, requested_by))
        return len(self.queued)

    def process_request(self, request_id: int, *, dry_run: bool = False):
        self.processed.append(request_id)
        if request_id >= 100:
            self.snapshot["positions"] = []
        return SimpleNamespace(status="RECONCILED")

    def queue_flatten_profile(self, *, profile_id: str, requested_by: str) -> int:
        self.flattened.append((profile_id, requested_by))
        return 100 + len(self.flattened)


def test_timeout_policy_auto_close_enabled_for_eligible_mt5_profile() -> None:
    daemon = LifecycleDaemon.__new__(LifecycleDaemon)
    daemon.config = {
        "position_manager": {
            "timeout_auto_close": {
                "enabled": True,
                "eligible_profiles": ["ftmo_demo_100k"],
            }
        }
    }
    profile = {"id": "ftmo_demo_100k", "bridge_type": "mt5_local"}
    assert daemon._timeout_policy_auto_close_enabled(profile) is True


def test_process_timeout_policy_closes_only_expired_positions() -> None:
    daemon = LifecycleDaemon.__new__(LifecycleDaemon)
    daemon.config = {}
    daemon._position_action_service = _FakeService(
        {
            "positions": [
                {
                    "broker_position_id": "expired-ticket",
                    "policy_timeout_minutes": 30,
                    "minutes_to_timeout": -0.1,
                },
                {
                    "broker_position_id": "future-ticket",
                    "policy_timeout_minutes": 30,
                    "minutes_to_timeout": 5.0,
                },
                {
                    "broker_position_id": "no-policy-ticket",
                    "policy_timeout_minutes": None,
                    "minutes_to_timeout": None,
                },
            ]
        }
    )
    results = daemon._process_timeout_policy_closes({"id": "ftmo_demo_100k"})
    assert results == [("expired-ticket", "RECONCILED")]
    assert daemon._position_action_service.queued == [
        ("ftmo_demo_100k", "expired-ticket", "lifecycle_daemon")
    ]
    assert daemon._position_action_service.processed == [1]


def test_process_scheduled_flatten_dispatches_once_within_window() -> None:
    daemon = LifecycleDaemon.__new__(LifecycleDaemon)
    daemon.config = {
        "position_manager": {
            "scheduled_flatten": {
                "enabled": True,
                "eligible_profiles": ["ftmo_demo_100k"],
                "start_et": "16:45",
                "end_et": "18:00",
            }
        }
    }
    daemon._position_action_service = _FakeService(
        {"positions": [{"broker_position_id": "ticket-1"}]}
    )
    profile = {"id": "ftmo_demo_100k", "bridge_type": "mt5_local"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = daemon._process_scheduled_flatten(
        profile,
        now_et=datetime(2026, 4, 15, 16, 50, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result == "RECONCILED"
    assert daemon._position_action_service.flattened == [
        ("ftmo_demo_100k", "lifecycle_daemon")
    ]
    assert daemon._position_action_service.processed == [101]

    result_again = daemon._process_scheduled_flatten(
        profile,
        now_et=datetime(2026, 4, 15, 16, 55, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result_again is None


def test_process_scheduled_flatten_retries_when_positions_remain() -> None:
    daemon = LifecycleDaemon.__new__(LifecycleDaemon)
    daemon.config = {
        "position_manager": {
            "scheduled_flatten": {
                "enabled": True,
                "eligible_profiles": ["ftmo_demo_100k"],
                "start_et": "16:45",
                "end_et": "18:00",
            }
        }
    }

    class _StickyService(_FakeService):
        def process_request(self, request_id: int, *, dry_run: bool = False):
            self.processed.append(request_id)
            return SimpleNamespace(status="RECONCILED")

    daemon._position_action_service = _StickyService(
        {"positions": [{"broker_position_id": "ticket-1"}]}
    )
    profile = {"id": "ftmo_demo_100k", "bridge_type": "mt5_local"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = daemon._process_scheduled_flatten(
        profile,
        now_et=datetime(2026, 4, 15, 16, 50, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result == "PARTIAL_REMAINING_1"

    result_again = daemon._process_scheduled_flatten(
        profile,
        now_et=datetime(2026, 4, 15, 16, 51, tzinfo=ZoneInfo("America/New_York")),
    )
    assert result_again == "PARTIAL_REMAINING_1"
    assert daemon._position_action_service.flattened == [
        ("ftmo_demo_100k", "lifecycle_daemon"),
        ("ftmo_demo_100k", "lifecycle_daemon"),
    ]
