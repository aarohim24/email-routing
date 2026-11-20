import tempfile
import unittest
from pathlib import Path
from alumni.db import migrate, connect
from alumni.config import Settings
from alumni.classifier import classify, plain
from alumni.workflow import ingest, get, action, configure_authority


def message(mid='m1', subject='Transcript request', body='Please help', thread='t1'):
    return {'id': 'g'+mid, 'internetMessageId': mid, 'conversationId': thread, 'from': {'emailAddress': {'address': 'student@example.org'}}, 'subject': subject, 'body': {'contentType': 'text', 'content': body}, 'receivedDateTime': '2026-10-23T10:00:00Z'}


class CaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'test.db')
        migrate(self.path)
        self.db = connect(self.path)
        self.settings = Settings(database=self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def setup_case(self):
        configure_authority(self.db, 'rita', 'registrar@example.org', 'admin')
        cid=ingest(self.db, message(),self.settings)[0]
        # Fixture consumes only its acknowledgement through a fake external adapter.
        from alumni.worker import deliver_one
        class Mail:
            def draft(self,*args):return 'fixture-ack'
            def send(self,*args):pass
        deliver_one(self.db,self.settings,Mail())
        return cid

    def act(self, cid, command, **kw):
        action(self.db, self.settings, cid, command, 'admin', get(self.db, cid)['version'], **kw)


class WorkflowTests(CaseFixture, unittest.TestCase):
    def test_classification(self):
        for text, expected in [('transcript', 'TRANSCRIPT'), ('provisional degree','PROVISIONAL_DEGREE'), ('degree verification','DEGREE_VERIFICATION'), ('medium of instruction','MEDIUM_OF_INSTRUCTION'), ('collect my degree','DEGREE_COLLECTION'), ('migration certificate','MIGRATION'), ('help','UNKNOWN'), ('transcript and migration','UNKNOWN'), ('I do not need a transcript','UNKNOWN')]:
            self.assertEqual(classify(text, '').category, expected)
        self.assertNotIn('alert', plain('<script>alert(1)</script><p>hello</p>', 'html'))

    def test_missing_route_and_duplicate(self):
        cid, created = ingest(self.db, message())
        self.assertTrue(created)
        self.assertEqual(get(self.db, cid)['status'], 'NEEDS_REVIEW')
        self.assertEqual(ingest(self.db, message()), (cid, False))
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0], 1)

    def test_gate_and_audit(self):
        cid = self.setup_case()
        self.assertEqual(get(self.db, cid)['status'], 'AWAITING_APPROVAL')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs WHERE kind="FORWARD"').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.act(cid, 'addressed')
        self.act(cid, 'approve')
        self.assertEqual(self.db.execute('SELECT recipient FROM jobs WHERE kind="FORWARD"').fetchone()[0], 'registrar@example.org')
        with self.assertRaises(ValueError):
            self.act(cid, 'approve')
        self.assertTrue(self.db.execute("SELECT 1 FROM audit WHERE event='APPROVED'").fetchone())

    def test_rejection_and_modified_routing(self):
        cid = self.setup_case()
        self.act(cid, 'reject', reason='Wrong category')
        self.assertEqual(get(self.db, cid)['status'], 'NEEDS_REVIEW')
        self.act(cid, 'route', category='MIGRATION', reason='Student clarification')
        self.assertEqual(get(self.db, cid)['status'], 'NEEDS_REVIEW')
        configure_authority(self.db, 'hemant', 'migration@example.org', 'admin')
        self.act(cid, 'route', category='MIGRATION', reason='Address now configured')
        self.act(cid, 'approve')
        self.assertEqual(self.db.execute('SELECT recipient FROM jobs WHERE kind="FORWARD"').fetchone()[0], 'migration@example.org')

    def test_stale_action(self):
        cid = self.setup_case()
        with self.assertRaises(ValueError):
            action(self.db, self.settings, cid, 'approve', 'admin', -1)


if __name__ == '__main__':
    unittest.main()
