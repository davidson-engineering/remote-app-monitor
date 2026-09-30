#!/usr/bin/env bash
# Any language can feed a dashboard: here, plain shell and curl.
#
#   curl -fsSL https://raw.githubusercontent.com/davidson-engineering/sightglass/main/examples/03_from_the_shell.sh | bash
#
# Starts `sightglass` (installed, or fetched with uvx), then posts a few
# readings about this machine every second. Stop with Ctrl+C.
set -euo pipefail

port=${PORT:-8080}
url="http://127.0.0.1:$port"
package="sightglass[web] @ git+https://github.com/davidson-engineering/sightglass"

if command -v sightglass >/dev/null; then
  sightglass=(sightglass)
else
  sightglass=(uvx --quiet --from "$package" sightglass)
fi

echo "Starting the dashboard (the first run downloads it)..."
"${sightglass[@]}" --port "$port" --title "From the shell" --open </dev/null &
dashboard=$!
trap 'kill $dashboard 2>/dev/null' EXIT

until curl -fs "$url/values" >/dev/null; do # wait until it's up
  kill -0 "$dashboard" 2>/dev/null || exit 1  # it failed; its error is above
  sleep 0.2
done

started=$(date +%s)
while true; do
  load=$(uptime | awk -F'load averages?: ' '{split($2, a, /[ ,]+/); print a[1]}')
  disk=$(df -P "$HOME" | awk 'NR == 2 {print $5}') # the volume with your files
  # Each line is key=value pairs; quote values with spaces.
  curl -fs "$url/update" -d "uptime_s=$(($(date +%s) - started)) load=$load disk_used=$disk"
  curl -fs "$url/update" -d "time=\"$(date '+%H:%M:%S')\" random=$RANDOM"
  sleep 1
done
