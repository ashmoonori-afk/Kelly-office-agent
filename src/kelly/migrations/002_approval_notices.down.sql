DROP TABLE approval_notices;
DROP TABLE channel_actions;
DROP TABLE channel_bindings;
ALTER TABLE approvals DROP COLUMN reject_reason;
ALTER TABLE approvals DROP COLUMN resolved_via;
ALTER TABLE approvals DROP COLUMN resolved_by;
