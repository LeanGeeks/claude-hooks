# Task 2.3: Code Block Formatting and Long Content Truncation

## Objective

Improve notification body formatting to support:
1. Code blocks with proper indentation
2. Truncation of long content with "..." indicator
3. Multi-line text formatting

## Implementation Strategy

GNOME Shell uses Pango markup for text formatting. We'll:
1. Use `<tt>` tags for monospace code blocks
2. Implement line counting for truncation
3. Use `\n` for newlines
4. Escape special characters properly

---

## Code

### 1. Create `lib/textFormatter.js`

```javascript
import GLib from 'gi://GLib';

/**
 * Text formatter for notifications
 * Handles code blocks, truncation, and Pango markup
 */
export class TextFormatter {
    constructor(options = {}) {
        this.maxLines = options.maxLines || 0; // 0 = no limit
        this.maxChars = options.maxChars || 500; // Soft limit
        this.codeStyle = options.codeStyle || 'pango'; // 'pango' or 'plain'
    }

    /**
     * Format notification body with code blocks
     * @param {string} body - Main body text
     * @param {string[]} codeBlocks - Array of code strings
     * @param {object} options - Formatting options
     * @returns {string} Formatted body
     */
    formatBody(body, codeBlocks = [], options = {}) {
        const maxLines = options.maxLines ?? this.maxLines;
        const truncate = options.truncate ?? true;

        let formatted = this._escapeText(body);

        // Add code blocks
        if (codeBlocks && codeBlocks.length > 0) {
            formatted += '\n\n';
            for (let i = 0; i < codeBlocks.length; i++) {
                const code = this._formatCodeBlock(codeBlocks[i], i);
                formatted += code;
            }
        }

        // Truncate if needed
        if (truncate && maxLines > 0) {
            formatted = this._truncateToLines(formatted, maxLines);
        }

        return formatted;
    }

    /**
     * Format a single code block
     */
    _formatCodeBlock(code, index = 0) {
        // Trim leading/trailing whitespace but preserve indentation
        const trimmed = code.trim();

        // Use Pango monospace tag
        if (this.codeStyle === 'pango') {
            // Escape the code content
            const escaped = this._escapeText(trimmed);
            return `<tt>${escaped}</tt>\n`;
        } else {
            // Plain text with simple formatting
            return `┌─ Code ${index + 1} ─${'─'.repeat(Math.max(0, 20 - trimmed.length))}\n` +
                   `${trimmed}\n` +
                   `└${'─'.repeat(Math.min(50, trimmed.length + 15))}\n`;
        }
    }

    /**
     * Truncate text to max lines, adding "..." indicator
     */
    _truncateToLines(text, maxLines) {
        const lines = text.split('\n');

        if (lines.length <= maxLines) {
            return text;
        }

        // Count visual lines (accounting for wrapping)
        let visualLines = 0;
        let truncateIndex = -1;

        for (let i = 0; i < lines.length; i++) {
            // Approximate line wrapping (roughly 50 chars per line)
            const wrappedLines = Math.ceil(lines[i].length / 50);
            visualLines += wrappedLines;

            if (visualLines > maxLines) {
                truncateIndex = i;
                break;
            }
        }

        if (truncateIndex >= 0) {
            const truncated = lines.slice(0, truncateIndex).join('\n');
            return truncated + '\n\n… (click to see more)';
        }

        return text;
    }

    /**
     * Escape text for Pango markup
     */
    _escapeText(text) {
        if (!text) return '';

        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    /**
     * Create a code block from command output
     * @param {string} command - Command string
     * @param {string} output - Command output
     * @returns {string} Formatted code block
     */
    formatCommand(command, output) {
        const formatted = `$ ${command}\n${output}`;
        return this._formatCodeBlock(formatted);
    }

    /**
     * Format a diff/code snippet
     * @param {string} diff - Diff content
     * @param {string} language - Optional language hint
     * @returns {string} Formatted code block
     */
    formatDiff(diff, language = 'diff') {
        return this._formatCodeBlock(diff);
    }

    /**
     * Get formatted code blocks from a multi-line string
     * @param {string} text - Text that may contain code blocks
     * @param {string} delimiter - Code block delimiter (default: ```)
     * @returns {object} { body, codeBlocks }
     */
    parseCodeBlocks(text, delimiter = '```') {
        const lines = text.split('\n');
        const bodyLines = [];
        const codeBlocks = [];
        let inCodeBlock = false;
        let currentCode = [];

        for (const line of lines) {
            if (line.trim().startsWith(delimiter)) {
                inCodeBlock = !inCodeBlock;
                if (!inCodeBlock && currentCode.length > 0) {
                    codeBlocks.push(currentCode.join('\n'));
                    currentCode = [];
                }
            } else if (inCodeBlock) {
                currentCode.push(line);
            } else {
                bodyLines.push(line);
            }
        }

        // Handle unclosed code block
        if (currentCode.length > 0) {
            codeBlocks.push(currentCode.join('\n'));
        }

        return {
            body: bodyLines.join('\n'),
            codeBlocks,
        };
    }
}
```

### 2. Update `lib/notificationManager.js`

```javascript
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';
import { CustomNotification } from '../widgets/customNotification.js';
import { TextFormatter } from './textFormatter.js';

