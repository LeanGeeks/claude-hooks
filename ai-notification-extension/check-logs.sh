#!/bin/bash
echo "=== Checking for extension errors ==="
echo ""
echo "1. Recent errors in GNOME Shell:"
journalctl --user -u gnome-shell --since "5 minutes ago" 2>&1 | grep -i "ai-notification\|error\|exception" | tail -30
echo ""
echo "2. All recent GNOME Shell logs:"
journalctl --user -u gnome-shell --since "5 minutes ago" 2>&1 | tail -50
