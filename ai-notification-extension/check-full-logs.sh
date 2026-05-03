#!/bin/bash
echo "=== Full extension log check ==="
echo ""
echo "1. All AI Notification logs (last 100 lines):"
journalctl --user --since "10 minutes ago" 2>&1 | grep -i "ai-notification\|ainotif" | tail -100
echo ""
echo "2. All GNOME Shell errors:"
journalctl --user -u gnome-shell --since "10 minutes ago" 2>&1 | grep -i "error" | tail -50
echo ""
echo "3. All D-Bus related logs:"
journalctl --user --since "10 minutes ago" 2>&1 | grep -i "dbus" | tail -50
