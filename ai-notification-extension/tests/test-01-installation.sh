#!/bin/bash
# test-01-installation.sh

set -e

echo "=== Test 1: Installation ==="

# Check GNOME version
echo "GNOME Shell version:"
gnome-shell --version

# Install extension
echo "Installing extension..."
cd /data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension
./install.sh

# Check installation
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/ai-notification-extension@local"
if [ -d "$EXT_DIR" ]; then
    echo "Extension directory exists"
else
    echo "ERROR: Extension directory not found"
    exit 1
fi

# Enable extension
echo "Enabling extension..."
gnome-extensions enable ai-notification-extension@local

# Check if enabled
if gnome-extensions list | grep -q "ai-notification-extension@local"; then
    echo "Extension is enabled"
else
    echo "ERROR: Extension not found in list"
    exit 1
fi

# Check for errors
ERRORS=$(gnome-extensions info ai-notification-extension@local 2>&1 || true)
if echo "$ERRORS" | grep -qi "error"; then
    echo "ERROR: Extension has errors:"
    echo "$ERRORS"
    exit 1
fi

# Give it time to fully load
echo "Waiting for extension to fully initialize..."
sleep 3

echo "Installation test passed"
