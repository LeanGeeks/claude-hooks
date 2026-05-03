#!/bin/bash
echo "=== Checking AI Notification Extension Status ==="
echo ""

# Check if extension is enabled
echo "1. Extension status:"
gnome-extensions info ai-notification-extension@local 2>&1 | head -10
echo ""

# Check for D-Bus service
echo "2. Checking D-Bus service:"
gdbus introspect --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    2>&1 | grep -i "ai-notif" || echo "   Service not found in DBus"
echo ""

# Check GNOME Shell logs
echo "3. Recent GNOME Shell logs:"
journalctl --user -u gnome-shell --since "5 minutes ago" 2>&1 | grep -i "ai-notification\|dbus.*notif" | tail -20
echo ""

# Check all active D-Bus names
echo "4. All active D-Bus names:"
dbus-send --session --dest=org.freedesktop.DBus --type=method_call --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>&1 | grep -i "shell\|extens" || echo "   Could not list names"
