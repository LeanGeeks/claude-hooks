#!/bin/bash
set -e

INSTALL_DIR="$HOME/.local/bin"
COMPLETION_DIR="$HOME/.local/share/bash-completion/completions"

# Create directories
mkdir -p "$INSTALL_DIR"
mkdir -p "$COMPLETION_DIR"

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Verify source files exist
if [ ! -f "$SCRIPT_DIR/notify-interactive" ]; then
    echo "Error: notify-interactive not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/notify-interactive-completion.bash" ]; then
    echo "Error: notify-interactive-completion.bash not found in $SCRIPT_DIR"
    exit 1
fi

# Install CLI
cp "$SCRIPT_DIR/notify-interactive" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/notify-interactive"

# Install completion
cp "$SCRIPT_DIR/notify-interactive-completion.bash" "$COMPLETION_DIR/notify-interactive"

# Add to PATH if needed
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo ""
    echo "⚠️  $HOME/.local/bin is not in your PATH"
    echo "   Add this to your ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "✅ Installed notify-interactive to $INSTALL_DIR"
echo "✅ Installed bash completion"
echo ""
echo "Run 'notify-interactive --help' to get started"
