import unittest
from test_workflow import CaseFixture, message
from alumni.config import Settings
from alumni.db import transaction
from alumni.graph import GraphError
from alumni.workflow import get, ingest
from alumni.worker import deliver_one, recover, reconcile, poll


class FakeGraph:
    def __init__(self, failure=None):
        self.sent = []
        self.failure = failure
        self.drafts = 0

    def draft(self, job, concern):
        self.drafts += 1
        return 'draft-1'

    def send(self, draft):
        if self.failure:
            raise self.failure
        self.sent.append(draft)


class DeliveryTests(CaseFixture, unittest.TestCase):
    def test_dry_run_lifecycle(self):
        cid = self.setup_case()
        fake = FakeGraph()
        self.act(cid, 'approve')
        deliver_one(self.db, self.settings, fake)
        self.assertEqual(fake.sent, [])
        self.assertEqual(get(self.db, cid)['status'], 'FORWARDED')
        self.act(cid, 'addressed')
        self.assertEqual(get(self.db, cid)['status'], 'ADDRESSED')
        deliver_one(self.db, self.settings, fake)
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_CONFIRMATION')
        ingest(self.db, message('reply1', 'Re: Transcript', 'RESOLVED'))
        self.assertEqual(get(self.db, cid)['status'], 'CLOSED')
        self.act(cid, 'reopen', reason='Student reported recurrence')
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_APPROVAL')
        self.assertFalse(deliver_one(self.db, self.settings, fake))

    def test_unresolved_requires_approval_again(self):
        cid = self.setup_case()
        self.act(cid, 'approve')
        deliver_one(self.db, self.settings)
        self.act(cid, 'addressed')
        deliver_one(self.db, self.settings)
        ingest(self.db, message('reply2', 'Re: Transcript', 'NOT RESOLVED'))
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_APPROVAL')
        self.assertFalse(deliver_one(self.db, self.settings))

    def test_unclear_reply_needs_review(self):
        cid = self.setup_case()
        self.act(cid, 'approve')
        deliver_one(self.db, self.settings)
        self.act(cid, 'addressed')
        deliver_one(self.db, self.settings)
        ingest(self.db, message('reply3', 'Re: Transcript', 'Thanks but I have another question'))
        self.assertEqual(get(self.db, cid)['status'], 'NEEDS_REVIEW')

    def test_live_adapter_mock_and_no_duplicate_send(self):
        self.settings.dry_run = False
        cid = self.setup_case()
        self.act(cid, 'approve')
        fake = FakeGraph()
        deliver_one(self.db, self.settings, fake)
        self.assertEqual(fake.sent, ['draft-1'])
        self.assertFalse(deliver_one(self.db, self.settings, fake))
        self.assertEqual(fake.sent, ['draft-1'])

    def test_ambiguous_send_not_retried(self):
        self.settings.dry_run = False
        cid = self.setup_case()
        self.act(cid, 'approve')
        fake = FakeGraph(GraphError(0))
        deliver_one(self.db, self.settings, fake)
        job = self.db.execute('SELECT * FROM jobs WHERE kind="FORWARD"').fetchone()
        self.assertEqual(job['state'], 'UNCERTAIN')
        self.assertEqual(get(self.db, cid)['status'], 'FAILED')
        self.assertFalse(deliver_one(self.db, self.settings, fake))
        with self.assertRaises(ValueError):
            self.act(cid, 'retry')
        reconcile(self.db, job['id'], True, 'admin', 'Verified Sent Items reference 123')
        self.assertEqual(get(self.db, cid)['status'], 'FORWARDED')

    def test_failure_safe_retry_requires_approval(self):
        self.settings.dry_run = False
        cid = self.setup_case()
        self.act(cid, 'approve')
        deliver_one(self.db, self.settings, FakeGraph(GraphError(403)))
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE kind="FORWARD"').fetchone()[0], 'FAILED')
        self.act(cid, 'retry')
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_APPROVAL')
        self.assertFalse(deliver_one(self.db, self.settings, FakeGraph()))
        self.act(cid, 'approve')
        fake = FakeGraph()
        deliver_one(self.db, self.settings, fake)
        self.assertEqual(len(fake.sent), 1)

    def test_restart_quarantines_running_job(self):
        cid = self.setup_case()
        self.act(cid, 'approve')
        self.db.execute("UPDATE jobs SET state='RUNNING' WHERE kind='FORWARD'")
        recover(self.db)
        job = self.db.execute('SELECT * FROM jobs WHERE kind="FORWARD"').fetchone()
        self.assertEqual(job['state'], 'UNCERTAIN')
        self.assertFalse(deliver_one(self.db, self.settings))
        reconcile(self.db, job['id'], False, 'admin', 'No send occurred, draft only')
        self.act(cid, 'retry')
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_APPROVAL')

    def test_mode_change_blocks_old_approvals(self):
        cid = self.setup_case()
        self.act(cid, 'approve')
        self.settings.dry_run = False
        fake = FakeGraph()
        deliver_one(self.db, self.settings, fake)
        self.assertEqual(fake.sent, [])
        self.assertEqual(get(self.db, cid)['status'], 'FAILED')

    def test_ingestion_cursor_retained_on_bad_email(self):
        class Delta:
            def delta(self, cursor, since):
                return {'value': [message(), {'id': 'bad'}], '@odata.deltaLink': 'next-cursor'}
        with self.assertRaises(ValueError):
            poll(self.db, Delta(), '2026-10-23T00:00:00Z')
        self.assertIsNone(self.db.execute("SELECT 1 FROM checkpoints WHERE name='graph_delta'").fetchone())
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            poll(self.db, Delta(), '2026-10-23T00:00:00Z')
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0], 1)

    def test_live_guard(self):
        with self.assertRaises(ValueError):
            Settings().require_live()
