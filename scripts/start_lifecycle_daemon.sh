#!/bin/bash
set -euo pipefail
cd /Users/sjani008/SS/Vanguard_QAenv
LOG=/tmp/lifecycle_daemon.log
PID=/tmp/lifecycle_daemon.pid

if [ -f "$PID" ] && kill -0 "$(cat $PID)" 2>/dev/null; then
  echo "Daemon already running, PID $(cat $PID)"
  exit 1
fi

nohup python3 -m vanguard.execution.lifecycle_daemon --interval 60 > "$LOG" 2>&1 &
echo $! > "$PID"
echo "Lifecycle daemon started. PID=$(cat $PID) LOG=$LOG"
