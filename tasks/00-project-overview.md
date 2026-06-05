# GNOME Shell Extension: Interactive Notifications with Buttons

## Project Goal

Create a GNOME Shell extension that displays interactive notifications with action buttons, enabling AI agents (or other applications) to request user approval/input through desktop notifications.

## User Requirements

| # | Requirement | Status | Notes |
|---|-------------|--------|-------|
| 1 | Multi-line notifications with code blocks and indentation | ✅ | Use Pango markup for formatting |
| 2 | Truncate long content with "..." | ✅ | Simple truncation, full text in drawer |
| 3 | Horizontal action buttons (yes/no, approve/deny) | ✅ | Native support (max 3 buttons) |
| 4 | Vertical list of buttons (up to 5 options, 1-2 lines each) | ⚠️ | Requires custom widget |
| 5 | Notifications with expire time + countdown circle indicator | ⚠️ | Requires custom progress widget |
| 6 | Return result to caller (button clicked, closed, expired) | ✅ | Via IPC (D-Bus or socket) |

## Architecture Overview

```
┌─────────────────┐         IPC          ┌──────────────────────┐
│   Caller        │ <─────────────────> │   GNOME Extension    │
│  (Terminal/     │   D-Bus or Socket   │   (Shell Extension)  │
│   App/Agent)    │                      │                      │
└─────────────────┘                      └──────────────────────┘
                                                  │
                                                  ▼
                                          ┌──────────────┐
                                          │ Notification │
                                          │    Widget    │
                                          └──────────────┘
```

## Components

### 1. GNOME Shell Extension
- **Entry point**: `extension.js`
- **Notification widget**: Custom widget supporting vertical button layout
- **Countdown indicator**: Circular progress for expiring notifications
- **IPC Service**: D-Bus or Unix socket for external communication

### 2. Client Library/CLI
- Python or Bash CLI tool for easy testing
- Library for programmatic access
- Handles IPC communication

### 3. Shared Protocol
- JSON-based message format
- Request/Response pattern
- Notification ID tracking

## Technical Decisions (Deferred)

| Area | Options | Decision |
|------|---------|----------|
| **IPC Method** | D-Bus (signals/polling) / Unix socket | TBD - choose easiest reliable option |
| **Button Layout** | Native horizontal / Custom vertical | Hybrid - use native for ≤3 buttons, custom for 4-5 |
| **Countdown Widget** | Shell drawing / Clutter canvas | TBD during implementation |

## File Structure

```
ai-notification-extension/
├── extension/
│   ├── extension.js              # Main extension entry point
│   ├── metadata.json             # Extension metadata
│   ├── prefs.js                  # Settings (optional)
│   ├── stylesheet.css            # Custom styles
│   ├── widgets/
│   │   ├── notificationWidget.js # Custom notification with vertical buttons
│   │   └── countdownIndicator.js  # Circular countdown widget
│   ├── ipc/
│   │   ├── dbusService.js         # D-Bus interface (option A)
│   │   └── socketServer.js        # Unix socket server (option B)
│   └── lib/
│       ├── notificationManager.js # Manages active notifications
│       └── resultTracker.js       # Tracks notification results
├── client/
│   ├── notify-interactive         # CLI tool
│   └── libnotify-interactive/     # Client library
└── tasks/                         # This folder
```

## Task Files

1. [`01-01-extension-setup.md`](./01-01-extension-setup.md) - Basic extension structure
2. [`01-02-ipc-communication.md`](./01-02-ipc-communication.md) - IPC layer (D-Bus or socket)
3. [`01-03-notification-manager.md`](./01-03-notification-manager.md) - Core notification management
4. [`02-01-custom-notification-widget.md`](./02-01-custom-notification-widget.md) - Vertical button layout
5. [`02-02-countdown-indicator.md`](./02-02-countdown-indicator.md) - Expiry countdown circle
6. [`02-03-code-block-formatting.md`](./02-03-code-block-formatting.md) - Pango markup for code
7. [`03-01-client-cli.md`](./03-01-client-cli.md) - CLI tool for testing/usage
8. [`03-02-client-library.md`](./03-02-client-library.md) - Python client library
9. [`04-01-testing.md`](./04-01-testing.md) - Test scenarios and validation

## Dependencies

- GNOME Shell 46+ (Ubuntu 25.10 uses GNOME 48)
- GJS (ES modules support)
- Clutter (for custom widgets)
- D-Bus (if using D-Bus IPC)

## Next Steps

Start with [`01-01-extension-setup.md`](./01-01-extension-setup.md) to create the basic extension structure and get it running.