export class NotificationManager {
    constructor(dbusService) {
        this._dbusService = dbusService;
        this._notifications = new Map();
        this._nextId = 0;
        this._textFormatter = new TextFormatter();
    }

    showNotification(options) {
        const id = `notif-${Date.now()}-${this._nextId++}`;
        log(`[AI Notification] Creating notification ${id}: ${options.title}`);

        // Determine button layout
        const actionLayout = options.actionLayout || 'horizontal';
        const actions = options.actions || [];
        const useVertical = actionLayout === 'vertical' || actions.length > 3;

        // Create or get notification source
        const source = this._getOrCreateSource(options.title);

        // Format body with code blocks and truncation
        const formattedBody = this._textFormatter.formatBody(
            options.body || '',
            options.codeBlocks || [],
            {
                maxLines: options.maxLines || 0,
                truncate: true,
            }
        );

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

        // Create notification
        let notification;
        if (useVertical && actions.length > 0) {
            notification = new CustomNotification({
                source: source,
                title: options.title,
                body: formattedBody,
                urgency: this._mapUrgency(options.urgency || 'normal'),
                actionLayout: 'vertical',
                expireTimeoutMs: options.expireTimeoutMs || 0,
            });

            for (const callback of actionCallbacks) {
                notification.addAction(callback.label, callback.callback);
            }
        } else {
            notification = new MessageTray.Notification({
                source: source,
                title: options.title,
                body: formattedBody,
                urgency: this._mapUrgency(options.urgency || 'normal'),
            });

            for (const callback of actionCallbacks.slice(0, 3)) {
                notification.addAction(callback.label, callback.callback);
            }
        }

        // Handle destruction and activation
        notification.connect('destroy', (notif, reason) => {
            this._onNotificationDestroyed(id, reason);
        });

        notification.connect('activated', (notif) => {
            this.setResult(id, { actionId: 'activated' });
        });

        // Store and show
        this._notifications.set(id, {
            notification,
            source,
            result: null,
            options,
        });

        source.addNotification(notification);

        log(`[AI Notification] Notification ${id} displayed (${actionLayout} layout)`);
        return id;
    }

    // ... rest of methods remain the same ...
}
```

### 3. Update Python Client with Code Block Parsing

```python
#!/usr/bin/env python3
import argparse
import json
import sys
import time
import re
from gi.repository import GLib, Gio

BUS_NAME = "org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH = "/org/gnome/Shell/Extensions/AINotifications"
INTERFACE = "org.gnome.Shell.Extensions.AINotifications"

