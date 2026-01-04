#!/bin/bash

BUS_NAME="org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH="/org/gnome/Shell/Extensions/AINotifications"
INTERFACE="org.gnome.Shell.Extensions.AINotifications"

# Test showing a notification
echo "Sending test notification..."

# Capture the notification ID from the response
NOTIFICATION_ID=$(gdbus call --session \
    --dest "$BUS_NAME" \
    --object-path "$OBJECT_PATH" \
    --method "$INTERFACE.ShowNotification" \
    "'Test Title'" \
    "'This is a test notification from D-Bus'" \
    "{'urgency': <'normal'>, 'actions': <[{'id': <'approve'>, 'label': <'Approve'>}, {'id': <'deny'>, 'label': <'Deny'>}]>}" \
    2>&1 | grep -oP "'([^']+)'" | head -1 | tr -d "'")

if [ -n "$NOTIFICATION_ID" ]; then
    echo ""
    echo "Notification created successfully!"
    echo "Notification ID: $NOTIFICATION_ID"
else
    echo ""
    echo "Failed to create notification or unable to parse ID"
    echo "Raw response above may contain the ID"
fi

echo ""
echo "Listening for results (press Ctrl+C to stop)..."

# Listen for result signals
gdbus monitor --session \
    --dest "$BUS_NAME" \
    --object-path "$OBJECT_PATH" \
    | grep --line-buffered "NotificationResult"
