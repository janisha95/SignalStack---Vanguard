from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.data_adapters.dwx.dwx_client import dwx_client


def test_load_json_payload_recovers_last_object_from_concatenated_json() -> None:
    client = dwx_client.__new__(dwx_client)
    client.verbose = False

    data = client._load_json_payload('{"a": 1}{"b": 2}', "messages")

    assert data == {"b": 2}


def test_load_json_payload_returns_none_for_invalid_json() -> None:
    client = dwx_client.__new__(dwx_client)
    client.verbose = False

    data = client._load_json_payload('{"a": 1', "messages")

    assert data is None