class NotificationClient:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.results = {}

    def show_notification(self, title, body, options=None):
        """Show a notification"""
        if options is None:
            options = {}

        options_builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))

        for key, value in options.items():
            if key == "actions":
                actions = []
                for action in value:
                    action_dict = GLib.Variant("a{ss}", action)
                    actions.append(action_dict)
                options_builder.add("{sv}", key, GLib.Variant("aa{ss}", actions))
            elif key == "code_blocks":
                options_builder.add("{sv}", key, GLib.Variant("as", value))
            else:
                variant_map = {
                    "urgency": "s",
                    "expire_timeout_ms": "i",
                    "action_layout": "s",
                    "max_lines": "i",
                }
                if key in variant_map:
                    options_builder.add("{sv}", key, GLib.Variant(variant_map[key], value))

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

        return result.unpack()[0]

    def parse_code_blocks(self, text):
        """Parse markdown-style code blocks from text"""
        pattern = r'```(\w*)\n(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL)

        code_blocks = []
        body = text

        for lang, code in matches:
            code_blocks.append(code.strip())
            body = body.replace(f'```{lang}\n{code}```', '', 1)

        return body.strip(), code_blocks

    def get_result(self, notification_id, timeout=300):
        """Wait for result with timeout"""
        start_time = time.time()

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
        if notification_id in self.results:
            return self.results[notification_id]

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
    parser = argparse.ArgumentParser(
        description="Send interactive notifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple notification
  %(prog)s "Hello" "World"

  # With code blocks
  %(prog)s "Deploy" "Confirm deployment:" --code "git push origin main"

  # Parse markdown code blocks
  %(prog)s "Review" "Check this:" --markdown <<'EOF'
  Please review:
  ```python
  def hello():
      print("world")
  ```
  EOF

  # Multiple choice
  %(prog)s "Background" "Choose style:" \\
      --layout vertical \\
      --action white "White" \\
      --action dark "Dark" \\
      --wait
        """
    )

    parser.add_argument("title", help="Notification title")
    parser.add_argument("body", help="Notification body")
    parser.add_argument("--urgency", choices=["low", "normal", "high", "critical"], default="normal")
    parser.add_argument("--expire", type=int, default=0, help="Expire timeout in milliseconds")
    parser.add_argument("--action", action="append", nargs=2, metavar=("ID", "LABEL"),
                        help="Add action button")
    parser.add_argument("--layout", choices=["horizontal", "vertical"], default="horizontal")
    parser.add_argument("--code", action="append", help="Add code block")
    parser.add_argument("--markdown", action="store_true", help="Parse ```code blocks from body")
    parser.add_argument("--max-lines", type=int, default=0, help="Max lines before truncation")
    parser.add_argument("--wait", action="store_true", help="Wait for user interaction")
    parser.add_argument("--timeout", type=int, default=300, help="Max time to wait (seconds)")

    args = parser.parse_args()

    client = NotificationClient()

    # Parse code blocks from markdown if requested
    body = args.body
    code_blocks = args.code or []

    if args.markdown:
        body, parsed_blocks = client.parse_code_blocks(args.body)
        code_blocks.extend(parsed_blocks)

    options = {
        "urgency": args.urgency,
        "action_layout": args.layout,
    }

    if args.expire > 0:
        options["expire_timeout_ms"] = args.expire
    if args.action:
        options["actions"] = [{"id": id, "label": label} for id, label in args.action]
    if code_blocks:
        options["code_blocks"] = code_blocks
    if args.max_lines > 0:
        options["max_lines"] = args.max_lines

    notification_id = client.show_notification(args.title, body, options)
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

### Test 1: Simple Code Block

```bash
./client/notify-interactive \
    "Code Review" \
    "Please review this code:" \
    --code "const x = 42;" \
    --code "return x * 2;" \
    --action lgtm "LGTM" \
    --action changes "Changes" \
    --wait
```

### Test 2: Markdown Code Blocks

```bash
./client/notify-interactive \
    "Deployment" \
    "Deploy this change?
    \`\`\`bash
    git push origin main
    \`\`\`" \
    --markdown \
    --action deploy "Deploy" \
    --action cancel "Cancel" \
    --wait
```

### Test 3: Long Content Truncation

```bash
# Generate a long body
LONG_TEXT=$(printf "Line %s\n" {1..50})

./client/notify-interactive \
    "Long Content" \
    "$LONG_TEXT" \
    --max-lines 10 \
    --action ok "OK" \
    --wait
```

### Test 4: Multi-line Code with Indentation

```bash
./client/notify-interactive \
    "Function Review" \
    "Review this function:" \
    --code "
    def process(data):
        result = []
        for item in data:
            result.append(item * 2)
        return result
    " \
    --action approve "Approve" \
    --action reject "Reject" \
    --wait
```

### Test 5: Multiple Code Blocks

```bash
./client/notify-interactive \
    "Config Change" \
    "Proposed configuration changes:" \
    --code "[database]
host = localhost
port = 5432" \
    --code "[cache]
backend = redis
ttl = 3600" \
    --action apply "Apply" \
    --action discard "Discard" \
    --wait
```

## Acceptance Criteria

- [ ] Code blocks display in monospace font
- [ ] Indentation is preserved in code blocks
- [ ] Long content is truncated with "..." indicator
- [ ] Clicking truncated notification shows full content in drawer
- [ ] Markdown-style code blocks are parsed correctly
- [ ] Special characters in code are properly escaped
- [ ] Multiple code blocks are supported

## Next Task

After completing this task, move to Phase 3:
- [`03-01-client-cli.md`](./03-01-client-cli.md) - Finalize CLI tool
- [`03-02-client-library.md`](./03-02-client-library.md) - Python library
