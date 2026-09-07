#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

SERVICE_NAME="waisoft-bot.service"
TARGET_DIR="$HOME/.config/systemd/user"
TARGET_FILE="$TARGET_DIR/$SERVICE_NAME"

mkdir -p "$DIR/server_logs"
mkdir -p "$TARGET_DIR"

echo "=================================================="
echo "   Installing WAISoft Bot as Linux systemd Service"
echo "=================================================="

# Generate unit file with current path
sed "s|__APP_DIR__|$DIR|g" "services/$SERVICE_NAME" > "$TARGET_FILE"

# Reload daemon and enable service
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "[✓] Successfully installed and started $SERVICE_NAME"
echo "[*] Service location: $TARGET_FILE"
echo "[*] Logs: $DIR/server_logs/bot_stdout.log"
echo ""
echo "Commands to manage the service:"
echo "  Status:  systemctl --user status $SERVICE_NAME"
echo "  Restart: systemctl --user restart $SERVICE_NAME"
echo "  Stop:    systemctl --user stop $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
