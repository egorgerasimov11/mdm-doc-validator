#!/usr/bin/env bash
# Install the LaunchAgent that keeps the mdmdoc operator server running
# permanently on 127.0.0.1:8766 (survives reboots; auto-restarts on crash).
set -euo pipefail

PLIST=~/Library/LaunchAgents/com.egor.mdmdoc.plist
MDMDOC="$(command -v mdmdoc || echo "$HOME/.local/bin/mdmdoc")"
LOG=~/Library/Logs/mdmdoc-server.log

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.egor.mdmdoc</string>
  <key>ProgramArguments</key>
  <array>
    <string>${MDMDOC}</string>
    <string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8766</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:${HOME}/.local/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed + loaded: com.egor.mdmdoc (server on 127.0.0.1:8766, log: $LOG)"
echo "open the console:   http://127.0.0.1:8766/ui   (or: mdmdoc ui)"
