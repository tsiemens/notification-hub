# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools. The implementation is being
delivered in the phases described in `local_md/design.md`.

## Development progress

Phases 1 and 2 are implemented: domain models and validation, TOML
configuration, SQLite migrations and repository operations, retention, the
complete Flask API, RFC 9421 public-key request authentication, RFC 9530 content
digests, scope enforcement, and transactional single-use mutation nonces.
Producer create, outcome, and cancellation routes remain intentionally
unauthenticated as specified in the design.

## Testing

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
