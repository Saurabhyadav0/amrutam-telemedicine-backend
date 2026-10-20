import uuid
from typing import Protocol

import structlog
from prometheus_client import Counter

logger = structlog.get_logger(__name__)
NOTIFICATIONS = Counter("notifications_sent_total", "Notifications sent", ["template"])


class Notifier(Protocol):
    async def send(self, user_id: uuid.UUID, template: str, data: dict[str, str]) -> None: ...


class LogNotifier:
    """Stand-in for SMS/WhatsApp/email/push providers. Templates carry ids only, never PHI;
    the app fetches details after the user authenticates."""

    async def send(self, user_id: uuid.UUID, template: str, data: dict[str, str]) -> None:
        NOTIFICATIONS.labels(template).inc()
        logger.info("notification_sent", user_id=str(user_id), template=template, **data)
