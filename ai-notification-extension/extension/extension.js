import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';

// Import our modules
import { DBusService } from './ipc/dbusService.js';
import { NotificationManager } from './lib/notificationManager.js';

export default class AiNotificationExtension {
    constructor() {
        this._source = null;
        this._dbusService = null;
        this._notificationManager = null;
    }

    enable() {
        log('[AI Notification Extension] Enabling...');

        // Create notification source
        this._source = new MessageTray.Source({
            title: 'AI Notifications',
            iconName: 'dialog-information',
        });
        Main.messageTray.add(this._source);

        // Initialize D-Bus service
        this._notificationManager = new NotificationManager(null); // Will set dbusService after
        this._dbusService = new DBusService(this._notificationManager);
        this._notificationManager._dbusService = this._dbusService;
        this._dbusService.enable();

        // Show a test notification
        const notification = new MessageTray.Notification({
            source: this._source,
            title: 'Extension Enabled',
            body: 'AI Notification Extension is now active with D-Bus support',
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

        if (this._dbusService) {
            this._dbusService.disable();
            this._dbusService = null;
        }

        if (this._notificationManager) {
            this._notificationManager.destroy();
            this._notificationManager = null;
        }

        if (this._source) {
            this._source.destroy();
            this._source = null;
        }

        log('[AI Notification Extension] Disabled');
    }
}
