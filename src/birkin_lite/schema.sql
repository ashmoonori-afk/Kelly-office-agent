-- Specification DDL, not a migration applied to the existing Birkin database.
-- JSON internals, permissions and transitions are validated by application code.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE automations (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL CHECK (json_valid(config_json)),
    next_due_utc TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('ui', 'telegram', 'automation')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    policy_json TEXT NOT NULL CHECK (json_valid(policy_json)),
    plan_json TEXT CHECK (plan_json IS NULL OR json_valid(plan_json)),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'preparing', 'running', 'checking', 'ready',
        'awaiting_input', 'retry_wait', 'awaiting_budget', 'awaiting_approval',
        'cancel_requested', 'cancelled', 'succeeded', 'rejected', 'expired',
        'failed', 'outcome_unknown'
    )),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    parent_run_id TEXT REFERENCES runs(id),
    automation_id TEXT REFERENCES automations(id),
    automation_revision INTEGER,
    task_cap_micro_usd INTEGER CHECK (task_cap_micro_usd IS NULL OR task_cap_micro_usd >= 0),
    task_cap_tokens INTEGER CHECK (task_cap_tokens IS NULL OR task_cap_tokens >= 0),
    error_code TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (actor_id, source, idempotency_key),
    CHECK ((automation_id IS NULL AND automation_revision IS NULL) OR
           (automation_id IS NOT NULL AND automation_revision >= 1))
);

CREATE TABLE steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 7),
    kind TEXT NOT NULL CHECK (kind IN ('tool', 'model', 'verify', 'apply')),
    effect TEXT NOT NULL CHECK (effect IN ('read', 'draft', 'external_write', 'destructive')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'running', 'checking', 'awaiting_approval',
        'succeeded', 'failed', 'cancelled', 'skipped', 'outcome_unknown'
    )),
    spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
    output_json TEXT CHECK (output_json IS NULL OR json_valid(output_json)),
    validation_json TEXT CHECK (validation_json IS NULL OR json_valid(validation_json)),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_by TEXT,
    claimed_at TEXT,
    completed_at TEXT,
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, id)
);

CREATE TABLE calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    step_id TEXT,
    logical_call_no INTEGER NOT NULL CHECK (logical_call_no BETWEEN 1 AND 12),
    transport_attempt_no INTEGER NOT NULL CHECK (transport_attempt_no BETWEEN 1 AND 16),
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('plan', 'generate', 'repair', 'review', 'replan')),
    request_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    route_json TEXT NOT NULL CHECK (json_valid(route_json)),
    price_snapshot_json TEXT NOT NULL CHECK (json_valid(price_snapshot_json)),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'in_flight', 'settled', 'released', 'unknown')),
    reserved_micro_usd INTEGER CHECK (reserved_micro_usd IS NULL OR reserved_micro_usd >= 0),
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens >= 0),
    actual_micro_usd INTEGER CHECK (actual_micro_usd IS NULL OR actual_micro_usd >= 0),
    input_tokens_total INTEGER CHECK (input_tokens_total IS NULL OR input_tokens_total >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    output_tokens_total INTEGER CHECK (output_tokens_total IS NULL OR output_tokens_total >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    usage_basis TEXT NOT NULL CHECK (usage_basis IN ('estimated', 'provider', 'unknown')),
    raw_usage_json TEXT CHECK (raw_usage_json IS NULL OR json_valid(raw_usage_json)),
    provider_request_id TEXT,
    finish_reason TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    timezone_name TEXT NOT NULL,
    day_start_utc TEXT NOT NULL,
    day_end_utc TEXT NOT NULL,
    month_start_utc TEXT NOT NULL,
    month_end_utc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (run_id, transport_attempt_no),
    FOREIGN KEY (run_id, step_id) REFERENCES steps(run_id, id),
    CHECK ((purpose IN ('plan', 'replan') AND step_id IS NULL) OR
           (purpose NOT IN ('plan', 'replan') AND step_id IS NOT NULL)),
    CHECK (cached_input_tokens IS NULL OR input_tokens_total IS NULL OR
           cached_input_tokens <= input_tokens_total),
    CHECK (state != 'settled' OR settled_at IS NOT NULL)
);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    step_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json)),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'approved', 'rejected', 'expired', 'consumed', 'cancelled'
    )),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    consumed_at TEXT,
    UNIQUE (run_id, step_id, proposal_hash),
    FOREIGN KEY (run_id, step_id) REFERENCES steps(run_id, id),
    CHECK (state != 'consumed' OR consumed_at IS NOT NULL)
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('source', 'draft', 'result', 'memory')),
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
    validation_json TEXT CHECK (validation_json IS NULL OR json_valid(validation_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE inbox_events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('telegram', 'automation')),
    connection_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    state TEXT NOT NULL DEFAULT 'received' CHECK (state IN (
        'received', 'enqueued', 'rejected', 'coalesced', 'skipped'
    )),
    run_id TEXT REFERENCES runs(id),
    received_at TEXT NOT NULL,
    UNIQUE (source, connection_id, event_key)
);

CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    delivery_kind TEXT NOT NULL,
    recipient_ref TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    authority_json TEXT NOT NULL CHECK (json_valid(authority_json)),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'sending', 'sent', 'retry_wait', 'failed', 'unknown', 'cancelled'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    next_attempt_at TEXT,
    remote_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (run_id, delivery_kind, recipient_ref, content_hash)
);

CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at TEXT NOT NULL
);

CREATE INDEX runs_queue ON runs(state, created_at);
CREATE INDEX runs_owner ON runs(actor_id, project_id, created_at);
CREATE INDEX calls_budget ON calls(state, created_at);
CREATE INDEX approvals_pending ON approvals(actor_id, state, expires_at);
CREATE INDEX automations_due ON automations(enabled, next_due_utc);
CREATE INDEX outbox_due ON outbox(state, next_attempt_at);
