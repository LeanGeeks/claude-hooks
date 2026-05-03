#!/bin/bash
echo "=== Checking all D-Bus names ==="
echo ""
dbus-send --session --dest=org.freedesktop.DBus --type=method_call --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>&1 | grep -i "shell\|extension\|notification" || echo "No matching names found"
