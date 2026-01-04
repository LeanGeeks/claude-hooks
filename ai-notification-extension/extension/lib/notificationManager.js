import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import GLib from 'gi://GLib';
import { CustomNotification } from '../widgets/customNotification.js';
import { TextFormatter } from './textFormatter.js';

export class NotificationManager {
    constructor(dbusService) {
        this._dbusService = dbusService;
        this._notifications = new Map(); // id -> { notification, source, result, options, expireTimeoutId, _deletionTimeoutId }
        this._nextId = 0;
        this._textFormatter = new TextFormatter();
    }

    /**
     * Show a notification
     * @param {object} options - Notification options
     * @returns {string} notification ID
     */
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

        // Create notification (custom or standard)
        let notification;
        const expireTimeoutMs = options.expireTimeoutMs || 0;

        if (useVertical && actions.length > 0) {
            // Use custom notification for vertical buttons
            notification = new CustomNotification({
                source: source,
                title: options.title,
                body: formattedBody,
                urgency: this._mapUrgency(options.urgency || 'normal'),
                actionLayout: 'vertical',
                expireTimeoutMs: expireTimeoutMs,
            });

            // Add actions to the custom notification
            for (const callback of actionCallbacks) {
                notification.addAction(callback.label, callback.callback);
            }
        } else if (expireTimeoutMs > 0) {
            // Use custom notification for countdown indicator even with horizontal buttons
            notification = new CustomNotification({
                source: source,
                title: options.title,
                body: formattedBody,
                urgency: this._mapUrgency(options.urgency || 'normal'),
                actionLayout: 'horizontal',
                expireTimeoutMs: expireTimeoutMs,
            });

            // Add actions to the custom notification
            for (const callback of actionCallbacks) {
                notification.addAction(callback.label, callback.callback);
            }
        } else {
            // Use standard notification for horizontal buttons without countdown
            notification = new MessageTray.Notification({
                source: source,
                title: options.title,
                body: formattedBody,
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

        // Handle notification activation (clicked)
        notification.connect('activated', (notif) => {
            this.setResult(id, { actionId: 'activated' });
        });

        // Set expire timeout
        let expireTimeoutId = null;
        if (expireTimeoutMs > 0) {
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
        if (expireTimeoutMs > 0) {
            expireTimeoutId = GLib.timeout_add(
                GLib.PRIORITY_DEFAULT,
                expireTimeoutMs,
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

        log(`[AI Notification] Notification ${id} displayed (${actionLayout} layout)`);
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
