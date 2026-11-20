#!/bin/sh
# Schedule the daily autopilot on macOS (launchd), for every config with `autopilot.enabled: true`.
#   sh scripts/install_autopilot.sh          # every day at 07:00
#   sh scripts/install_autopilot.sh 6 30     # every day at 06:30
# On Linux, use cron instead:  0 7 * * *  cd /path/to/seo-engine && .venv/bin/python main.py autopilot --all
set -e
ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
HOUR="${1:-7}"
MINUTE="${2:-0}"
LABEL="com.seo-engine.autopilot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$ENGINE/.venv/bin/python" ] || { echo "No .venv in $ENGINE; run the Setup steps in QUICKSTART.md first."; exit 1; }
command -v gh >/dev/null 2>&1 || echo "Note: the gh CLI is not on PATH; sites that publish through pull requests need it."
mkdir -p "$HOME/Library/LaunchAgents" "$ENGINE/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$ENGINE</string>
  <key>ProgramArguments</key><array>
    <string>$ENGINE/.venv/bin/python</string>
    <string>main.py</string><string>autopilot</string><string>--all</string><string>--notify</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$ENGINE/logs/autopilot.log</string>
  <key>StandardErrorPath</key><string>$ENGINE/logs/autopilot.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Autopilot scheduled daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE"). Log: $ENGINE/logs/autopilot.log"
echo "Remove it with: launchctl unload \"$PLIST\" && rm \"$PLIST\""
