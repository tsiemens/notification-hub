# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools. The implementation is being
delivered in the phases described in `local_md/design.md`.

The current code contains phase 1: domain models and validation, TOML
configuration, SQLite migrations and repository operations, and retention.
Phase 2 includes the Flask application factory and the notifier-facing producer
API (health, create, outcome waiting, and cancellation). Signed hub/UI client
endpoints are registered as stubs for later phase-2 work.

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
