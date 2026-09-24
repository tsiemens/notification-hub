from __future__ import annotations

from argparse import Namespace
from io import StringIO

import pytest

from notification_hub.client.errors import ServerError
from notification_hub.client.nonce import NoncePool
from notification_hub.debug_cli import cli


def _issued(count: int) -> list[dict[str, str]]:
    return [
        {"value": f"nonce-{index}", "expires_at": "2099-01-01T00:00:00.000Z"}
        for index in range(count)
    ]


def test_pool_uses_remaining_nonces_when_refill_is_rate_limited() -> None:
    calls = 0

    def fetch(_request_id: str, count: int) -> object:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ServerError(429, "rate_limited", "nonce limit reached")
        return _issued(count)

    pool = NoncePool(fetch, batch_size=4, clock=lambda: 0)
    assert [pool.take(), pool.take(), pool.take(), pool.take()] == [
        "nonce-0",
        "nonce-1",
        "nonce-2",
        "nonce-3",
    ]
    with pytest.raises(ServerError):
        pool.take()


def test_pool_reduces_batch_to_server_limit() -> None:
    requested: list[int] = []

    def fetch(_request_id: str, count: int) -> object:
        requested.append(count)
        if count > 2:
            raise ServerError(429, "rate_limited", "nonce limit reached")
        return _issued(count)

    pool = NoncePool(fetch, batch_size=32, clock=lambda: 0)
    assert pool.take() == "nonce-0"
    assert requested == [32, 16, 8, 4, 2]
    with pytest.raises(ValueError, match="between 1 and 64"):
        NoncePool(fetch, batch_size=65)


def test_one_shot_cli_allocates_one_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[int] = []

    class Client:
        def __init__(self, _config: object, *, nonce_batch_size: int) -> None:
            requested.append(nonce_batch_size)

        def list_domains(self) -> list[object]:
            return []

    monkeypatch.setattr(cli, "HubClient", Client)
    args = Namespace(command="domains", json=True)
    assert cli.run(args, object(), stdout=StringIO()) == cli.EXIT_OK  # type: ignore[arg-type]
    assert requested == [1]
