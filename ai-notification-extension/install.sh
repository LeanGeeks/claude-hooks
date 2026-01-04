#!/bin/bash
EXTENSION_UUID="ai-notification-extension@local"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"

# Create extension directory if it doesn't exist
mkdir -p "$EXTENSION_DIR"

# Copy extension files
cp -r extension/* "$EXTENSION_DIR/"

# Log installation
echo "Extension installed to: $EXTENSION_DIR"
echo "Enable with: gnome-extensions enable $EXTENSION_UUID"
echo "Or restart GNOME Shell: Alt+F2, type 'r', press Enter"
