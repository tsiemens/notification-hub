from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from notification_hub.config import LimitsConfig, ServerConfig
from notification_hub.server import create_app


class ServerStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = self.app.test_client()

    def test_complete_v1_route_surface_is_registered(self) -> None:
        routes = {
            (rule.rule, method)
            for rule in self.app.url_map.iter_rules()
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"} and rule.endpoint != "static"
        }
        self.assertEqual(
            routes,
            {
                ("/healthz", "GET"),
                ("/api/v1/auth/nonces", "POST"),
                ("/api/v1/notifications", "POST"),
                ("/api/v1/notifications", "GET"),
                ("/api/v1/notifications/<notification_id>", "GET"),
                ("/api/v1/notifications/<notification_id>/cancel", "POST"),
                ("/api/v1/notifications/<notification_id>/outcome", "GET"),
                ("/api/v1/notifications/<notification_id>/response", "POST"),
                ("/api/v1/notifications/<notification_id>/read-state", "PATCH"),
                ("/api/v1/read-state", "POST"),
                ("/api/v1/domains", "GET"),
                ("/api/v1/snapshot", "GET"),
                ("/api/v1/events", "GET"),
            },
        )

    def test_stub_uses_error_envelope_and_echoes_request_id(self) -> None:
        response = self.client.get(
            "/api/v1/notifications/example", headers={"X-Request-ID": "request-123"}
        )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.headers["X-Request-ID"], "request-123")
        self.assertEqual(
            response.get_json(),
            {
                "error": {
                    "code": "not_implemented",
                    "message": "This API endpoint has not been implemented",
                    "details": {"endpoint": "notification_get"},
                }
            },
        )


class ProducerEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        config = ServerConfig(database=Path(self.temporary_directory.name) / "hub.sqlite3")
        self.app = create_app(config)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def notification(*, options: bool = True) -> dict[str, object]:
        return {
            "id": str(uuid.uuid4()),
            "domain": "build-container",
            "sender": "codex",
            "summary": "Approval required",
            "message": "Run the test suite?",
            "tags": ["task:test"],
            "response_options": (
                [
                    {
                        "id": "approve",
                        "label": "Approve",
                        "message_mode": "none",
                        "appearance": "primary",
                    }
                ]
                if options
                else []
            ),
        }

    def test_health_initializes_and_checks_the_database(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_create_retry_is_idempotent(self) -> None:
        payload = self.notification()
        first = self.client.post("/api/v1/notifications", json=payload)
        retry = self.client.post("/api/v1/notifications", json=payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.get_json(), retry.get_json())
        self.assertEqual(first.get_json()["notification"]["response_state"], "pending")

    def test_create_rejects_unknown_fields_and_changed_retry(self) -> None:
        payload = self.notification()
        invalid = {**payload, "created_at": "2026-09-20T00:00:00.000Z"}
        response = self.client.post("/api/v1/notifications", json=invalid)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_data")

        self.assertEqual(
            self.client.post("/api/v1/notifications", json=payload).status_code, 201
        )
        changed = {**payload, "summary": "Different"}
        conflict = self.client.post("/api/v1/notifications", json=changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["error"]["code"], "idempotency_conflict")

    def test_outcome_and_cancel(self) -> None:
        payload = self.notification()
        self.client.post("/api/v1/notifications", json=payload)
        outcome_url = f"/api/v1/notifications/{payload['id']}/outcome"
        pending = self.client.get(outcome_url)
        self.assertEqual(
            pending.get_json(),
            {"notification_id": payload["id"], "state": "pending", "response": None},
        )

        cancel_url = f"/api/v1/notifications/{payload['id']}/cancel"
        cancelled = self.client.post(cancel_url, json={"reason": "No longer needed"})
        retry = self.client.post(cancel_url, json={"reason": "No longer needed"})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json(), retry.get_json())
        self.assertEqual(self.client.get(outcome_url).get_json()["state"], "cancelled")

    def test_outcome_validates_wait_and_unknown_notification(self) -> None:
        notification_id = str(uuid.uuid4())
        invalid = self.client.get(
            f"/api/v1/notifications/{notification_id}/outcome?wait_seconds=31"
        )
        self.assertEqual(invalid.status_code, 422)
        missing = self.client.get(f"/api/v1/notifications/{notification_id}/outcome")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.get_json()["error"]["details"]["notification_id"], notification_id
        )

    def test_waiting_outcome_wakes_after_cancellation(self) -> None:
        payload = self.notification()
        self.client.post("/api/v1/notifications", json=payload)
        outcome_url = f"/api/v1/notifications/{payload['id']}/outcome?wait_seconds=2"
        cancel_url = f"/api/v1/notifications/{payload['id']}/cancel"
        started = threading.Event()
        result: list[tuple[int, dict[str, object]]] = []

        def wait_for_outcome() -> None:
            with self.app.test_client() as client:
                started.set()
                response = client.get(outcome_url)
                result.append((response.status_code, response.get_json()))

        waiter = threading.Thread(target=wait_for_outcome)
        waiter.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        self.client.post(cancel_url)
        waiter.join(1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertEqual(result[0][1]["state"], "cancelled")

    def test_create_limits_return_rate_limited(self) -> None:
        config = ServerConfig(
            database=Path(self.temporary_directory.name) / "limited.sqlite3",
            limits=LimitsConfig(creates_per_minute=1, pending_total=1),
        )
        client = create_app(config).test_client()
        self.assertEqual(
            client.post("/api/v1/notifications", json=self.notification()).status_code, 201
        )
        response = client.post("/api/v1/notifications", json=self.notification())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
