# Task 2.2: Countdown Indicator for Expiring Notifications

## Objective

Create a circular countdown indicator that visually shows remaining time for expiring notifications. The circle starts full and is erased counter-clockwise as time progresses.

## Visual Design

```
    Full Circle (t=0)      Halfway (t=50%)      Empty (t=100%)
          ●                     ◐                ○
     (30 seconds)          (15 seconds)      (0 seconds)

The indicator appears next to the notification or as an overlay.
```

## Implementation Strategy

Using `Clutter.Canvas` for drawing the circular progress indicator:

1. Create a custom actor that draws a circle
2. Update the arc based on remaining time
3. Use Cairo drawing for smooth rendering
4. Attach to notifications with `expireTimeoutMs > 0`

---

## Code

### 1. Create `widgets/countdownIndicator.js`

```javascript
import GObject from 'gi://GObject';
import Clutter from 'gi://Clutter';
import Cairo from 'gi://Cairo';
import St from 'gi://St';
import GLib from 'gi://GLib';

/**
 * Circular countdown indicator
 * Shows remaining time as a circle that gets erased counter-clockwise
 */
export const CountdownIndicator = GObject.registerClass({
    GTypeName: 'AINotificationCountdownIndicator',
    Properties: {
        'progress': GObject.ParamSpec.double(
            'progress',
            'Progress',
            'Progress from 1.0 (full) to 0.0 (empty)',
            GObject.ParamFlags.READWRITE,
            0.0,
            1.0,
            1.0
        ),
        'radius': GObject.ParamSpec.double(
            'radius',
            'Radius',
            'Circle radius in pixels',
            GObject.ParamFlags.READWRITE,
            4.0,
            32.0,
            12.0
        ),
    },
}, class CountdownIndicator extends St.DrawingArea {
    constructor(params = {}) {
        super({
            style_class: 'ai-countdown-indicator',
            width: 32,
            height: 32,
            ...params,
        });

        this._progress = 1.0;
        this._radius = 12.0;
        this._lineWidth = 2.5;

        // Connect to redraw signal
        this.connect('repaint', this._repaint.bind(this));
    }

    get progress() {
        return this._progress;
    }

    set progress(value) {
        if (this._progress !== value) {
            this._progress = Math.max(0.0, Math.min(1.0, value));
            this.queue_repaint();
            this.notify('progress');
        }
    }

    get radius() {
        return this._radius;
    }

    set radius(value) {
        if (this._radius !== value) {
            this._radius = value;
            this.queue_repaint();
            this.notify('radius');
        }
    }

    /**
     * Start countdown with duration
     * @param {number} durationMs - Duration in milliseconds
     * @returns {number} Timeout ID
     */
    startCountdown(durationMs) {
        this.progress = 1.0;
        const startTime = Date.now();
        const endTime = startTime + durationMs;

        // Update every 50ms for smooth animation
        const interval = 50;
        let lastUpdate = startTime;

        const timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, interval, () => {
            const now = Date.now();
            const elapsed = now - startTime;
            const remaining = Math.max(0, endTime - now);
            const newProgress = remaining / durationMs;

            this.progress = newProgress;

            if (remaining <= 0) {
                return GLib.SOURCE_REMOVE; // Stop timer
            }

            return GLib.SOURCE_CONTINUE; // Continue timer
        });

        return timeoutId;
    }

    /**
     * Stop countdown
     * @param {number} timeoutId - Timeout ID to remove
     */
    stopCountdown(timeoutId) {
        if (timeoutId) {
            GLib.source_remove(timeoutId);
        }
    }

    /**
     * Repaint handler - draws the circle
     */
    _repaint() {
        const width = this.width;
        const height = this.height;
        const cx = width / 2;
        const cy = height / 2;

        const cr = this.get_context();

        if (!cr) return;

        // Clear background
        cr.setSourceRGBA(0, 0, 0, 0);
        cr.paint();

        // Draw background circle (empty state - subtle)
        cr.setSourceRGBA(0.4, 0.4, 0.4, 0.3);
        cr.setLineWidth(1.5);
        cr.arc(cx, cy, this._radius, 0, 2 * Math.PI);
        cr.stroke();

        // Draw progress arc (counter-clockwise from top)
        if (this._progress > 0) {
            // Start from top (-90 degrees = -π/2)
            const startAngle = -Math.PI / 2;
            // End angle (counter-clockwise = negative direction)
            const endAngle = startAngle - (2 * Math.PI * this._progress);

            cr.setSourceRGBA(0.21, 0.48, 0.94, 0.9); // Blue color
            cr.setLineWidth(this._lineWidth);
            cr.setLineCap(Cairo.LineCap.ROUND);

            cr.arc(cx, cy, this._radius, startAngle, endAngle);
            cr.stroke();
        }

        // Optionally show progress text in center
        if (this._radius > 8) {
            const seconds = Math.ceil(this._progress * 30); // Assuming 30s max for text
            const text = seconds.toString();

            cr.setSourceRGBA(1, 1, 1, 0.8);
            cr.setFontSize(8);
            cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);

            const extents = cr.textExtents(text);
            const textX = cx - (extents.width / 2) - extents.x_bearing;
            const textY = cy - (extents.height / 2) - extents.y_bearing;

            cr.moveTo(textX, textY);
            cr.showText(text);
        }

        cr.$dispose();
    }

    /**
     * Destroy the indicator
     */
    vfunc_destroy() {
        // Cleanup if needed
        super.vfunc_destroy();
    }
});
```

