#!/bin/bash
# Launcher for a RECURRING scheduled headless-Claude job (does NOT self-remove).
#   $1 = launchd job label (for the log filename)
#   $2 = path to the prompt file handed to `claude -p`
# Wakes a fresh headless Claude with full tool access, scoped by the prompt to
# read-only analysis + a single Telegram send. Keeps its launchd job (recurring).
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="/Users/vikasreddy"
cd /Users/vikasreddy/cryptobot || exit 1

JOB="$1"
PF="$2"
LOG="/Users/vikasreddy/cryptobot/${JOB}.log"

{
  echo "=== $(/bin/date) firing ${JOB} ==="
  /opt/homebrew/bin/claude -p "$(/bin/cat "$PF")" --dangerously-skip-permissions
  echo "=== $(/bin/date) done ${JOB} (exit $?) ==="
} >> "$LOG" 2>&1
exit 0
