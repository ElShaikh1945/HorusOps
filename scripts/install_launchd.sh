#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

PLIST_NAME="com.waisoft.bot.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$PLIST_NAME"

mkdir -p "$DIR/server_logs"
mkdir -p "$TARGET_DIR"

echo "=================================================="
echo "    Installing WAISoft Bot as macOS launchd Service"
echo "=================================================="

# Unload previous instance if running
if launchctl list | grep -q "com.waisoft.bot"; then
    echo "[*] Unloading existing service..."
    launchctl unload "$TARGET_PLIST" 2>/dev/null || true
fi

# Generate real plist with current path
sed "s|__APP_DIR__|$DIR|g" "services/$PLIST_NAME" > "$TARGET_PLIST"

# Load new service
launchctl load -w "$TARGET_PLIST"

echo "[✓] Successfully installed and started com.waisoft.bot"
echo "[*] Plist location: $TARGET_PLIST"
echo "[*] Logs: $DIR/server_logs/bot_stdout.log"
echo ""
echo "Commands to manage the service:"
echo "  Start:   launchctl start com.waisoft.bot"
echo "  Stop:    launchctl stop com.waisoft.bot"
echo "  Unload:  launchctl unload $TARGET_PLIST"
