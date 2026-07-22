#!/bin/bash
# Generic launcher for a scheduled headless-Claude job.
#   $1 = launchd job label (self-removed after the run — one-shot)
#   $2 = path to the prompt file to hand to `claude -p`
# Wakes a fresh headless Claude with full tool access, scoped by the prompt to
# read-only analysis + a single Telegram send, then boots itself out so it never repeats.
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

# one-shot: remove self so it can never fire again
/bin/rm -f "/Users/vikasreddy/Library/LaunchAgents/${JOB}.plist"
/bin/launchctl bootout "gui/$(/usr/bin/id -u)/${JOB}" 2>/dev/null
exit 0
