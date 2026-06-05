# Task 1.2: IPC Communication Layer

## Objective

Establish communication between external callers and the GNOME Shell extension. This allows applications, terminals, or AI agents to send notification requests and receive results.

## Decision: D-Bus vs Unix Socket

| Factor | D-Bus | Unix Socket |
|--------|-------|-------------|
| **Integration** | Native GNOME integration | Lighter weight |
| **Activation** | Can activate extension on demand | Extension must be running |
| **Complexity** | More boilerplate, well-documented | Simpler protocol, less standard |
| **Security** | Built-in security model | Manual permission handling |
| **Debugging** `busctl`, `gdbus`, `dbus-monitor` | `nc`, `socat` | |

### Recommendation: **Start with D-Bus**

- More reliable for GNOME Shell extensions
- Better integration with system
- Can use `Gio.DBusExportedObject` wrapper
- Easier to debug with standard tools

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          D-Bus Session Bus                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Bus Name: org.gnome.Shell.Extensions.AINotifications           │
│  Object Path: /org/gnome/Shell/Extensions/AINotifications       │
│  Interface: org.gnome.Shell.Extensions.AINotifications           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
┌──────────────────┐                  ┌──────────────┐
│   Extension      │                  │   Client     │
│   (Server)       │◄─────────────────│   (Caller)   │
│                  │   Method Call     │              │
│  - Shows notifs  │   + Result Signal │  - Sends     │
│  - Returns       │◄─────────────────│    request   │
│    results       │   Result Signal   │  - Gets      │
└──────────────────┘                  │    result    │
                                      └──────────────┘
```

## D-Bus Interface Definition

### Methods

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `ShowNotification` | `(title, body, options: dict)` | `(notification_id: str)` | Show a notification, return ID |
| `GetResult` | `(notification_id: str)` | `(result: dict)` or `null` | Query result by ID |

### Signals

| Signal | Parameters | Description |
|--------|------------|-------------|
| `NotificationResult` | `(notification_id, result: dict)` | Emitted when user interacts |

### `options` Dictionary (for `ShowNotification`)

| Key | Type | Description |
|-----|------|-------------|
| `urgency` | `s` | `"low"`, `"normal"`, `"high"`, `"critical"` |
| `expire_timeout_ms` | `i` | Milliseconds until auto-close (0 = never) |
| `actions` | `aa{ss}` | Array of `{id, label}` pairs for buttons |
| `action_layout` | `s` | `"horizontal"` or `"vertical"` |
| `code_blocks` | `as` | Array of strings to format as code |
| `max_lines` | `i` | Max lines before truncation (0 = no limit) |

### `result` Dictionary (from `GetResult` or signal)

| Key | Type | Description |
|-----|------|-------------|
| `action_id` | `s` | ID of button clicked, or `"closed"`, `"expired"` |
| `timestamp` | `t` | When the result occurred |

---

## Implementation

### 1. Create `ipc/dbusService.js`

```javascript
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const AI_NOTIFICATIONS_BUS_NAME = 'org.gnome.Shell.Extensions.AINotifications';
const AI_NOTIFICATIONS_OBJECT_PATH = '/org/gnome/Shell/Extensions/AINotifications';
const AI_NOTIFICATIONS_INTERFACE = 'org.gnome.Shell.Extensions.AINotifications';

const DBusIface = `
<node>
  <interface name="${AI_NOTIFICATIONS_INTERFACE}">
    <method name="ShowNotification">
      <arg name="title" type="s" direction="in"/>
      <arg name="body" type="s" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="notification_id" type="s" direction="out"/>
    </method>
    <method name="GetResult">
      <arg name="notification_id" type="s" direction="in"/>
      <arg name="result" type="a{sv}" direction="out"/>
    </method>
    <signal name="NotificationResult">
      <arg name="notification_id" type="s"/>
      <arg name="result" type="a{sv}"/>
    </signal>
  </interface>
</node>
`;

export class DBusService {
    constructor(notificationManager) {
        this._notificationManager = notificationManager;
        this._dbusImpl = null;
        this._busId = 0;
    }

