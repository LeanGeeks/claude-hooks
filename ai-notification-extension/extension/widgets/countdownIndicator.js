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
        this._durationMs = 0;

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
        this._durationMs = durationMs;
        const startTime = Date.now();
        const endTime = startTime + durationMs;

        // Update every 50ms for smooth animation
        const interval = 50;

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

        try {
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
                // End angle (positive direction for counter-clockwise visual effect)
                const endAngle = startAngle + (2 * Math.PI * this._progress);

                cr.setSourceRGBA(0.21, 0.48, 0.94, 0.9); // Blue color
                cr.setLineWidth(this._lineWidth);
                cr.setLineCap(Cairo.LineCap.ROUND);

                cr.arc(cx, cy, this._radius, startAngle, endAngle);
                cr.stroke();
            }

            // Optionally show progress text in center
            if (this._radius > 8) {
                const seconds = Math.ceil(this._progress * (this._durationMs / 1000));
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
        } finally {
            cr.$dispose();
        }
    }

    /**
     * Destroy the indicator
     */
    vfunc_destroy() {
        // Cleanup if needed
        super.vfunc_destroy();
    }
});
