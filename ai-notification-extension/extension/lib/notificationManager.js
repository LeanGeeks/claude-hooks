import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';

export class NotificationManager {
    constructor(dbusService) {
        this._dbusService = dbusService;
        this._notifications = new Map(); // id -> { notification, source, result, options, expireTimeoutId, _deletionTimeoutId }
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
        let expireTimeoutId = null;
        if (options.expireTimeoutMs > 0) {
            // Make notification resident (doesn't auto-dismiss)
            notification.setResident(true);
        }

        // Store notification
        this._notifications.set(id, {
            notification,
            source,
            result: null,
            options,
            expireTimeoutId, // Store for cleanup
        });

        // Set up expire timeout AFTER storing
        if (options.expireTimeoutMs > 0) {
            expireTimeoutId = GLib.timeout_add(
                GLib.PRIORITY_DEFAULT,
                options.expireTimeoutMs,
                () => {
                    const currentData = this._notifications.get(id);
                    if (currentData && !currentData.result) {
                        notification.destroy(MessageTray.NotificationDestroyedReason.EXPIRED);
                    }
                    return GLib.SOURCE_REMOVE;
                }
            );
            // Update stored data with timeout ID
            this._notifications.get(id).expireTimeoutId = expireTimeoutId;
        }

        // Show the notification AFTER storing
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
            // Prevent destroy signal from emitting another result
            data.result = data.result || { actionId: 'removed', timestamp: Date.now() };

            // Clean up expire timeout if exists
            if (data.expireTimeoutId) {
                GLib.source_remove(data.expireTimeoutId);
            }

            // Clean up deletion timeout if exists
            if (data._deletionTimeoutId) {
                GLib.source_remove(data._deletionTimeoutId);
            }

            data.notification.destroy(MessageTray.NotificationDestroyedReason.DISMISSED);
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
        let formatted = GLib.markup_escape_text(body, -1);

        if (codeBlocks.length > 0) {
            formatted += '\n\n';
            for (const code of codeBlocks) {
                const escapedCode = GLib.markup_escape_text(code, -1);
                formatted += `<tt>${escapedCode}</tt>\n`;
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

        // Remove from map after a delay (prevent multiple timeouts)
        if (data && !data._deletionTimeoutId) {
            data._deletionTimeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 5000, () => {
                if (this._notifications.has(id)) {
                    this._notifications.delete(id);
                }
                return GLib.SOURCE_REMOVE;
            });
        }
    }
}
