# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools. The implementation is being
delivered in the phases described in `local_md/design.md`.

The current code contains phase 1: domain models and validation, TOML
configuration, SQLite migrations and repository operations, and retention.

Run the tests with:

```sh
uv run python -m unittest discover -s tests
```

