"""Shared fake-upstream fixtures for the offline endpoint suite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.upstream import FrankfurterClient


class FakeUpstream:
    """Programmable MockTransport handler with request history."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.default_rate = "1.0847"
        self.default_rate_date = "2026-08-28"
        self.currency_payload = {
            "EUR": "Euro",
            "TRY": "Turkish lira",
            "USD": "United States dollar",
            "GBP": "Pound sterling",
        }
        self._responses: dict[str, tuple[int, bytes]] = {}
        self._exceptions: dict[str, Exception] = {}

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def reset_calls(self) -> None:
        self.requests.clear()

    def set_json(self, path: str, payload: object, status: int = 200) -> None:
        self._responses[path] = (status, json.dumps(payload).encode())
        self._exceptions.pop(path, None)

    def set_content(self, path: str, content: bytes, status: int = 200) -> None:
        self._responses[path] = (status, content)
        self._exceptions.pop(path, None)

    def set_rate(
        self,
        path: str,
        *,
        rate_date: str,
        rate: str | float = "1.0847",
        target: str = "TRY",
    ) -> None:
        # Build the numeric token directly so the client exercises
        # json.loads(..., parse_float=Decimal), not a quoted-rate shortcut.
        content = (
            f'{{"amount":1,"base":"EUR","date":"{rate_date}",'
            f'"rates":{{"{target}":{rate}}}}}'
        ).encode()
        self.set_content(path, content)

    def set_exception(self, path: str, exception: Exception) -> None:
        self._exceptions[path] = exception
        self._responses.pop(path, None)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self._exceptions:
            raise self._exceptions[path]

        if path in self._responses:
            status, content = self._responses[path]
        elif path.endswith("/currencies"):
            status, content = 200, json.dumps(self.currency_payload).encode()
        else:
            status, content = 200, (
                f'{{"amount":1,"base":"EUR","date":"{self.default_rate_date}",'
                f'"rates":{{"TRY":{self.default_rate}}}}}'
            ).encode()
        return httpx.Response(
            status,
            content=content,
            headers={"content-type": "application/json"},
            request=request,
        )


@pytest.fixture
def fake_upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def client(fake_upstream: FakeUpstream) -> Iterator[TestClient]:
    upstream = FrankfurterClient(
        transport=httpx.MockTransport(fake_upstream),
    )
    with TestClient(app) as test_client:
        # Lifespan creates the production client. Replace only the state slot
        # after startup so the endpoint uses this in-process transport.
        app.state.upstream = upstream
        yield test_client
    # The lifespan does not own the injected test client.
    asyncio.run(upstream.close())