### 2. Update `widgets/customNotification.js` to Support Countdown

```javascript
import GObject from 'gi://GObject';
import Clutter from 'gi://Clutter';
import St from 'gi://St';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import { CountdownIndicator } from './countdownIndicator.js';

export const CustomNotification = GObject.registerClass({
    GTypeName: 'AINotificationCustomNotification',
}, class CustomNotification extends MessageTray.Notification {
    constructor(params) {
        super(params);

        this._actionLayout = params.actionLayout || 'horizontal';
        this._verticalButtonBox = null;
        this._countdownIndicator = null;
        this._countdownTimeoutId = null;
        this._expireTimeoutMs = params.expireTimeoutMs || 0;
    }

    createBanner() {
        const banner = super.createBanner();

        // Add countdown indicator if expiring
        if (this._expireTimeoutMs > 0) {
            this._setupCountdownIndicator(banner);
        }

        // Setup vertical buttons if needed
        if (this._actionLayout === 'vertical' && this._actions.length > 3) {
            this._setupVerticalButtons(banner);
        }

        return banner;
    }

    /**
     * Setup countdown indicator
     */
    _setupCountdownIndicator(banner) {
        const indicator = new CountdownIndicator({
            radius: 10,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
            margin_left: 8,
        });

        // Add to banner title area
        if (banner._titleBin) {
            banner._titleBin.add_child(indicator);
        } else {
            banner.actor.add_child(indicator);
        }

        this._countdownIndicator = indicator;

        // Start countdown
        this._countdownTimeoutId = indicator.startCountdown(this._expireTimeoutMs);
    }

    /**
     * Stop countdown (when notification is destroyed or action taken)
     */
    _stopCountdown() {
        if (this._countdownTimeoutId) {
            this._countdownIndicator.stopCountdown(this._countdownTimeoutId);
            this._countdownTimeoutId = null;
        }
    }

    /**
     * Emit action signal (override to stop countdown)
     */
    _emitAction(index) {
        this._stopCountdown();

        if (this._actions && this._actions[index]) {
            const action = this._actions[index];
            action.callback();
        }
    }

    // ... rest of the class ...
});
```

### 3. Update `stylesheet.css`

```css
/* Countdown Indicator */

.ai-countdown-indicator {
    opacity: 0.9;
}

.ai-countdown-indicator:hover {
    opacity: 1.0;
}
```

