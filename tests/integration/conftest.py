import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from app.core.config import get_settings
from app.core.deps import get_payment_gateway
from app.worker.jobs import WorkerDeps


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, str, dict[str, str]]] = []

    async def send(self, user_id: uuid.UUID, template: str, data: dict[str, str]) -> None:
        self.sent.append((user_id, template, data))


@pytest.fixture
async def worker(app: FastAPI) -> AsyncIterator[WorkerDeps]:
    yield WorkerDeps(
        db=app.state.db,
        settings=get_settings(),
        gateway=get_payment_gateway(),
        notifier=RecordingNotifier(),
    )