    enable() {
        const dbusProxy = Gio.DBusExportedObject.wrapJSObject(
            DBusIface,
            this
        );

        this._dbusImpl = dbusProxy;
        this._busId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            AI_NOTIFICATIONS_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            this._onBusAcquired.bind(this),
            this._onNameAcquired.bind(this),
            this._onNameLost.bind(this)
        );

        log('[AI Notification] D-Bus service enabled');
    }

    disable() {
        if (this._busId) {
            Gio.bus_unown_name(this._busId);
            this._busId = 0;
        }
        if (this._dbusImpl) {
            this._dbusImpl.unexport();
            this._dbusImpl = null;
        }
        log('[AI Notification] D-Bus service disabled');
    }

    _onBusAcquired(connection, name) {
        this._dbusImpl.export(connection, AI_NOTIFICATIONS_OBJECT_PATH);
        log(`[AI Notification] D-Bus bus acquired: ${name}`);
    }

    _onNameAcquired(connection, name) {
        log(`[AI Notification] D-Bus name acquired: ${name}`);
    }

    _onNameLost(connection, name) {
        log(`[AI Notification] D-Bus name lost: ${name}`);
    }

    /**
     * D-Bus Method: ShowNotification
     * @param {string} title - Notification title
     * @param {string} body - Notification body
     * @param {object} options - Options dictionary
     * @returns {string} notification_id
     */
    ShowNotification(title, body, options) {
        log(`[AI Notification] ShowNotification: ${title}`);

        const notificationId = this._notificationManager.showNotification({
            title,
            body,
            urgency: options['urgency']?.unpack() || 'normal',
            expireTimeoutMs: options['expire_timeout_ms']?.unpack() || 0,
            actions: this._unpackActions(options['actions']?.unpack()),
            actionLayout: options['action_layout']?.unpack() || 'horizontal',
            codeBlocks: options['code_blocks']?.unpack() || [],
            maxLines: options['max_lines']?.unpack() || 0,
        });

        return notificationId;
    }

    /**
     * D-Bus Method: GetResult
     * @param {string} notificationId - ID of notification to query
     * @returns {object|null} Result dictionary or null if no result yet
     */
    GetResult(notificationId) {
        log(`[AI Notification] GetResult: ${notificationId}`);
        const result = this._notificationManager.getResult(notificationId);
        return result ? this._packResult(result) : null;
    }

    /**
     * Emit a result signal
     * @param {string} notificationId - ID of notification
     * @param {object} result - Result to emit
     */
    emitResult(notificationId, result) {
        this._dbusImpl.emit_signal(
            'NotificationResult',
            new GLib.Variant('(sa{sv})', [notificationId, this._packResult(result)])
        );
    }

    _unpackActions(actionsVariant) {
        if (!actionsVariant) return [];
        // Unpack GVariant array of {id, label} dictionaries
        return actionsVariant.recursiveUnpack();
    }

    _packResult(result) {
        return new GLib.Variant('a{sv}', {
            action_id: new GLib.Variant('s', result.actionId),
            timestamp: new GLib.Variant('t', result.timestamp),
        });
    }
}
```

### 2. Create `lib/notificationManager.js` (Skeleton)

```javascript
import GLib from 'gi://GLib';

export class NotificationManager {
    constructor(dbusService) {
        this._dbusService = dbusService;
        this._notifications = new Map(); // id -> { notification, result }
        this._nextId = 0;
    }

    /**
     * Show a notification
     * @param {object} options - Notification options
     * @returns {string} notification ID
     */
    showNotification(options) {
        const id = `notif-${Date.now()}-${this._nextId++}`;
        log(`[AI Notification] Creating notification ${id}`);

        // TODO: Create and show the actual notification widget
        // This will be implemented in task 2.1

        this._notifications.set(id, {
            options,
            result: null,
        });

        return id;
    }

