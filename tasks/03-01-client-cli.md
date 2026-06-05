# Task 3.1: Client CLI Tool

## Objective

Create a polished CLI tool for sending interactive notifications. This tool will be the primary way users and scripts interact with the extension.

## Features

- Simple command-line interface
- Support for all notification options
- Markdown-style code block parsing
- Wait for result with exit codes
- JSON output mode for scripting
- Shell completion

---

## Implementation

### 1. Final Python CLI Tool `client/notify-interactive`

```python
#!/usr/bin/env python3
"""
AI Notification Interactive CLI

Send interactive desktop notifications with action buttons.

Usage:
    notify-interactive "Title" "Body" --action approve "Approve" --wait
"""

import argparse
import json
import re
import sys
import time
from typing import Optional, Dict, List, Any
from gi.repository import GLib, Gio

# D-Bus constants
BUS_NAME = "org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH = "/org/gnome/Shell/Extensions/AINotifications"
INTERFACE = "org.gnome.Shell.Extensions.AINotifications"


class NotificationClient:
    """Client for AI Notification Extension D-Bus service"""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.results = {}
        self._signal_id = None

    def show_notification(
        self,
        title: str,
        body: str,
        urgency: str = "normal",
        expire_ms: int = 0,
        actions: Optional[List[Dict[str, str]]] = None,
        layout: str = "horizontal",
        code_blocks: Optional[List[str]] = None,
        max_lines: int = 0,
    ) -> str:
        """Show a notification and return its ID"""
        options = {
            "urgency": urgency,
            "action_layout": layout,
        }

        if expire_ms > 0:
            options["expire_timeout_ms"] = expire_ms
        if actions:
            options["actions"] = actions
        if code_blocks:
            options["code_blocks"] = code_blocks
        if max_lines > 0:
            options["max_lines"] = max_lines

        options_builder = self._build_options_variant(options)

        result = self.bus.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "ShowNotification",
            GLib.Variant("(ssa{sv})", (title, body, options_builder)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

        if result:
            return result.unpack()[0]
        return None

    def get_result(self, notification_id: str, timeout: int = 300) -> Optional[Dict[str, Any]]:
        """Wait for notification result with timeout"""
        start_time = time.time()

        # Subscribe to result signals
        self._signal_id = self.bus.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            "NotificationResult",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_result,
            None,
        )

        try:
            while time.time() - start_time < timeout:
                # Check local cache first
                if notification_id in self.results:
                    return self.results[notification_id]

                # Poll via D-Bus
                result = self._poll_result(notification_id)
                if result:
                    return result

                time.sleep(0.1)

            # Timeout
            return {"action_id": "expired", "timestamp": int(time.time() * 1000)}

        finally:
            if self._signal_id:
                self.bus.signal_unsubscribe(self._signal_id)
                self._signal_id = None

    def _on_result(self, connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        """Handle result signal"""
        notification_id, result = parameters.unpack()
        self.results[notification_id] = result

    def _poll_result(self, notification_id: str) -> Optional[Dict[str, Any]]:
        """Poll for result via D-Bus"""
        result = self.bus.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "GetResult",
            GLib.Variant("(s)", (notification_id,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )

        if result:
            return result.unpack()[0]
        return None

    def _build_options_variant(self, options: Dict[str, Any]) -> GLib.Variant:
        """Build D-Bus variant for options dictionary"""
        builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))

        for key, value in options.items():
            if key == "actions":
                # Array of {id, label} dictionaries
                action_variants = []
                for action in value:
                    action_dict = GLib.Variant("a{ss}", action)
                    action_variants.append(action_dict)
                builder.add("{sv}", key, GLib.Variant("aa{ss}", action_variants))

            elif key == "code_blocks":
                builder.add("{sv}", key, GLib.Variant("as", value))

            else:
                # Map types to GVariant types
                type_map = {
                    "urgency": "s",
                    "expire_timeout_ms": "i",
                    "action_layout": "s",
                    "max_lines": "i",
                }
                if key in type_map:
                    builder.add("{sv}", key, GLib.Variant(type_map[key], value))

        return builder.end()

    def parse_code_blocks(self, text: str) -> tuple[str, List[str]]:
        """Parse markdown-style code blocks from text"""
        pattern = r'```(\w*)\n(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL)

        code_blocks = []
        body = text

        for lang, code in matches:
            code_blocks.append(code.strip())
            body = body.replace(f'```{lang}\n{code}```', '', 1)

        return body.strip(), code_blocks


