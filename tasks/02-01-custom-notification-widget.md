# Task 2.1: Custom Notification Widget (Vertical Buttons)

## Objective

Create a custom notification widget that supports vertical button layout for notifications with 4-5 action buttons. This extends GNOME's `MessageTray.Notification` with custom rendering.

## Why This Is Complex

GNOME Shell's `MessageTray.Notification` class only supports horizontal action buttons (max 3). To support vertical layout, we need to:

1. Subclass `MessageTray.Notification` or create a custom widget
2. Override the banner creation logic
3. Manually handle button layout and event dispatch
4. Maintain compatibility with the notification drawer

## Implementation Strategy

We'll create a custom notification class that:
- Extends `MessageTray.Notification`
- Overrides banner content creation to add custom button container
- Detects action count and switches between horizontal/vertical layout

---

## Code

### 1. Create `widgets/customNotification.js`

```javascript
import GObject from 'gi://GObject';
import Clutter from 'gi://Clutter';
import St from 'gi://St';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

/**
 * Custom notification with vertical button support
 */
export const CustomNotification = GObject.registerClass({
    GTypeName: 'AINotificationCustomNotification',
}, class CustomNotification extends MessageTray.Notification {
    constructor(params) {
        super(params);

        this._actionLayout = params.actionLayout || 'horizontal';
        this._verticalButtonBox = null;
    }

    /**
     * Create banner content - override to add custom button layout
     */
    createBanner() {
        // Call parent to create basic banner
        const banner = super.createBanner();

        // If we have many actions, use vertical layout
        if (this._actionLayout === 'vertical' && this._actions.length > 3) {
            this._setupVerticalButtons(banner);
        }

        return banner;
    }

    /**
     * Set up vertical button layout
     */
    _setupVerticalButtons(banner) {
        // Find and hide the default button box
        const defaultButtonBox = banner._buttonBox;
        if (defaultButtonBox) {
            defaultButtonBox.hide();
        }

        // Create vertical button container
        const verticalBox = new St.BoxLayout({
            vertical: true,
            style_class: 'ai-notification-button-container',
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
            margin_top: 8,
            spacing: 4,
        });

        // Add buttons vertically
        for (let i = 0; i < this._actions.length; i++) {
            const action = this._actions[i];
            const button = this._createVerticalButton(action.label, i);
            verticalBox.add_child(button);
        }

        // Add to banner
        banner.actor.add_child(verticalBox);
        this._verticalButtonBox = verticalBox;
    }

    /**
     * Create a single vertical action button
     */
    _createVerticalButton(label, index) {
        const button = new St.Button({
            label: label,
            style_class: 'ai-notification-button',
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.CENTER,
            can_focus: true,
        });

        button.connect('clicked', () => {
            this._emitAction(index);
        });

        // Add hover effect
        button.connect('notify::hover', () => {
            if (button.hover) {
                button.add_style_pseudo_class('hover');
            } else {
                button.remove_style_pseudo_class('hover');
            }
        });

        return button;
    }

    /**
     * Emit action signal
     */
    _emitAction(index) {
        if (this._actions && this._actions[index]) {
            const action = this._actions[index];
            action.callback();
        }
    }
});
```

### 2. Update `stylesheet.css`

```css
/* AI Notification Extension Styles */

.ai-notification-button-container {
    min-width: 180px;
}

.ai-notification-button {
    background-color: rgba(53, 132, 228, 0.1);
    border: 1px solid rgba(53, 132, 228, 0.3);
    border-radius: 6px;
    padding: 8px 16px;
    color: #3584e4;
    font-weight: 500;
    transition: all 150ms ease;
}

.ai-notification-button:hover {
    background-color: rgba(53, 132, 228, 0.2);
    border-color: rgba(53, 132, 228, 0.6);
}

.ai-notification-button:active {
    background-color: rgba(53, 132, 228, 0.3);
    transform: translateY(1px);
}

/* Dark theme support */
.ai-notification-button.dark {
    background-color: rgba(92, 167, 255, 0.15);
    border-color: rgba(92, 167, 255, 0.4);
    color: #5ca7ff;
}

.ai-notification-button.dark:hover {
    background-color: rgba(92, 167, 255, 0.25);
    border-color: rgba(92, 167, 255, 0.6);
}
```

### 3. Update `lib/notificationManager.js`

Import and use the custom notification:

