#!/bin/bash
# reset_autoclose_breaker.sh — Reset the auto-close circuit breaker.
#
# Run this after investigating consecutive close failures.
# The lifecycle daemon reads /tmp/autoclose_breaker_reset at the top of each
# iteration and resets its in-memory breaker when the file is present.
#
# Usage: bash scripts/reset_autoclose_breaker.sh

touch /tmp/autoclose_breaker_reset
echo "Breaker reset flag written to /tmp/autoclose_breaker_reset"
echo "Daemon will pick up within 1 iteration (up to ${1:-60}s)."
