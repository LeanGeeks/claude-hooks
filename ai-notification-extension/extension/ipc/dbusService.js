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
        this._registrationId = 0;
    }

    enable() {
        log('[AI Notification] Starting D-Bus service...');

        try {
            // Get the session bus
            this._connection = Gio.bus_get_sync(Gio.BusType.SESSION, null);
            log('[AI Notification] Got session bus');

            // Parse the D-Bus interface
            const dbusInfo = Gio.DBusNodeInfo.new_for_xml(DBusIface);
            log('[AI Notification] Parsed D-Bus interface XML');

            // Register the object on the bus
            this._registrationId = this._connection.register_object(
                AI_NOTIFICATIONS_OBJECT_PATH,
                dbusInfo.interfaces[0],
                this._handleDBusCall.bind(this),
                null,
                null
            );
            log(`[AI Notification] Registered object with ID: ${this._registrationId}`);

            // Request the bus name
            this._busId = this._connection.call_sync(
                'org.freedesktop.DBus',
                '/org/freedesktop/DBus',
                'org.freedesktop.DBus',
                'RequestName',
                GLib.Variant.new('(su)', [AI_NOTIFICATIONS_BUS_NAME, 0]),
                GLib.VariantType.new('(u)'),
                Gio.DBusCallFlags.NONE,
                -1,
                null
            );
            log(`[AI Notification] Requested bus name, got result: ${this._busId}`);

            log('[AI Notification] D-Bus service enabled successfully');
        } catch (e) {
            log(`[AI Notification] Error enabling D-Bus: ${e.message}`);
            log(`[AI Notification] Error stack: ${e.stack}`);
        }
    }

    disable() {
        if (this._connection) {
            // Unregister the object path first
            if (this._registrationId) {
                this._connection.unregister_object(this._registrationId);
                this._registrationId = 0;
            }

            // Release the bus name
            try {
                this._connection.call_sync(
                    'org.freedesktop.DBus',
                    '/org/freedesktop/DBus',
                    'org.freedesktop.DBus',
                    'ReleaseName',
                    GLib.Variant.new('(s)', [AI_NOTIFICATIONS_BUS_NAME]),
                    null,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    null
                );
            } catch (e) {
                // Ignore
            }
        }
        this._connection = null;
        log('[AI Notification] D-Bus service disabled');
    }

    _handleDBusCall(connection, sender, object_path, interface_name, method_name, parameters, invocation) {
        log(`[AI Notification] D-Bus call: ${method_name}`);

        try {
            switch (method_name) {
                case 'ShowNotification':
                    this._handleShowNotification(parameters, invocation);
                    break;
                case 'GetResult':
                    this._handleGetResult(parameters, invocation);
                    break;
                default:
                    invocation.return_error_literal(
                        Gio.io_error_quark(),
                        Gio.IOErrorEnum.NOT_SUPPORTED,
                        'Unknown method'
                    );
            }
        } catch (e) {
            log(`[AI Notification] Error handling ${method_name}: ${e.message}`);
            invocation.return_error_literal(
                Gio.io_error_quark(),
                Gio.IOErrorEnum.FAILED,
                e.message
            );
        }
    }

    _handleShowNotification(parameters, invocation) {
        log('[AI Notification] _handleShowNotification called');

        let title, body, options;
        try {
            // Use recursiveUnpack to fully unpack nested variants
            [title, body, options] = parameters.recursiveUnpack();
            log('[AI Notification] Parameters unpacked successfully');
            log(`[AI Notification] Options keys: ${Object.keys(options).join(', ')}`);
        } catch (e) {
            log(`[AI Notification] Error unpacking parameters: ${e.message}`);
            invocation.return_error_literal(
                Gio.io_error_quark(),
                Gio.IOErrorEnum.FAILED,
                `Failed to unpack parameters: ${e.message}`
            );
            return;
        }

        // Ensure title and body are strings
        const titleStr = title;
        const bodyStr = body;

        // Get code blocks
        const codeBlocks = options['code_blocks'] || [];
        log(`[AI Notification] Code blocks count: ${codeBlocks.length}`);

        const notificationId = this._notificationManager.showNotification({
            title: titleStr,
            body: bodyStr,
            urgency: options['urgency'] || 'normal',
            expireTimeoutMs: options['expire_timeout_ms'] || 0,
            actions: this._unpackActions(options['actions']),
            actionLayout: options['action_layout'] || 'horizontal',
            codeBlocks: codeBlocks,
            maxLines: options['max_lines'] || 0,
        });

        log(`[AI Notification] Created notification: ${notificationId}`);
        invocation.return_value(GLib.Variant.new('(s)', [notificationId]));
    }

    _handleGetResult(parameters, invocation) {
        const [notificationId] = parameters.unpack();
        log(`[AI Notification] GetResult for ${notificationId}`);

        // Simple version: just return empty result for now
        // TODO: Fix the tuple wrapping issue
        invocation.return_error_literal(
            Gio.io_error_quark(),
            Gio.IOErrorEnum.FAILED,
            'GetResult not yet implemented - use signals instead'
        );
    }

    _packPendingResult() {
        // Build a dictionary indicating pending status using object literal
        return new GLib.Variant('a{sv}', {
            action_id: GLib.Variant.new_string('pending'),
            timestamp: GLib.Variant.new_uint64(0),
        });
    }

    emitResult(notificationId, result) {
        if (!this._connection) return;

        this._connection.emit_signal(
            null,
            AI_NOTIFICATIONS_OBJECT_PATH,
            AI_NOTIFICATIONS_INTERFACE,
            'NotificationResult',
            GLib.Variant.new('(sa{sv})', [notificationId, this._packResult(result)])
        );
    }

    _unpackActions(actionsVariant) {
        if (!actionsVariant) return [];
        // If already unpacked (native array), return as-is
        if (Array.isArray(actionsVariant)) return actionsVariant;
        // Otherwise unpack
        return actionsVariant.recursiveUnpack();
    }

    _packResult(result) {
        // Build dictionary variant using object literal approach
        // This is the recommended way in GJS for a{sv} variants
        return new GLib.Variant('a{sv}', {
            action_id: GLib.Variant.new_string(result.actionId),
            timestamp: GLib.Variant.new_uint64(result.timestamp),
        });
    }
}