    /**
     * Get result for a notification
     * @param {string} id - Notification ID
     * @returns {object|null} Result or null
     */
    getResult(id) {
        const notification = this._notifications.get(id);
        return notification?.result || null;
    }

    /**
     * Set result for a notification
     * @param {string} id - Notification ID
     * @param {object} result - Result to set
     */
    setResult(id, result) {
        const notification = this._notifications.get(id);
        if (notification) {
            notification.result = {
                ...result,
                timestamp: Date.now(),
            };

            // Emit D-Bus signal
            this._dbusService.emitResult(id, notification.result);

            log(`[AI Notification] Result for ${id}: ${result.actionId}`);
        }
    }

    /**
     * Remove a notification
     * @param {string} id - Notification ID
     */
    removeNotification(id) {
        this._notifications.delete(id);
    }
}
```

### 3. Update `extension.js`

```javascript
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

// Import our modules
import { DBusService } from './ipc/dbusService.js';
import { NotificationManager } from './lib/notificationManager.js';

export default class AiNotificationExtension {
    constructor() {
        this._source = null;
        this._dbusService = null;
        this._notificationManager = null;
    }

    enable() {
        log('[AI Notification Extension] Enabling...');

        // Create notification source
        this._source = new MessageTray.Source({
            title: 'AI Notifications',
            iconName: 'dialog-information',
        });
        Main.messageTray.add(this._source);

        // Initialize D-Bus service
        this._notificationManager = new NotificationManager(null); // Will set dbusService after
        this._dbusService = new DBusService(this._notificationManager);
        this._notificationManager._dbusService = this._dbusService;
        this._dbusService.enable();

        log('[AI Notification Extension] Enabled successfully');
    }

    disable() {
        log('[AI Notification Extension] Disabling...');

        if (this._dbusService) {
            this._dbusService.disable();
            this._dbusService = null;
        }

        this._notificationManager = null;

        if (this._source) {
            this._source.destroy();
            this._source = null;
        }

        log('[AI Notification Extension] Disabled');
    }
}
```

### 4. Create Test Client `client/test-dbus.sh`

```bash
#!/bin/bash

BUS_NAME="org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH="/org/gnome/Shell/Extensions/AINotifications"
INTERFACE="org.gnome.Shell.Extensions.AINotifications"

# Test showing a notification
echo "Sending test notification..."

gdbus call --session \
    --dest "$BUS_NAME" \
    --object-path "$OBJECT_PATH" \
    --method "$INTERFACE.ShowNotification" \
    "'Test Title'" \
    "'This is a test notification from D-Bus'" \
    "{'urgency': <'normal'>, 'actions': <[{'id': <'approve'>, 'label': <'Approve'>}, {'id': <'deny'>, 'label': <'Deny'>}]>}"

echo ""
echo "Listening for results (press Ctrl+C to stop)..."

# Listen for result signals
gdbus monitor --session \
    --dest "$BUS_NAME" \
    --object-path "$OBJECT_PATH" \
    | grep --line-buffered "NotificationResult"
```

### 5. Create Python Client `client/notify-interactive`

```python
#!/usr/bin/env python3
import argparse
import json
import sys
import time
from gi.repository import GLib, Gio

BUS_NAME = "org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH = "/org/gnome/Shell/Extensions/AINotifications"
INTERFACE = "org.gnome.Shell.Extensions.AINotifications"

