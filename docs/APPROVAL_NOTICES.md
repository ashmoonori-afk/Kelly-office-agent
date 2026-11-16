# Approval notifications

Every pending approval in the paired owner's project is eligible for a messenger
notification, including work started in the web UI. The card names the current
tool, destination, recipient, impact and expiry in KST. Approval buttons use opaque
tokens stored as hashes in `channel_actions`; the raw token is carried only by the
outgoing card. Each token lasts until its approval expires and binds the user,
project, pairing revision, decision, approval revision and proposal hash.

A press commits its inbox event, decision, token consumption and receive cursor
together. Repeated presses and replayed updates cannot repeat the decision.
An acknowledgement only ends the messenger's waiting indicator. A used button
answers that the request was already handled.

The notice loop reads `approvals`, without a ledger, for all run sources.
It queues every initial card, outcome edit, fallback follow-up and approved-step
result in the existing outbox. Resolving in the web inbox uses the same approval
row, so the next notice iteration writes back the outcome, owner and time and
removes the buttons. Expired requests are marked expired and their notices lose
their buttons. A channel without edit support receives one follow-up instead.
Once the approved step finishes, the same paired chat receives its status and
receipt, even if later steps in the run are still pending.

Pairing remains scoped to one owner and project until multi-user roles arrive.
Approval notifications grant no general permission to send arbitrary web-run
results or unrelated artifacts. Re-pairing or rotating bot credentials invalidates
queued deliveries and old buttons.

## Database upgrade and rollback

`init_db` creates the original schema for a new database and runs numbered
migration `002_approval_notices.up.sql` inside one immediate transaction. Existing
version-one databases take the same migration. Pairings are copied from their
connection records and legacy callback rows move from `kv` to `channel_actions`.
The database schema version becomes 2; the configuration format stays at 1.
Connection records remain the compatibility source for polling and credential
epochs; their pairing changes update `channel_bindings` in the same transaction.

With the daemon stopped and a database backup retained, an operator can call
`kelly.migrations.downgrade(conn)` before using version-one code. It restores
migrated legacy callback records including their consumed state, cancels pending
C2 deliveries, drops the three tables and three approval columns, and restores
schema version 1. New hash-only tokens are intentionally revoked by rollback.
Reopening with C2 code upgrades the database again.

## Verification boundary

The synthetic adapter tests cover the web request, engine approval, messenger
press, web inbox, metadata, continuation and final chat receipt. The HTTP
walkthrough replaces configuration loading and secret storage at the platform
boundary because platform support is on a separate branch. It uses real HTTP
routes, SQLite, engine steps, handwritten Telegram payloads and `FakeAdapter`.
No test contacts a bot or network. A reviewer must still verify the real phone
walk and Windows CI.
