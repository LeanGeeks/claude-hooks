# Task 1.1: Extension Setup

## Objective

Create a minimal working GNOME Shell extension that can be enabled and shows a test notification.

## Prerequisites

- GNOME Shell 46+ (Ubuntu 25.10 uses GNOME 48)
- `jq` for JSON formatting (optional)

## Steps

### 1. Create Extension Directory Structure

```bash
# Create the extension directory
mkdir -p ai-notification-extension/extension/{widgets,ipc,lib,icons}
cd ai-notification-extension
```

### 2. Create `metadata.json`

```json
{
  "name": "AI Notification Extension",
  "description": "Interactive notifications with action buttons for AI agent approval",
  "uuid": "ai-notification-extension@local",
  "version": 1,
  "shell-version": ["46", "47", "48"],
  "url": "https://github.com/yourusername/ai-notification-extension",
  "author": "Your Name"
}
```

### 3. Create `extension.js` (Basic Entry Point)

```javascript
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

const ExtensionUtils = imports.misc.extensionUtils;

export default class AiNotificationExtension {
    constructor() {
        this._source = null;
    }

    enable() {
        log('[AI Notification Extension] Enabling...');

        // Create a notification source
        this._source = new MessageTray.Source({
            title: 'AI Notifications',
            iconName: 'dialog-information',
        });
        Main.messageTray.add(this._source);

        // Show a test notification
        const notification = new MessageTray.Notification({
            source: this._source,
            title: 'Extension Enabled',
            body: 'AI Notification Extension is now active',
            urgency: MessageTray.Urgency.NORMAL,
        });

        notification.addAction('OK', () => {
            log('[AI Notification Extension] Test button clicked');
        });

        this._source.addNotification(notification);

        log('[AI Notification Extension] Enabled successfully');
    }

    disable() {
        log('[AI Notification Extension] Disabling...');

        if (this._source) {
            this._source.destroy();
            this._source = null;
        }

        log('[AI Notification Extension] Disabled');
    }
}
```

### 4. Create `stylesheet.css`

```css
/* Base styles for AI notifications */
.ai-notification {
    /* Styles will be added as we build custom widgets */
}
```

### 5. Create Installation Script `install.sh`

```bash
#!/bin/bash
EXTENSION_UUID="ai-notification-extension@local"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"

# Create extension directory if it doesn't exist
mkdir -p "$EXTENSION_DIR"

# Copy extension files
cp -r extension/* "$EXTENSION_DIR/"

# Log installation
echo "Extension installed to: $EXTENSION_DIR"
echo "Enable with: gnome-extensions enable $EXTENSION_UUID"
echo "Or restart GNOME Shell: Alt+F2, type 'r', press Enter"
```

### 6. Installation and Testing

```bash
# Make install script executable
chmod +x install.sh

# Install the extension
./install.sh

# Enable the extension (method 1 - via CLI)
gnome-extensions enable ai-notification-extension@local

# Or enable via GUI:
# 1. Open Extensions app (gnome-extensions-app)
# 2. Find "AI Notification Extension"
# 3. Toggle it on

# Check logs
journalctl -f --user -t gnome-shell | grep -i "ai-notification"
```

### 7. Debugging

If the extension doesn't load, check for errors:

```bash
# Check extension status
gnome-extensions list | grep ai-notification
gnome-extensions info ai-notification-extension@local

# View GNOME Shell logs
journalctl -f --user -t gnome-shell

# Use Looking Glass (Alt+F2, type 'lg', press Enter)
# - Extensions tab: check for errors
# - Evaluate: imports.misc.extensionUtils.getCurrentExtension()
```

### 8. Create Uninstall Script `uninstall.sh`

```bash
#!/bin/bash
EXTENSION_UUID="ai-notification-extension@local"
EXTENSION_DIR="$HOME/.local/share/gnome-shell/extensions/$EXTENSION_UUID"

# Disable extension first
gnome-extensions disable "$EXTENSION_UUID" 2>/dev/null || true

# Remove extension directory
rm -rf "$EXTENSION_DIR"

echo "Extension uninstalled"
```

## Acceptance Criteria

- [ ] Extension installs without errors
- [ ] Extension shows in Extensions app
- [ ] Test notification appears when extension is enabled
- [ ] Test notification has working "OK" button
- [ ] Extension can be disabled without errors
- [ ] Logs appear in `journalctl` with expected messages

## Notes

- The extension uses ES module syntax (required for GNOME 46+)
- `MessageTray.Source` creates the app icon in the notification tray
- `MessageTray.Notification` is the base notification class
- `addAction()` adds action buttons (max 3 native buttons)

## Next Task

After completing this task, move to [`01-02-ipc-communication.md`](./01-02-ipc-communication.md).
