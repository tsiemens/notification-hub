# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools. The implementation is being
delivered in the phases described in `local_md/design.md`.

## Early development progress

The current code contains phase 1: domain models and validation, TOML
configuration, SQLite migrations and repository operations, and retention.
Phase 2 includes the Flask application factory, the notifier-facing producer API
(health, create, outcome waiting, and cancellation), and the read-only hub/UI
API (filtered notification pages, domains, snapshot, and change feed). Signed
authentication and nonce replay protection remain later phase-2 work. The
state-changing hub/UI response and read-state endpoints are wired to the
transactional repository, but are temporarily unauthenticated until that
boundary is added.

## Testing

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
