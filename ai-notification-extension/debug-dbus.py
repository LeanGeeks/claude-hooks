#!/usr/bin/env python3
import gi
gi.require_version('GLib', '2.0')
gi.require_version('Gio', '2.0')
from gi.repository import GLib, Gio

BUS_NAME = 'org.gnome.Shell.Extensions.AINotifications'
OBJECT_PATH = '/org/gnome/Shell/Extensions/AINotifications'
INTERFACE = 'org.gnome.Shell.Extensions.AINotifications'

# Create D-Bus connection
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

# Check if service exists
print(f"Checking for {BUS_NAME}...")

# Try to call the method
try:
    # Pass empty dict directly - GLib will convert it
    result = bus.call_sync(
        BUS_NAME,
        OBJECT_PATH,
        INTERFACE,
        "ShowNotification",
        GLib.Variant("(ssa{sv})", ("Debug", "Test notification", {})),
        GLib.VariantType("(s)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )

    if result:
        notification_id = result.unpack()[0]
        print(f"✓ Success! Notification ID: {notification_id}")
    else:
        print("✗ No result returned")
except Exception as e:
    print(f"✗ Error: {e}")
    print(f"   Error type: {type(e).__name__}")
    if hasattr(e, 'code'):
        print(f"   Error code: {e.code}")
    if hasattr(e, 'message'):
        print(f"   Error message: {e.message}")
