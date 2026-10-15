#!/bin/zsh
# Launch (or no-op if already running) the dedicated FPMS Chrome window:
# a separate Chrome profile + remote debugging port for bot automation.
set -e

PROFILE="$HOME/.fpms-chrome"
PORT=9222

if curl -s -m 2 "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
  echo "[fpms-chrome] already running at :${PORT}"
  exit 0
fi

mkdir -p "$PROFILE"
open -na "Google Chrome" --args --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
  >/tmp/fpms-chrome.log 2>&1

# Wait up to 10s for the port to come up.
for i in {1..10}; do
  if curl -s -m 1 "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
    echo "[fpms-chrome] up at :${PORT}"
    exit 0
  fi
  sleep 1
done

echo "[fpms-chrome] FAILED to come up — see /tmp/fpms-chrome.log" >&2
exit 1
