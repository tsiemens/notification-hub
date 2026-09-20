MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE domains (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 128),
        created_at TEXT NOT NULL,
        last_activity_at TEXT NOT NULL
    );

    CREATE TABLE notifications (
        id TEXT PRIMARY KEY CHECK (
            length(id) = 36 AND substr(id, 15, 1) = '4'
        ),
        domain_id INTEGER NOT NULL REFERENCES domains(id),
        sender TEXT NOT NULL CHECK (length(sender) BETWEEN 1 AND 128),
        summary TEXT NOT NULL CHECK (
            length(summary) BETWEEN 1 AND 256
            AND instr(summary, char(10)) = 0 AND instr(summary, char(13)) = 0
        ),
        message_markdown TEXT NOT NULL CHECK (length(message_markdown) <= 32768),
        details_markdown TEXT CHECK (
            details_markdown IS NULL OR length(details_markdown) <= 131072
        ),
        tags_json TEXT NOT NULL CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
        priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
        source_created_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        read_at TEXT,
        response_state TEXT NOT NULL CHECK (
            response_state IN ('not_requested', 'pending', 'answered', 'cancelled', 'expired')
        ),
        cancelled_at TEXT,
        cancellation_reason TEXT CHECK (
            cancellation_reason IS NULL OR length(cancellation_reason) <= 16384
        ),
        version INTEGER NOT NULL CHECK (version >= 1),
        create_fingerprint TEXT NOT NULL CHECK (length(create_fingerprint) = 64)
    );

    CREATE TABLE response_options (
        notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 15),
        option_id TEXT NOT NULL CHECK (length(option_id) BETWEEN 1 AND 64),
        label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
        message_mode TEXT NOT NULL CHECK (message_mode IN ('none', 'optional', 'required')),
        appearance TEXT NOT NULL CHECK (appearance IN ('default', 'primary', 'danger')),
        PRIMARY KEY (notification_id, option_id),
        UNIQUE (notification_id, position)
    );

    CREATE TABLE responses (
        notification_id TEXT PRIMARY KEY REFERENCES notifications(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL UNIQUE,
        option_id TEXT NOT NULL,
        message TEXT CHECK (message IS NULL OR length(message) <= 16384),
        responded_at TEXT NOT NULL,
        responder_principal TEXT NOT NULL CHECK (length(responder_principal) BETWEEN 1 AND 256),
        FOREIGN KEY (notification_id, option_id)
            REFERENCES response_options(notification_id, option_id) ON DELETE CASCADE
    );

    CREATE TABLE auth_nonce_batches (
        key_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        requested_count INTEGER NOT NULL CHECK (requested_count BETWEEN 1 AND 64),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (key_id, request_id)
    );

    CREATE TABLE auth_nonces (
        nonce TEXT PRIMARY KEY,
        key_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        FOREIGN KEY (key_id, request_id)
            REFERENCES auth_nonce_batches(key_id, request_id) ON DELETE CASCADE
    );

    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL CHECK (event_type IN (
            'notification.created', 'notification.updated',
            'notifications.read_state_changed', 'notification.deleted', 'domain.deleted'
        )),
        entity_id TEXT,
        occurred_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
    );

    CREATE INDEX notifications_domain_created_idx
        ON notifications(domain_id, created_at, id);
    CREATE INDEX notifications_created_idx ON notifications(created_at, id);
    CREATE INDEX notifications_state_created_idx
        ON notifications(response_state, created_at);
    CREATE INDEX notifications_read_created_idx ON notifications(read_at, created_at);
    CREATE INDEX auth_nonces_key_expiry_used_idx
        ON auth_nonces(key_id, expires_at, used_at);
    CREATE INDEX events_occurred_idx ON events(occurred_at);
    """,
)


LATEST_SCHEMA_VERSION = len(MIGRATIONS)