```javascript
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';
import { CustomNotification } from '../widgets/customNotification.js';

export class NotificationManager {
    // ... (previous code) ...

    showNotification(options) {
        const id = `notif-${Date.now()}-${this._nextId++}`;
        log(`[AI Notification] Creating notification ${id}: ${options.title}`);

        // Determine button layout
        const actionLayout = options.actionLayout || 'horizontal';
        const actions = options.actions || [];
        const useVertical = actionLayout === 'vertical' || actions.length > 3;

        // Create or get notification source
        const source = this._getOrCreateSource(options.title);

        // Build notification body with code blocks
        const body = this._formatBody(options.body || '', options.codeBlocks || []);

        // Prepare action callbacks
        const actionCallbacks = [];
        for (const action of actions) {
            actionCallbacks.push({
                label: action.label,
                callback: () => {
                    this.setResult(id, { actionId: action.id });
                },
            });
        }

        // Create notification (custom or standard)
        let notification;
        if (useVertical && actions.length > 0) {
            // Use custom notification for vertical buttons
            notification = new CustomNotification({
                source: source,
                title: options.title,
                body: body,
                urgency: this._mapUrgency(options.urgency || 'normal'),
                actionLayout: 'vertical',
            });

            // Add actions to the custom notification
            for (const callback of actionCallbacks) {
                notification.addAction(callback.label, callback.callback);
            }
        } else {
            // Use standard notification for horizontal buttons
            notification = new MessageTray.Notification({
                source: source,
                title: options.title,
                body: body,
                urgency: this._mapUrgency(options.urgency || 'normal'),
            });

            // Add up to 3 action buttons horizontally
            for (const callback of actionCallbacks.slice(0, 3)) {
                notification.addAction(callback.label, callback.callback);
            }
        }

        // Handle notification destruction
        notification.connect('destroy', (notif, reason) => {
            this._onNotificationDestroyed(id, reason);
        });

        // Handle notification activation
        notification.connect('activated', (notif) => {
            this.setResult(id, { actionId: 'activated' });
        });

        // Set expire timeout
        if (options.expireTimeoutMs > 0) {
            notification.setForReaction(options.expireTimeoutMs);
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

        log(`[AI Notification] Notification ${id} displayed (${actionLayout} layout)`);
        return id;
    }

    // ... (rest of the code remains the same) ...
}
```

### 4. Update Python Client for Vertical Layout

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
    parser.add_argument("--layout", choices=["horizontal", "vertical"], default="horizontal",
                        help="Button layout (default: horizontal)")
    parser.add_argument("--code", action="append", help="Add code block")
    parser.add_argument("--wait", action="store_true", help="Wait for user interaction")
    parser.add_argument("--timeout", type=int, default=300, help="Max time to wait (seconds)")

    args = parser.parse_args()

    client = NotificationClient()

    options = {"urgency": args.urgency, "action_layout": args.layout}
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

## Testing

### Test 1: Vertical Layout with 4 Options

```bash
./client/notify-interactive \
    "Background Style" \
    "How should the empty area be displayed?" \
    --layout vertical \
    --action white "White background" \
    --action transparent "Transparent background" \
    --action inherit "Inherit from parent" \
    --action gradient "Gradient" \
    --wait
```

### Test 2: Vertical Layout with 5 Options

```bash
./client/notify-interactive \
    "Color Scheme" \
    "Select a color scheme:" \
    --layout vertical \
    --action light "Light" \
    --action dark "Dark" \
    --action auto "Auto (system)" \
    --action sepia "Sepia" \
    --action grayscale "Grayscale" \
    --wait
```

### Test 3: Auto-switch to Vertical (4+ actions)

```bash
# Without explicit --layout, should auto-switch to vertical
./client/notify-interactive \
    "Choose Option" \
    "Please select one:" \
    --action option1 "Option 1" \
    --action option2 "Option 2" \
    --action option3 "Option 3" \
    --action option4 "Option 4" \
    --wait
```

### Test 4: Horizontal Layout (3 or fewer)

```bash
./client/notify-interactive \
    "Confirm Action" \
    "Are you sure?" \
    --layout horizontal \
    --action yes "Yes" \
    --action no "No" \
    --wait
```

## Acceptance Criteria

- [ ] Horizontal layout works for 1-3 buttons (native behavior)
- [ ] Vertical layout works for 4-5 buttons
- [ ] Vertical buttons are styled consistently
- [ ] Buttons are clickable and return correct action IDs
- [ ] Notification banner looks good in both layouts
- [ ] Auto-switch to vertical when >3 actions without explicit layout
- [ ] Styles work in both light and dark themes

## Known Issues & Limitations

1. **Banner width** - Very long button labels may overflow
2. **Drawer view** - Custom buttons may not appear correctly in notification drawer
3. **Accessibility** - Keyboard navigation may need additional work
4. **Animation** - Button animations use basic CSS transitions

## Next Task

After completing this task, move to [`02-02-countdown-indicator.md`](./02-02-countdown-indicator.md) for the countdown circle widget.
