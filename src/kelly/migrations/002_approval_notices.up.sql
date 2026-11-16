CREATE TABLE channel_bindings (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, channel TEXT NOT NULL,
    address_ref TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pairing','paired','revoked')),
    revision INTEGER NOT NULL DEFAULT 1, paired_at TEXT,
    UNIQUE(channel,address_ref)
);
CREATE TABLE channel_actions (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, approval_id TEXT,
    target_id TEXT NOT NULL, decision TEXT NOT NULL, user_id TEXT NOT NULL,
    project_id TEXT NOT NULL, binding_id TEXT NOT NULL REFERENCES channel_bindings(id),
    binding_revision INTEGER NOT NULL, proposal_hash TEXT NOT NULL,
    expected_revision INTEGER NOT NULL, expires_at TEXT NOT NULL, used_at TEXT,
    legacy_token TEXT, legacy_expires_at TEXT
);
CREATE TABLE approval_notices (
    id TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id),
    user_id TEXT NOT NULL, channel TEXT NOT NULL, binding_id TEXT NOT NULL REFERENCES channel_bindings(id),
    binding_revision INTEGER NOT NULL, sent_ref TEXT,
    state TEXT NOT NULL CHECK(state IN ('queued','sent','updated','failed')),
    UNIQUE(approval_id,user_id,channel)
);
ALTER TABLE approvals ADD COLUMN resolved_by TEXT;
ALTER TABLE approvals ADD COLUMN resolved_via TEXT;
ALTER TABLE approvals ADD COLUMN reject_reason TEXT;