class NotificationClient:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.results = {}

    def show_notification(self, title, body, options=None):
        """Show a notification and return its ID"""
        if options is None:
            options = {}

        # Build variant for options
        options_builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))

        for key, value in options.items():
            if key == "actions":
                # Array of {id, label} dictionaries
                actions = []
                for action in value:
                    action_dict = GLib.Variant("a{ss}", action)
                    actions.append(action_dict)
                options_builder.add("{sv}", key, GLib.Variant("aa{ss}", actions))
            elif key == "urgency":
                options_builder.add("{sv}", key, GLib.Variant("s", value))
            elif key == "expire_timeout_ms":
                options_builder.add("{sv}", key, GLib.Variant("i", value))
            elif key == "action_layout":
                options_builder.add("{sv}", key, GLib.Variant("s", value))
            elif key == "max_lines":
                options_builder.add("{sv}", key, GLib.Variant("i", value))

        # Call the method
        result = self.bus.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "ShowNotification",
            GLib.Variant("(ssa{sv})", (title, body, options_builder.end())),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None
        )

        notification_id = result.unpack()[0]
        return notification_id

    def get_result(self, notification_id, timeout=300):
        """Wait for result with timeout"""
        start_time = time.time()

        # Subscribe to signals
        signal_id = self.bus.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            "NotificationResult",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_result,
            None
        )

        try:
            # Poll for existing result
            while time.time() - start_time < timeout:
                result = self._poll_result(notification_id)
                if result:
                    return result

                time.sleep(0.1)

            return {"action_id": "expired", "timestamp": int(time.time() * 1000)}
        finally:
            self.bus.signal_unsubscribe(signal_id)

    def _on_result(self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        notification_id, result = parameters.unpack()
        self.results[notification_id] = result

    def _poll_result(self, notification_id):
        # Check local cache first
        if notification_id in self.results:
            return self.results[notification_id]

        # Query via D-Bus
        result = self.bus.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "GetResult",
            GLib.Variant("(s)", (notification_id,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None
        )

        if result:
            return result.unpack()[0]
        return None


def main():
    parser = argparse.ArgumentParser(description="Send interactive notifications")
    parser.add_argument("title", help="Notification title")
    parser.add_argument("body", help="Notification body")
    parser.add_argument("--urgency", choices=["low", "normal", "high", "critical"], default="normal")
    parser.add_argument("--expire", type=int, default=0, help="Expire timeout in milliseconds")
    parser.add_argument("--action", action="append", nargs=2, metavar=("ID", "LABEL"),
                        help="Add action button (can be used multiple times)")
    parser.add_argument("--wait", action="store_true", help="Wait for user interaction")
    parser.add_argument("--timeout", type=int, default=300, help="Max time to wait (seconds, default: 300)")

    args = parser.parse_args()

    client = NotificationClient()

    options = {"urgency": args.urgency}
    if args.expire > 0:
        options["expire_timeout_ms"] = args.expire
    if args.action:
        options["actions"] = [{"id": id, "label": label} for id, label in args.action]

    notification_id = client.show_notification(args.title, args.body, options)
    print(f"Notification ID: {notification_id}")

    if args.wait:
        print("Waiting for user response...")
        result = client.get_result(notification_id, args.timeout)
        action_id = result.get("action_id", "unknown")
        print(f"Result: {action_id}")
        sys.exit(0 if action_id == "approve" else 1)


if __name__ == "__main__":
    main()
```

## Testing

```bash
# Make test scripts executable
chmod +x client/test-dbus.sh
chmod +x client/notify-interactive

# Test with bash/gdbus
./client/test-dbus.sh

# Test with Python client
./client/notify-interactive \
    "Deployment Approval" \
    "Deploy to production?" \
    --urgency high \
    --action approve "Approve" \
    --action deny "Deny" \
    --wait

# Monitor D-Bus traffic
dbus-monitor --session "interface=org.gnome.Shell.Extensions.AINotifications"
```

## Acceptance Criteria

- [ ] D-Bus service starts when extension is enabled
- [ ] Bus name `org.gnome.Shell.Extensions.AINotifications` is registered
- [ ] `ShowNotification` method accepts parameters and returns notification ID
- [ ] `GetResult` method returns results
- [ ] `NotificationResult` signal is emitted when user interacts
- [ ] Test client can send notifications and receive results
- [ ] Extension logs show D-Bus activity

## Notes

- Use `gdbus` or `busctl` for debugging D-Bus issues
- D-Bus activation is not used; extension must be running
- Signal emission happens immediately when user interacts
- Results are cached in memory until queried

## Next Task

After completing this task, move to [`01-03-notification-manager.md`](./01-03-notification-manager.md) to implement actual notification display.
