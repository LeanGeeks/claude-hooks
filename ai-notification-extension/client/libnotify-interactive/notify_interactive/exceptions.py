"""Exception classes for notify-interactive"""


class NotificationError(Exception):
    """Base exception for notification errors"""

    pass


class ExtensionNotFoundError(NotificationError):
    """Raised when the GNOME extension is not available"""

    pass


class NotificationTimeoutError(NotificationError):
    """Raised when notification waiting times out"""

    pass


class InvalidActionError(NotificationError):
    """Raised when an invalid action is specified"""

    pass
