"""
base_adapter.py — Abstract base interface for all Vanguard data adapters.

All adapters (Alpaca WebSocket, Twelve Data REST, etc.) implement this interface.
They write bars to vanguard_bars_1m or vanguard_bars_5m as appropriate.

Location: ~/SS/Vanguard/vanguard/data_adapters/base_adapter.py
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class BaseDataAdapter(ABC):
    """
    Interface for Vanguard data adapters.

    All adapters write bars to the same DB tables. The caller (V1 cache)
    is responsible for triggering aggregation after data is written.
    """

    @abstractmethod
    async def start(self) -> None:
        """
        Start streaming/polling. Non-blocking — returns after setup.
        For WebSocket adapters this runs the receive loop.
        For REST adapters this is a no-op (use poll_latest_bars() instead).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop cleanly. Waits for in-flight writes to complete."""

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """
        Return adapter health status dict.

        Minimum keys:
            adapter:       str — adapter name
            symbols:       int — number of subscribed symbols
            bars_received: int — total bars written this session
            running:       bool — whether adapter is active
        """