def parse_actions(action_strings: List[str]) -> List[Dict[str, str]]:
    """Parse --action arguments into list of {id, label} dicts"""
    actions = []
    for action_str in action_strings:
        # Split on first ':' or space
        if ':' in action_str:
            id, label = action_str.split(':', 1)
        elif ' ' in action_str:
            parts = action_str.split(' ', 1)
            id = parts[0]
            label = parts[1] if len(parts) > 1 else id
        else:
            id = label = action_str

        actions.append({"id": id.strip(), "label": label.strip()})
    return actions


def print_progress_bar(remaining: float, total: float, width: int = 20):
    """Print a progress bar for countdown"""
    progress = remaining / total
    filled = int(width * (1 - progress))
    bar = '█' * filled + '░' * (width - filled)
    secs = int(remaining)
    print(f'\r⏱ [{bar}] {secs:3d}s remaining', end='', flush=True)


def main():
    parser = argparse.ArgumentParser(
        prog='notify-interactive',
        description='Send interactive desktop notifications with action buttons',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple notification
  %(prog)s "Hello" "World"

  # Yes/No choice
  %(prog)s "Confirm" "Delete this file?" \\
      --action approve:Yes --action deny:No --wait

  # Multiple choice (vertical)
  %(prog)s "Background" "Choose style:" \\
      --layout vertical \\
      --action white "White background" \\
      --action transparent "Transparent" \\
      --action inherit "Inherit from parent" \\
      --wait

  # With code block
  %(prog)s "Deploy" "Confirm deployment:" \\
      --code "git push origin main" \\
      --action deploy "Deploy" \\
      --action cancel "Cancel" --wait

  # Markdown code blocks
  %(prog)s "Review" "Check this:" --markdown <<'EOF'
  Please review:
  ```python
  def hello():
      print("world")
  ```
  EOF

  # With expiry countdown
  %(prog)s "Timeout" "Choose in 15 seconds" \\
      --expire 15000 \\
      --action yes "Yes" \\
      --action no "No" \\
      --wait

Exit codes:
  0 - First/default action (approve, yes, confirm, etc.)
  1 - Second action (deny, no, cancel, etc.)
  2 - Closed/expired/other
        """
    )

    parser.add_argument("title", help="Notification title")
    parser.add_argument("body", nargs='?', default="", help="Notification body")
    parser.add_argument("-u", "--urgency", choices=["low", "normal", "high", "critical"],
                        default="normal", help="Notification urgency")
    parser.add_argument("-e", "--expire", type=int, default=0,
                        help="Expire timeout in milliseconds (0 = no expiry)")
    parser.add_argument("-a", "--action", dest="actions", action="append",
                        metavar="ID:LABEL", help="Add action button (can be used multiple times)")
    parser.add_argument("-l", "--layout", choices=["horizontal", "vertical"],
                        default="horizontal", help="Button layout")
    parser.add_argument("-c", "--code", dest="code_blocks", action="append",
                        help="Add code block (can be used multiple times)")
    parser.add_argument("-m", "--markdown", action="store_true",
                        help="Parse ```code blocks from body")
    parser.add_argument("--max-lines", type=int, default=0,
                        help="Max lines before truncation (0 = no limit)")
    parser.add_argument("-w", "--wait", action="store_true",
                        help="Wait for user interaction and exit with result code")
    parser.add_argument("-t", "--timeout", type=int, default=300,
                        help="Max time to wait for result (seconds, default: 300)")
    parser.add_argument("-j", "--json", action="store_true",
                        help="Output notification info as JSON")
    parser.add_argument("--show-progress", action="store_true",
                        help="Show countdown progress bar when waiting")

    args = parser.parse_args()

    client = NotificationClient()

    # Parse body and code blocks
    body = args.body
    code_blocks = list(args.code_blocks) if args.code_blocks else []

    if args.markdown:
        body, parsed_blocks = client.parse_code_blocks(args.body)
        code_blocks.extend(parsed_blocks)

    # Parse actions
    actions = parse_actions(args.actions) if args.actions else []

    # Show notification
    try:
        notification_id = client.show_notification(
            title=args.title,
            body=body,
            urgency=args.urgency,
            expire_ms=args.expire,
            actions=actions,
            layout=args.layout,
            code_blocks=code_blocks if code_blocks else None,
            max_lines=args.max_lines,
        )
    except Exception as e:
        print(f"Error: Failed to send notification: {e}", file=sys.stderr)
        print(f"\nMake sure the AI Notification Extension is installed and enabled.", file=sys.stderr)
        sys.exit(2)

    if args.json:
        output = {
            "notification_id": notification_id,
            "title": args.title,
            "body": body,
            "actions": actions,
        }
        print(json.dumps(output, indent=2))

    elif not args.wait:
        print(f"Notification ID: {notification_id}")

    else:
        # Wait for result
        print(f"Waiting for response (ID: {notification_id})...", file=sys.stderr)

        if args.show_progress and args.expire > 0:
            # Show progress bar for countdown
            import threading
            stop_progress = threading.Event()

            def show_progress():
                start = time.time()
                while not stop_progress.is_set():
                    elapsed = time.time() - start
                    remaining = max(0, (args.expire / 1000) - elapsed)
                    if remaining <= 0:
                        break
                    print_progress_bar(remaining, args.expire / 1000)
                    time.sleep(0.1)
                    # Clear line on exit
                    if stop_progress.is_set():
                        print('\r' + ' ' * 50 + '\r', end='', flush=True)

            progress_thread = threading.Thread(target=show_progress, daemon=True)
            progress_thread.start()

            result = client.get_result(notification_id, args.timeout)

            stop_progress.set()
            progress_thread.join(timeout=0.5)
        else:
            result = client.get_result(notification_id, args.timeout)

        action_id = result.get("action_id", "unknown")

        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Result: {action_id}")

        # Determine exit code
        if action_id in ("approve", "yes", "confirm", "ok", "accept", "allow"):
            sys.exit(0)
        elif action_id in ("deny", "no", "cancel", "reject", "decline", "disallow"):
            sys.exit(1)
        else:
            sys.exit(2)


if __name__ == "__main__":
    main()
```

### 2. Shell Completion `client/notify-interactive-completion.bash`

```bash
# Bash completion for notify-interactive

_notify_interactive_completion() {
    local cur prev words cword
    _init_completion || return

    case "$prev" in
        -u|--urgency)
            COMPREPLY=($(compgen -W "low normal high critical" -- "$cur"))
            return
            ;;
        -l|--layout)
            COMPREPLY=($(compgen -W "horizontal vertical" -- "$cur"))
            return
            ;;
        -t|--timeout|-e|--expire|--max-lines)
            # Numeric values
            return
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "
            -h --help
            -u --urgency
            -e --expire
            -a --action
            -l --layout
            -c --code
            -m --markdown
            --max-lines
            -w --wait
            -t --timeout
            -j --json
            --show-progress
        " -- "$cur"))
    fi
}

complete -F _notify_interactive_completion notify-interactive
```

### 3. Installation Script `client/install.sh`

```bash
#!/bin/bash
set -e

INSTALL_DIR="$HOME/.local/bin"
COMPLETION_DIR="$HOME/.local/share/bash-completion/completions"

# Create directories
mkdir -p "$INSTALL_DIR"
mkdir -p "$COMPLETION_DIR"

# Install CLI
cp notify-interactive "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/notify-interactive"

# Install completion
cp notify-interactive-completion.bash "$COMPLETION_DIR/notify-interactive"

# Add to PATH if needed
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo ""
    echo "⚠️  $HOME/.local/bin is not in your PATH"
    echo "   Add this to your ~/.bashrc or ~/.zshrc:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "✅ Installed notify-interactive to $INSTALL_DIR"
echo "✅ Installed bash completion"
echo ""
echo "Run 'notify-interactive --help' to get started"
```

## Testing

```bash
# Install
cd client && chmod +x install.sh && ./install.sh

# Test basic notification
notify-interactive "Test" "Hello World"

# Test with actions
notify-interactive "Deploy" "Push to production?" \
    --action approve:Approve \
    --action deny:Cancel \
    --wait

# Test vertical layout
notify-interactive "Choice" "Pick one:" \
    --action opt1:Option1 \
    --action opt2:Option2 \
    --action opt3:Option3 \
    --action opt4:Option4 \
    --layout vertical \
    --wait

# Test with code
notify-interactive "Code" "Review this:" \
    --code "const x = 42;" \
    --action lgtm:LGTM \
    --wait

# Test JSON output
notify-interactive "JSON Test" "Body" \
    --action ok:OK \
    --json
```

## Acceptance Criteria

- [ ] CLI tool installs correctly
- [ ] All options work as documented
- [ ] Exit codes reflect user choice
- [ ] JSON output is valid
- [ ] Bash completion works
- [ ] Help text is comprehensive
- [ ] Error messages are helpful

## Next Task

[`03-02-client-library.md`](./03-02-client-library.md) - Python library wrapper.
