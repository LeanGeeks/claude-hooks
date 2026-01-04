#!/bin/bash
EXTENSION_UUID="ai-notification-extension@local"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"

# Disable extension first
gnome-extensions disable "$EXTENSION_UUID" 2>/dev/null || true

# Remove extension directory
rm -rf "$EXTENSION_DIR"

echo "Extension uninstalled"
