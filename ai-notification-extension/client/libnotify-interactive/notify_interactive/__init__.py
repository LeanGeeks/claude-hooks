"""AI Notification Extension - Python Client Library"""

from typing import Optional, List, Tuple, Union

from .client import NotificationClient
from .models import Action, NotificationOptions, NotificationResult
from .exceptions import (
    NotificationError,
    ExtensionNotFoundError,
    NotificationTimeoutError,
    InvalidActionError,
)

__version__ = "0.1.0"
__all__ = [
    "NotificationClient",
    "Action",
    "NotificationOptions",
    "NotificationResult",
    "NotificationError",
    "ExtensionNotFoundError",
    "NotificationTimeoutError",
    "InvalidActionError",
]


# Convenience function for quick notifications
def notify(
    title: str,
    body: str = "",
    actions: Optional[List[Tuple[str, str]]] = None,
    wait: bool = False,
    **kwargs,
) -> Union[str, 'NotificationResult']:
    """
    Quick notification function.

    Args:
        title: Notification title
        body: Notification body
        actions: List of (id, label) tuples
        wait: If True, wait for and return result
        **kwargs: Passed to NotificationOptions

    Returns:
        Notification ID if wait=False, NotificationResult if wait=True

    Example:
        >>> from notify_interactive import notify
        >>> result = notify(
        ...     "Deploy?",
        ...     "Push to production?",
        ...     actions=[("yes", "Deploy"), ("no", "Cancel")],
        ...     wait=True
        ... )
        >>> if result.is_approved:
        ...     print("User approved!")
    """
    client = NotificationClient()

    action_objs = [Action(id=id_val, label=label) for id_val, label in (actions or [])]
    options = NotificationOptions(**kwargs)

    if wait:
        return client.show_and_wait(title, body, action_objs, options)
    else:
        return client.show_notification(title, body, action_objs, options)
