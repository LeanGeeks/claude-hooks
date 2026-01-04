import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

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
