#!/bin/bash
PID=/tmp/lifecycle_daemon.pid
if [ ! -f "$PID" ]; then
  echo "No PID file found"
  exit 0
fi
kill -TERM "$(cat $PID)" 2>/dev/null || echo "Process not running"
rm -f "$PID"
echo "Daemon stopped"
