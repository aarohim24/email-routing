-- Stable Office internetMessageId lookup for original emails AND confirmation replies.
CREATE INDEX receipts_concern ON receipts(concern_id);
-- Bind this single-mailbox database to one configured mailbox before API/worker use.
CREATE TABLE deployment_config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_migrations VALUES(2);
