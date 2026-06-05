# Task 1.3: Notification Manager

## Objective

Implement the core notification management logic that displays notifications using GNOME's `MessageTray.Notification` API. This handles basic notifications with horizontal button layout.

## Previous Tasks

- [ ] [`01-01-extension-setup.md`](./01-01-extension-setup.md) - Extension structure
- [ ] [`01-02-ipc-communication.md`](./01-02-ipc-communication.md) - D-Bus interface

## Implementation

### 1. Create `lib/notificationManager.js` (Complete)

```javascript
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';

export class NotificationManager {
    constructor(dbusService) {
        this._dbusService = dbusService;
        this._notifications = new Map(); // id -> { notification, result, source }
        this._nextId = 0;
    }

    /**
     * Show a notification
     * @param {object} options - Notification options
     * @returns {string} notification ID
     */
    showNotification(options) {
        const id = `notif-${Date.now()}-${this._nextId++}`;
        log(`[AI Notification] Creating notification ${id}: ${options.title}`);

        // Create or get notification source
        const source = this._getOrCreateSource(options.title);

        // Build notification body with code blocks
        const body = this._formatBody(options.body || '', options.codeBlocks || []);

        // Create notification
        const notification = new MessageTray.Notification({
            source: source,
            title: options.title,
            body: body,
            urgency: this._mapUrgency(options.urgency || 'normal'),
        });

        // Handle notification destruction
        notification.connect('destroy', (notif, reason) => {
            this._onNotificationDestroyed(id, reason);
        });

        // Handle notification activation (clicked)
        notification.connect('activated', (notif) => {
            this.setResult(id, { actionId: 'activated' });
        });

        // Add action buttons
        const actions = options.actions || [];
        if (actions.length > 0) {
            // For horizontal layout, use native addAction (max 3 buttons)
            for (const action of actions.slice(0, 3)) {
                notification.addAction(action.label, () => {
                    this.setResult(id, { actionId: action.id });
                });
            }
        }

        // Set expire timeout
        if (options.expireTimeoutMs > 0) {
            notification.setForReaction(options.expireTimeoutMs);
            // TODO: In task 2.2, add countdown indicator
        }

        // Store notification
        this._notifications.set(id, {
            notification,
            source,
            result: null,
            options,
        });

        // Show the notification
        source.addNotification(notification);

        log(`[AI Notification] Notification ${id} displayed`);
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
        const data = this._notifications.get(id);
        if (data && !data.result) {
            data.result = {
                ...result,
                timestamp: Date.now(),
            };

            // Emit D-Bus signal
            if (this._dbusService) {
                this._dbusService.emitResult(id, data.result);
            }

            // Remove notification after a delay
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
                this.removeNotification(id);
                return GLib.SOURCE_REMOVE;
            });

            log(`[AI Notification] Result for ${id}: ${result.actionId}`);
        }
    }

    /**
     * Remove a notification
     * @param {string} id - Notification ID
     */
    removeNotification(id) {
        const data = this._notifications.get(id);
        if (data) {
            data.notification.destroy();
            this._notifications.delete(id);
            log(`[AI Notification] Removed notification ${id}`);
        }
    }

    /**
     * Get or create notification source
     */
    _getOrCreateSource(sourceName) {
        // Use system source for simplicity
        return MessageTray.getSystemSource();
    }

    /**
     * Format body with code blocks
     */
    _formatBody(body, codeBlocks) {
        let formatted = body;

        // Simple code block formatting using Pango markup
        // Code blocks are wrapped in <tt> tags (monospace)
        if (codeBlocks.length > 0) {
            formatted += '\n\n';
            for (const code of codeBlocks) {
                formatted += `<tt>${code}</tt>\n`;
            }
        }

        return formatted;
    }

    /**
     * Map urgency string to MessageTray.Urgency
     */
    _mapUrgency(urgency) {
        switch (urgency) {
            case 'low':
                return MessageTray.Urgency.LOW;
            case 'high':
                return MessageTray.Urgency.HIGH;
            case 'critical':
                return MessageTray.Urgency.CRITICAL;
            default:
                return MessageTray.Urgency.NORMAL;
        }
    }

    /**
     * Handle notification destruction
     */
    _onNotificationDestroyed(id, reason) {
        const data = this._notifications.get(id);

        if (data && !data.result) {
            let actionId;

            switch (reason) {
                case MessageTray.NotificationDestroyedReason.EXPIRED:
                    actionId = 'expired';
                    break;
                case MessageTray.NotificationDestroyedReason.DISMISSED:
                    actionId = 'closed';
                    break;
                case MessageTray.NotificationDestroyedReason.SOURCE_CLOSED:
                    actionId = 'source_closed';
                    break;
                case MessageTray.NotificationDestroyedReason.REPLACED:
                    actionId = 'replaced';
                    break;
                default:
                    actionId = 'unknown';
            }

            this.setResult(id, { actionId });
        }

        // Remove from map after a delay
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 5000, () => {
            this._notifications.delete(id);
            return GLib.SOURCE_REMOVE;
        });
    }
}
```

