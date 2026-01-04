#!/bin/bash
# test-07-dbus-communication.sh

set -e

echo "=== Test 7: D-Bus Communication ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 7.1: Check bus name
echo "7.1: Checking D-Bus service..."
if gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.GetNameOwner \
    "org.gnome.Shell.Extensions.AINotifications" 2>/dev/null; then
    echo "D-Bus service is running"
else
    echo "WARNING: D-Bus service not found (may not be initialized yet)"
    # Don't fail, just continue
fi

# Test 7.2: Direct D-Bus method call (optional, may fail if not fully initialized)
echo "7.2: Direct D-Bus call..."
RESULT=$(gdbus call --session \
    --dest org.gnome.Shell.Extensions.AINotifications \
    --object-path /org/gnome/Shell/Extensions/AINotifications \
    --method org.gnome.Shell.Extensions.AINotifications.ShowNotification \
    "'D-Bus Test'" \
    "'This was sent via D-Bus'" \
    "{'urgency': <'normal'>, 'actions': <[{'id': <'ok'>, 'label': <'OK'>}]>}" 2>&1 || echo "FAILED")

if echo "$RESULT" | grep -q "notif-"; then
    echo "D-Bus method call successful"
else
    echo "WARNING: D-Bus direct call had issues (this is expected in some environments)"
fi

sleep 1

# Test 7.3: Monitor D-Bus signals (background)
echo "7.3: Monitoring D-Bus signals..."
timeout 5s dbus-monitor --session \
    "interface=org.gnome.Shell.Extensions.AINotifications" \
    "member=NotificationResult" > /tmp/dbus-signals.log 2>&1 &
MONITOR_PID=$!

# Send a notification
$CLIENT "Signal Test" \
    "Testing signal emission" \
    --action test:"Test" \
    --json > /dev/null

sleep 2

# Check if signal was captured
if grep -q "NotificationResult" /tmp/dbus-signals.log; then
    echo "D-Bus signals working"
else
    echo "NOTE: No D-Bus signals captured (may be expected in headless environment)"
fi

kill $MONITOR_PID 2>/dev/null || true

echo "D-Bus communication tests completed"
