"""Client for AI Notification Extension"""

import logging
import time
from typing import Dict, Any, List, Optional

from gi.repository import GLib, Gio

from .exceptions import ExtensionNotFoundError, NotificationTimeoutError
from .models import Action, NotificationOptions, NotificationResult

logger = logging.getLogger(__name__)

# D-Bus constants
BUS_NAME = "org.gnome.Shell.Extensions.AINotifications"
OBJECT_PATH = "/org/gnome/Shell/Extensions/AINotifications"
INTERFACE = "org.gnome.Shell.Extensions.AINotifications"


class NotificationClient:
    """Client for AI Notification Extension"""

    def __init__(self):
        """Initialize the notification client"""
        self._bus: Optional[Gio.DBusConnection] = None
        self._results: Dict[str, Dict[str, Any]] = {}
        self._signal_id: Optional[int] = None

    @property
    def bus(self) -> Gio.DBusConnection:
        """Get or create D-Bus connection"""
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def show_notification(
        self,
        title: str,
        body: str = "",
        actions: Optional[List[Action]] = None,
        options: Optional[NotificationOptions] = None,
    ) -> str:
        """
        Show a notification and return its ID.

        Args:
            title: Notification title
            body: Notification body text
            actions: List of Action objects
            options: NotificationOptions for display settings

        Returns:
            Notification ID string

        Raises:
            ExtensionNotFoundError: If extension is not available
        """
        if options is None:
            options = NotificationOptions()

        opts_dict = options.to_dict()

        if actions:
            opts_dict["actions"] = [{"id": a.id, "label": a.label} for a in actions]

        options_variant = self._build_options_variant(opts_dict)

        try:
            result = self.bus.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                "ShowNotification",
                GLib.Variant("(ssa{sv})", (title, body, options_variant)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.GError as e:
            if "org.freedesktop.DBus.Error.ServiceUnknown" in str(e):
                raise ExtensionNotFoundError(
                    "AI Notification Extension not found. "
                    "Ensure it's installed and enabled."
                ) from e
            raise

        if result:
            notification_id: str = result.unpack()[0]
            logger.info(f"Notification sent: {notification_id}")
            return notification_id

        raise ExtensionNotFoundError("Failed to send notification")

    def wait_for_result(
        self,
        notification_id: str,
        timeout: int = 300,
    ) -> NotificationResult:
        """
        Wait for notification result.

        Args:
            notification_id: ID from show_notification()
            timeout: Maximum seconds to wait

        Returns:
            NotificationResult with user's choice

        Raises:
            NotificationTimeoutError: If timeout is reached
        """
        start_time = time.time()

        # Subscribe to result signals
        self._signal_id = self.bus.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            "NotificationResult",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_result_signal,
            None,
        )

        try:
            while time.time() - start_time < timeout:
                # Check local cache first
                if notification_id in self._results:
                    result_dict = self._results.pop(notification_id)
                    return NotificationResult(**result_dict)

                # Poll via D-Bus
                result = self._poll_result(notification_id)
                if result:
                    return result

                time.sleep(0.1)

            raise NotificationTimeoutError(
                f"No response within {timeout} seconds"
            )

        finally:
            if self._signal_id:
                self.bus.signal_unsubscribe(self._signal_id)
                self._signal_id = None

    def show_and_wait(
        self,
        title: str,
        body: str = "",
        actions: Optional[List[Action]] = None,
        options: Optional[NotificationOptions] = None,
        timeout: int = 300,
    ) -> NotificationResult:
        """
        Show notification and wait for result in one call.

        This is a convenience method combining show_notification() and
        wait_for_result().

        Args:
            title: Notification title
            body: Notification body text
            actions: List of Action objects
            options: NotificationOptions for display settings
            timeout: Maximum seconds to wait

        Returns:
            NotificationResult with user's choice
        """
        notification_id = self.show_notification(title, body, actions, options)
        return self.wait_for_result(notification_id, timeout)

    def _on_result_signal(
        self,
        connection: Gio.DBusConnection,
        sender_name: str,
        object_path: str,
        interface_name: str,
        signal_name: str,
        parameters: GLib.Variant,
        user_data: Optional[object],
    ) -> None:
        """Handle result signal from extension"""
        notification_id, result = parameters.unpack()
        self._results[notification_id] = result

    def _poll_result(self, notification_id: str) -> Optional[NotificationResult]:
        """Poll for result via D-Bus GetResult method"""
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
            result_dict = result.unpack()[0]
            if result_dict:
                return NotificationResult(**result_dict)

        return None

    def _build_options_variant(self, options: dict) -> GLib.Variant:
        """Build D-Bus variant for options dictionary"""
        builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))

        for key, value in options.items():
            if key == "actions":
                # Array of {id, label} dictionaries
                action_variants = [
                    GLib.Variant("a{ss}", {"id": a["id"], "label": a["label"]})
                    for a in value
                ]
                builder.add("{sv}", key, GLib.Variant("aa{ss}", action_variants))

            elif key == "code_blocks":
                builder.add("{sv}", key, GLib.Variant("as", value))

            else:
                # Primitive types
                type_map = {
                    "urgency": "s",
                    "expire_timeout_ms": "i",
                    "action_layout": "s",
                    "max_lines": "i",
                }
                if key in type_map:
                    builder.add("{sv}", key, GLib.Variant(type_map[key], value))

        return builder.end()
