import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const AI_NOTIFICATIONS_BUS_NAME = 'org.gnome.Shell.Extensions.AINotifications';
const AI_NOTIFICATIONS_OBJECT_PATH = '/org/gnome/Shell/Extensions/AINotifications';
const AI_NOTIFICATIONS_INTERFACE = 'org.gnome.Shell.Extensions.AINotifications';

const DBusIface = `
<node>
  <interface name="${AI_NOTIFICATIONS_INTERFACE}">
    <method name="ShowNotification">
      <arg name="title" type="s" direction="in"/>
      <arg name="body" type="s" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="notification_id" type="s" direction="out"/>
    </method>
    <method name="GetResult">
      <arg name="notification_id" type="s" direction="in"/>
      <arg name="result" type="a{sv}" direction="out"/>
    </method>
    <signal name="NotificationResult">
      <arg name="notification_id" type="s"/>
      <arg name="result" type="a{sv}"/>
    </signal>
  </interface>
</node>
`;

export class DBusService {
    constructor(notificationManager) {
        this._notificationManager = notificationManager;
        this._dbusImpl = null;
        this._busId = 0;
    }

    enable() {
        const dbusProxy = Gio.DBusExportedObject.wrapJSObject(
            DBusIface,
            this
        );

        this._dbusImpl = dbusProxy;
        this._busId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            AI_NOTIFICATIONS_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            this._onBusAcquired.bind(this),
            this._onNameAcquired.bind(this),
            this._onNameLost.bind(this)
        );

        log('[AI Notification] D-Bus service enabled');
    }

    disable() {
        if (this._busId) {
            Gio.bus_unown_name(this._busId);
            this._busId = 0;
        }
        if (this._dbusImpl) {
            this._dbusImpl.unexport();
            this._dbusImpl = null;
        }
        log('[AI Notification] D-Bus service disabled');
    }

    _onBusAcquired(connection, name) {
        this._dbusImpl.export(connection, AI_NOTIFICATIONS_OBJECT_PATH);
        log(`[AI Notification] D-Bus bus acquired: ${name}`);
    }

    _onNameAcquired(connection, name) {
        log(`[AI Notification] D-Bus name acquired: ${name}`);
    }

    _onNameLost(connection, name) {
        log(`[AI Notification] D-Bus name lost: ${name}`);
    }

    /**
     * D-Bus Method: ShowNotification
     * @param {string} title - Notification title
     * @param {string} body - Notification body
     * @param {object} options - Options dictionary
     * @returns {string} notification_id
     */
    ShowNotification(title, body, options) {
        log(`[AI Notification] ShowNotification: ${title}`);

        const notificationId = this._notificationManager.showNotification({
            title,
            body,
            urgency: options['urgency']?.unpack() || 'normal',
            expireTimeoutMs: options['expire_timeout_ms']?.unpack() || 0,
            actions: this._unpackActions(options['actions']?.unpack()),
            actionLayout: options['action_layout']?.unpack() || 'horizontal',
            codeBlocks: options['code_blocks']?.unpack() || [],
            maxLines: options['max_lines']?.unpack() || 0,
        });

        return notificationId;
    }

    /**
     * D-Bus Method: GetResult
     * @param {string} notificationId - ID of notification to query
     * @returns {object} Result dictionary (empty if no result yet)
     */
    GetResult(notificationId) {
        log(`[AI Notification] GetResult: ${notificationId}`);
        const result = this._notificationManager.getResult(notificationId);
        if (result) {
            return this._packResult(result);
        }
        // Return empty dict instead of null to match D-Bus interface
        return new GLib.Variant('a{sv}', {});
    }

    /**
     * Emit a result signal
     * @param {string} notificationId - ID of notification
     * @param {object} result - Result to emit
     */
    emitResult(notificationId, result) {
        this._dbusImpl.emit_signal(
            'NotificationResult',
            new GLib.Variant('(sa{sv})', [notificationId, this._packResult(result)])
        );
    }

    _unpackActions(actionsVariant) {
        if (!actionsVariant) return [];
        // Unpack GVariant array of {id, label} dictionaries
        return actionsVariant.recursiveUnpack();
    }

    _packResult(result) {
        return new GLib.Variant('a{sv}', {
            action_id: new GLib.Variant('s', result.actionId),
            timestamp: new GLib.Variant('t', result.timestamp),
        });
    }
}