### 4. Update Python Client for Countdown Display

Add support for showing remaining time when waiting:

```python
import sys
import time
import threading
from gi.repository import GLib, Gio

# ... (previous code) ...

class NotificationClient:
    # ... (previous methods) ...

    def get_result_with_progress(self, notification_id, timeout=300, duration_ms=0):
        """Wait for result, showing progress countdown"""
        if duration_ms <= 0:
            return self.get_result(notification_id, timeout)

        start_time = time.time()
        expire_seconds = duration_ms / 1000.0

        def print_progress():
            while time.time() - start_time < expire_seconds:
                elapsed = time.time() - start_time
                remaining = max(0, expire_seconds - elapsed)
                progress = remaining / expire_seconds

                # Simple text progress bar
                bar_width = 20
                filled = int(bar_width * (1 - progress))
                bar = '█' * filled + '░' * (bar_width - filled)

                print(f'\r⏱ [{bar}] {int(remaining)}s remaining', end='', flush=True)
                time.sleep(0.1)

            print('\r⏱ [░░░░░░░░░░░░░░░░░░░░] Expired!    ', flush=True)

        # Start progress in thread
        progress_thread = threading.Thread(target=print_progress, daemon=True)
        progress_thread.start()

        try:
            # Wait for result or timeout
            result = self.get_result(notification_id, timeout)
            return result
        finally:
            progress_thread.join(timeout=0.5)
```

## Testing

### Test 1: 10 Second Countdown

```bash
./client/notify-interactive \
    "Timeout Test" \
    "This will expire in 10 seconds" \
    --expire 10000 \
    --action approve "Approve" \
    --action deny "Deny" \
    --wait
```

### Test 2: 30 Second Countdown

```bash
./client/notify-interactive \
    "Long Operation" \
    "Please confirm within 30 seconds" \
    --expire 30000 \
    --urgency high \
    --action confirm "Confirm" \
    --action cancel "Cancel" \
    --wait
```

### Test 3: Countdown with Vertical Buttons

```bash
./client/notify-interactive \
    "Quick Choice" \
    "Pick one before time runs out!" \
    --expire 15000 \
    --layout vertical \
    --action opt1 "Option 1" \
    --action opt2 "Option 2" \
    --action opt3 "Option 3" \
    --action opt4 "Option 4" \
    --wait
```

### Test 4: Critical Urgency with Countdown

```bash
./client/notify-interactive \
    "URGENT: Rollback" \
    "Database migration failed. Rollback changes?" \
    --expire 20000 \
    --urgency critical \
    --action rollback "Rollback Now" \
    --action investigate "Investigate First" \
    --wait
```

## Acceptance Criteria

- [ ] Countdown circle appears for notifications with `expireTimeoutMs > 0`
- [ ] Circle starts full and is erased counter-clockwise
- [ ] Animation is smooth (updates ~20fps)
- [ ] Countdown stops when notification is interacted with
- [ ] Countdown stops when notification is dismissed
- [ ] Visual style matches GNOME notification aesthetic
- [ ] No performance issues with multiple expiring notifications

## Design Decisions

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| **Drawing API** | Cairo via `Clutter.Canvas` | Native to GNOME Shell, smooth rendering |
| **Update frequency** | 50ms (20fps) | Balance smoothness vs CPU usage |
| **Color** | Blue (#3584e4) | Matches GNOME accent color |
| **Line width** | 2.5px | Visible but not overwhelming |
| **Radius** | 12px (default) | Subtle, doesn't dominate notification |

## Alternative Approaches Considered

1. **CSS animation** - Not feasible for dynamic arc drawing
2. **SVG image** - Complex to update dynamically
3. **Progress bar** - Less distinctive, takes more horizontal space

## Next Task

After completing this task, move to [`02-03-code-block-formatting.md`](./02-03-code-block-formatting.md) for better code block formatting.
