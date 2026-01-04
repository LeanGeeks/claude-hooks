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

        // Store notification data
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
