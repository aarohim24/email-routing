-- Provider-issued immutable IDs identify every receipt, including confirmation replies.
-- Old receipts without an ID remain nullable; do not invent historical provider IDs.
ALTER TABLE receipts ADD COLUMN graph_id TEXT;
UPDATE receipts SET graph_id=json_extract(payload,'$.id') WHERE json_valid(payload) AND json_type(payload,'$.id')='text';
UPDATE receipts SET graph_id=(SELECT c.graph_id FROM concerns c WHERE c.id=receipts.concern_id AND c.message_id=receipts.message_id) WHERE graph_id IS NULL;
CREATE UNIQUE INDEX receipts_graph_id ON receipts(graph_id) WHERE graph_id IS NOT NULL;
INSERT INTO schema_migrations VALUES(4);
