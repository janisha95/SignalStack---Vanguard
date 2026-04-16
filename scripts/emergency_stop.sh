#!/bin/bash
# emergency_stop.sh — Kill Vanguard prod orchestrator and lifecycle daemon immediately.
# MetaApi positions remain open — manage manually via MetaTrader or MetaApi dashboard.
pkill -f "vanguard_orchestrator.py" || true
pkill -f "lifecycle_daemon" || true
echo "Stopped. MetaApi positions remain open — manage manually."
