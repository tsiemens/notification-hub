from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode

from notification_hub.config import ClientConfig
from notification_hub.domain import DomainSummary, Notification, parse_timestamp

from .errors import NetworkError, ProtocolError, ServerError
from .models import (
    ClientSnapshot,
    DomainPage,
    EventPage,
    MutationResult,
    NotificationPage,
    NotificationQuery,
    parse_domain,
    parse_event,
    parse_notification,
)
from .nonce import NoncePool
from .signing import RequestSigner, encode_json
from .transport import HttpResponse, JsonTransport, decode_json_response


class HubClient:
    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: object | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        signer: RequestSigner | None = None,
        mutation_attempts: int = 3,
        snapshot_attempts: int = 3,
        nonce_batch_size: int = 32,
    ) -> None:
        if mutation_attempts < 1 or snapshot_attempts < 1:
            raise ValueError("retry attempt counts must be positive")
        self.config = config
        self._transport = transport or JsonTransport(config.server)
        self._clock = clock
        self._sleep = sleep
        self._random = random_source
        self._signer = signer or RequestSigner.from_file(
            config.key_id, config.private_key_file, clock=clock
        )
        self._mutation_attempts = mutation_attempts
        self._snapshot_attempts = snapshot_attempts
        self._nonces = NoncePool(self._issue_nonces, batch_size=nonce_batch_size, clock=clock)

    def list_notifications(self, query: NotificationQuery | None = None) -> NotificationPage:
        query = query or NotificationQuery()
        value = self._request("GET", "/api/v1/notifications", query=query.parameters())
        if set(value) != {"items", "next_cursor"} or not isinstance(value["items"], list):
            raise ProtocolError("notification page is malformed")
        cursor = value["next_cursor"]
        if cursor is not None and not isinstance(cursor, str):
            raise ProtocolError("notification page cursor is malformed")
        return NotificationPage(tuple(parse_notification(item) for item in value["items"]), cursor)

    def get_notification(self, notification_id: str) -> Notification:
        value = self._request("GET", f"/api/v1/notifications/{quote(notification_id, safe='')}")
        if set(value) != {"notification"}:
            raise ProtocolError("notification response is malformed")
        return parse_notification(value["notification"])

    def get_domain_page(self) -> DomainPage:
        value = self._request("GET", "/api/v1/domains")
        if set(value) != {"items"} or not isinstance(value["items"], list):
            raise ProtocolError("domain response is malformed")
        return DomainPage(tuple(parse_domain(item) for item in value["items"]))

    def list_domains(self) -> list[DomainSummary]:
        return list(self.get_domain_page().items)

    def get_events(self, after: int, *, wait_seconds: int = 0, limit: int = 200) -> EventPage:
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (after, wait_seconds, limit)
            )
            or after < 0
            or not 0 <= wait_seconds <= 30
            or not 1 <= limit <= 500
        ):
            raise ValueError("invalid event query")
        value = self._request(
            "GET",
            "/api/v1/events",
            query={"after": after, "wait_seconds": wait_seconds, "limit": limit},
            timeout=max(self.config.server.request_timeout_seconds, wait_seconds + 1),
        )
        if (
            set(value) != {"events", "last_sequence", "has_more"}
            or not isinstance(value["events"], list)
            or isinstance(value["last_sequence"], bool)
            or not isinstance(value["last_sequence"], int)
            or not isinstance(value["has_more"], bool)
        ):
            raise ProtocolError("event page is malformed")
        events = tuple(parse_event(item) for item in value["events"])
        expected = after + 1
        for event in events:
            if event.seq != expected:
                raise ProtocolError("event page contains a sequence gap")
            expected += 1
        expected_last = events[-1].seq if events else after
        if value["last_sequence"] != expected_last or (value["has_more"] and not events):
            raise ProtocolError("event page cursor is inconsistent")
        return EventPage(events, value["last_sequence"], value["has_more"])

    def get_snapshot(self) -> ClientSnapshot:
        last_error: ServerError | None = None
        for _attempt in range(self._snapshot_attempts):
            try:
                return self._get_snapshot_once()
            except ServerError as exc:
                if not (
                    exc.code == "snapshot_expired"
                    and isinstance(exc.details, dict)
                    and exc.details.get("reset_required") is True
                ):
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error

    def _get_snapshot_once(self) -> ClientSnapshot:
        value = self._request("GET", "/api/v1/snapshot")
        notifications = []
        domains = []
        expected: tuple[int, str, str | None] | None = None
        while True:
            allowed = {"sequence", "generated_at", "domains", "notifications"}
            paged = "snapshot_token" in value or "next_cursor" in value
            if paged:
                allowed |= {"snapshot_token", "next_cursor"}
            if set(value) != allowed:
                raise ProtocolError("snapshot page is malformed")
            sequence = value["sequence"]
            generated_at = value["generated_at"]
            token = value.get("snapshot_token")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
                or not isinstance(generated_at, str)
                or not isinstance(value["domains"], list)
                or not isinstance(value["notifications"], list)
                or (paged and (not isinstance(token, str) or not token))
            ):
                raise ProtocolError("snapshot page is malformed")
            try:
                parse_timestamp(generated_at, require_canonical=True)
            except (TypeError, ValueError) as exc:
                raise ProtocolError("snapshot generation time is malformed") from exc
            metadata = (sequence, generated_at, token)
            if expected is None:
                expected = metadata
            elif metadata != expected:
                raise ProtocolError("snapshot metadata changed between pages")
            domains.extend(parse_domain(item) for item in value["domains"])
            notifications.extend(parse_notification(item) for item in value["notifications"])
            cursor = value.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str):
                raise ProtocolError("snapshot cursor is malformed")
            value = self._request(
                "GET",
                "/api/v1/snapshot",
                query={"snapshot_token": token, "cursor": cursor},
            )
        if len({item.id for item in notifications}) != len(notifications):
            raise ProtocolError("snapshot contains duplicate notification ids")
        if len({item.name for item in domains}) != len(domains):
            raise ProtocolError("snapshot contains duplicate domain names")
        assert expected is not None
        return ClientSnapshot(expected[0], expected[1], tuple(domains), tuple(notifications))

    def set_read_state(self, notification_ids: Sequence[str], read: bool) -> MutationResult:
        ids = list(notification_ids)
        if (
            not ids
            or any(not isinstance(item, str) or not item for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("notification ids must be non-empty unique strings")
        if not isinstance(read, bool):
            raise ValueError("read must be boolean")
        notifications = []
        event_seq = None
        for start in range(0, len(ids), 500):
            value = self._mutation(
                "/api/v1/read-state",
                {"notification_ids": ids[start : start + 500], "read": read},
            )
            if (
                set(value) != {"notifications", "event_seq"}
                or not isinstance(value["notifications"], list)
                or (
                    value["event_seq"] is not None
                    and (
                        isinstance(value["event_seq"], bool)
                        or not isinstance(value["event_seq"], int)
                    )
                )
            ):
                raise ProtocolError("read-state response is malformed")
            notifications.extend(parse_notification(item) for item in value["notifications"])
            if value["event_seq"] is not None:
                event_seq = value["event_seq"]
        return MutationResult(tuple(notifications), event_seq)

    def respond(
        self,
        notification_id: str,
        option_id: str,
        message: str | None = None,
        *,
        request_id: str | None = None,
    ) -> MutationResult:
        if request_id is not None:
            try:
                parsed_request_id = uuid.UUID(request_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("request_id must be a canonical UUIDv4") from exc
            if parsed_request_id.version != 4 or str(parsed_request_id) != request_id:
                raise ValueError("request_id must be a canonical UUIDv4")
        body: dict[str, object] = {
            "request_id": request_id or str(uuid.uuid4()),
            "option_id": option_id,
        }
        if message is not None:
            body["message"] = message
        try:
            value = self._mutation(
                f"/api/v1/notifications/{quote(notification_id, safe='')}/response", body
            )
        except ServerError as exc:
            if isinstance(exc.details, dict) and "notification" in exc.details:
                exc.notification = parse_notification(exc.details["notification"])
            raise
        if (
            set(value) != {"notification", "event_seq"}
            or isinstance(value["event_seq"], bool)
            or not isinstance(value["event_seq"], int)
        ):
            raise ProtocolError("response mutation result is malformed")
        return MutationResult(
            (parse_notification(value["notification"]),), value["event_seq"], True
        )

    def _issue_nonces(self, request_id: str, count: int) -> object:
        body = {"request_id": request_id, "count": count}
        last_error: NetworkError | ServerError | None = None
        for attempt in range(self._mutation_attempts):
            try:
                value = self._request("POST", "/api/v1/auth/nonces", body=body)
                if set(value) != {"nonces"}:
                    raise ProtocolError("nonce response is malformed")
                return value["nonces"]
            except (NetworkError, ServerError) as exc:
                if isinstance(exc, ServerError) and not exc.retryable:
                    raise
                last_error = exc
                if attempt + 1 < self._mutation_attempts:
                    self._sleep(self._retry_delay(attempt, exc))
        assert last_error is not None
        raise last_error

    def _mutation(self, path: str, body: Mapping[str, object]) -> dict[str, Any]:
        last_error: NetworkError | ServerError | None = None
        for attempt in range(self._mutation_attempts):
            nonce = self._nonces.take()
            try:
                return self._request("POST", path, body=body, nonce=nonce)
            except (NetworkError, ServerError) as exc:
                if isinstance(exc, ServerError) and not exc.retryable:
                    raise
                last_error = exc
                if attempt + 1 < self._mutation_attempts:
                    self._sleep(self._retry_delay(attempt, exc))
        assert last_error is not None
        raise last_error

    def _retry_delay(self, attempt: int, error: NetworkError | ServerError) -> float:
        if isinstance(error, ServerError):
            value = next(
                (value for key, value in error.headers.items() if key.lower() == "retry-after"),
                None,
            )
            if value is not None:
                try:
                    return max(0.0, float(value))
                except ValueError:
                    try:
                        return max(0.0, parsedate_to_datetime(value).timestamp() - self._clock())
                    except (TypeError, ValueError, OverflowError):
                        pass
        return 0.5 * (2**attempt) * (0.5 + self._random())

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | Sequence[tuple[str, str]] | None = None,
        body: Mapping[str, object] | None = None,
        nonce: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if query:
            path += "?" + urlencode(query, doseq=True)
        url = self.config.server.url + path
        encoded = None if body is None else encode_json(body)
        headers = self._signer.headers(method, url, encoded, nonce=nonce)
        headers["X-Request-ID"] = str(uuid.uuid4())
        response = self._send(
            method,
            url,
            encoded,
            headers,
            timeout or self.config.server.request_timeout_seconds,
        )
        return decode_json_response(response)

    def _send(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        sender = getattr(self._transport, "send", self._transport)
        if not callable(sender):
            raise TypeError("transport must be callable or provide send()")
        result = sender(method, url, body, headers, timeout)
        if isinstance(result, HttpResponse):
            return result
        if isinstance(result, tuple) and len(result) in {2, 3}:
            response_headers = result[2] if len(result) == 3 else {}
            return HttpResponse(result[0], result[1], response_headers)
        raise TypeError("transport returned an unsupported response")