### 2. Update `ipc/dbusService.js` (Minor Update)

Update the `ShowNotification` method to handle the body formatting:

```javascript
    ShowNotification(title, body, options) {
        log(`[AI Notification] ShowNotification: ${title}`);

        // Unpack code blocks
        const codeBlocksVariant = options['code_blocks'];
        let codeBlocks = [];
        if (codeBlocksVariant) {
            codeBlocks = codeBlocksVariant.deepUnpack();
        }

        const notificationId = this._notificationManager.showNotification({
            title,
            body,
            urgency: options['urgency']?.unpack() || 'normal',
            expireTimeoutMs: options['expire_timeout_ms']?.unpack() || 0,
            actions: this._unpackActions(options['actions']?.unpack()),
            actionLayout: options['action_layout']?.unpack() || 'horizontal',
            codeBlocks,
            maxLines: options['max_lines']?.unpack() || 0,
        });

        return notificationId;
    }
```

### 3. Update Python Client to Support Code Blocks

```python
#!/usr/bin/env python3
# ... (previous imports) ...

def main():
    parser = argparse.ArgumentParser(description="Send interactive notifications")
    parser.add_argument("title", help="Notification title")
    parser.add_argument("body", help="Notification body")
    parser.add_argument("--urgency", choices=["low", "normal", "high", "critical"], default="normal")
    parser.add_argument("--expire", type=int, default=0, help="Expire timeout in milliseconds")
    parser.add_argument("--action", action="append", nargs=2, metavar=("ID", "LABEL"),
                        help="Add action button (can be used multiple times)")
    parser.add_argument("--code", action="append", help="Add code block (can be used multiple times)")
    parser.add_argument("--wait", action="store_true", help="Wait for user interaction")
    parser.add_argument("--timeout", type=int, default=300, help="Max time to wait (seconds, default: 300)")

    args = parser.parse_args()

    client = NotificationClient()

    options = {"urgency": args.urgency}
    if args.expire > 0:
        options["expire_timeout_ms"] = args.expire
    if args.action:
        options["actions"] = [{"id": id, "label": label} for id, label in args.action]
    if args.code:
        options["code_blocks"] = args.code

    notification_id = client.show_notification(args.title, args.body, options)
    print(f"Notification ID: {notification_id}")

    if args.wait:
        print("Waiting for user response...")
        result = client.get_result(notification_id, args.timeout)
        action_id = result.get("action_id", "unknown")
        print(f"Result: {action_id}")
        sys.exit(0 if action_id == "approve" else 1)
```

## Testing Scenarios

### Test 1: Basic Notification

```bash
./client/notify-interactive \
    "Hello" \
    "This is a test notification" \
    --wait
```

### Test 2: Notification with Actions

```bash
./client/notify-interactive \
    "Deploy to Production?" \
    "Confirm deployment to production environment" \
    --urgency high \
    --action approve "Deploy" \
    --action deny "Cancel" \
    --wait
```

### Test 3: Notification with Code Blocks

```bash
./client/notify-interactive \
    "Code Review Request" \
    "Please review the following changes:" \
    --code "const x = 42;" \
    --code "function foo() { return x; }" \
    --action approve "LGTM" \
    --action deny "Request Changes" \
    --wait
```

### Test 4: Notification with Expiry

```bash
./client/notify-interactive \
    "Auto-close Test" \
    "This will close in 10 seconds" \
    --expire 10000 \
    --wait
```

### Test 5: Multiple Notifications

```bash
for i in {1..3}; do
    ./client/notify-interactive \
        "Notification $i" \
        "Test notification number $i" \
        --action ok "OK" &
done
```

## Acceptance Criteria

- [ ] Basic notifications appear with title and body
- [ ] Action buttons (up to 3) appear horizontally
- [ ] Clicking action buttons returns the correct action ID
- [ ] Closing notification (dismiss) returns action ID "closed"
- [ ] Expired notifications return action ID "expired"
- [ ] Code blocks are displayed in monospace font
- [ ] Urgency levels affect notification behavior
- [ ] Multiple notifications can be displayed simultaneously
- [ ] D-Bus signals are emitted for all result types

## Known Limitations (To be addressed in Phase 2)

1. **Long content truncation** - Currently just shows as-is, no truncation
2. **Vertical button layout** - Only horizontal (max 3 buttons) supported
3. **Countdown indicator** - Expiring notifications don't show visual countdown
4. **Advanced code formatting** - Basic monospace only, no syntax highlighting

## Next Task

After completing this task, move to Phase 2:
- [`02-01-custom-notification-widget.md`](./02-01-custom-notification-widget.md) - Vertical button layout
- [`02-02-countdown-indicator.md`](./02-02-countdown-indicator.md) - Countdown circle
- [`02-03-code-block-formatting.md`](./02-03-code-block-formatting.md) - Better formatting
