import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from test_workflow import CaseFixture, message
from alumni.config import Settings
from alumni.db import connect, migrate
from alumni.workflow import ingest, get, configure_authority
from alumni.worker import deliver_one
from alumni.notifications import schedule, configure_policy


class AuditRegressions(CaseFixture,unittest.TestCase):
    def test_each_pilot_category_completes_only_after_explicit_approval(self):
        from alumni.classifier import PILOT_SCOPE
        configure_authority(self.db,'rita','rita@demo.invalid','admin')
        configure_authority(self.db,'hemant','hemant@demo.invalid','admin')
        for index,category in enumerate(PILOT_SCOPE):
            with self.subTest(category=category):
                subject=category.replace('_',' ')
                thread='category-thread-%d'%index
                cid,_=ingest(self.db,message('category-%d'%index,subject,'Please help',thread),self.settings)
                row=get(self.db,cid)
                self.assertEqual(row['status'],'AWAITING_APPROVAL')
                self.assertEqual(row['category'],category)
                self.assertEqual(row['authority_id'],'hemant' if category=='MIGRATION' else 'rita')
                self.assertFalse(self.db.execute("SELECT 1 FROM jobs WHERE concern_id=? AND kind='FORWARD'",(cid,)).fetchone())
                deliver_one(self.db,self.settings)  # acknowledgement only
                self.assertEqual(get(self.db,cid)['status'],'AWAITING_APPROVAL')
                self.act(cid,'approve');deliver_one(self.db,self.settings)
                self.assertEqual(get(self.db,cid)['status'],'FORWARDED')
                self.act(cid,'addressed',reason='Synthetic completion evidence');deliver_one(self.db,self.settings)
                self.assertEqual(get(self.db,cid)['status'],'AWAITING_CONFIRMATION')
                ingest(self.db,message('resolved-%d'%index,subject,'RESOLVED',thread),self.settings)
                self.assertEqual(get(self.db,cid)['status'],'CLOSED')
                self.assertIsNotNone(get(self.db,cid)['fulfilled_at'])
                self.assertEqual(self.db.execute("SELECT count(*) FROM audit WHERE concern_id=? AND event='APPROVED'",(cid,)).fetchone()[0],1)

    def test_all_disallowed_state_transitions_leave_no_mutation(self):
        from alumni.workflow import TRANSITIONS, transition
        cid=self.setup_case()
        for source,allowed in TRANSITIONS.items():
            for target in set(TRANSITIONS)-allowed:
                with self.subTest(source=source,target=target):
                    self.db.execute('UPDATE concerns SET status=? WHERE id=?',(source,cid))
                    version=get(self.db,cid)['version']
                    count=self.db.execute('SELECT count(*) FROM audit').fetchone()[0]
                    with self.assertRaisesRegex(ValueError,'Invalid transition'):
                        transition(self.db,cid,target,'test','Forbidden edge')
                    self.assertEqual(get(self.db,cid)['status'],source)
                    self.assertEqual(get(self.db,cid)['version'],version)
                    self.assertEqual(self.db.execute('SELECT count(*) FROM audit').fetchone()[0],count)

    def waiting(self):
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings)
        self.act(cid,'addressed');deliver_one(self.db,self.settings)
        return cid

    def test_automated_resolution_cannot_close_ticket(self):
        cid=self.waiting()
        payload=message('auto-reply','Re: Transcript','RESOLVED')
        payload['internetMessageHeaders']=[{'name':'Auto-Submitted','value':'auto-replied'}]
        ingest(self.db,payload,self.settings)
        self.assertEqual(get(self.db,cid)['status'],'NEEDS_REVIEW')
        self.assertIsNone(get(self.db,cid)['fulfilled_at'])

    def test_header_collision_is_visible_instead_of_dropping_another_email(self):
        ingest(self.db,message(),self.settings)
        collision=message();collision['id']='another-provider-message'
        with self.assertRaisesRegex(ValueError,'collision'):
            ingest(self.db,collision,self.settings)
        self.assertEqual(self.db.execute('SELECT count(*) FROM receipts').fetchone()[0],1)

    def test_replay_with_same_provider_id_and_changed_header_is_not_processed_again(self):
        cid=self.waiting()
        reply=message('reply','Re: Transcript','RESOLVED')
        ingest(self.db,reply,self.settings)
        reply['internetMessageId']='changed-header'
        self.assertEqual(ingest(self.db,reply,self.settings),(cid,False))
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0],1)

    def test_due_change_obsoletes_failed_reminder_draft(self):
        from alumni.notifications import set_due, utc
        from datetime import timedelta
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings)
        due=get(self.db,cid)['due_at'];schedule(self.db,self.settings,due)
        self.db.execute("UPDATE jobs SET state='FAILED',draft_id='old-due-draft' WHERE kind='REMINDER'")
        set_due(self.db,cid,(utc(due)+timedelta(days=2)).isoformat(),'admin',get(self.db,cid)['version'])
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='REMINDER'").fetchone()[0],'CANCELLED')
        with self.assertRaises(ValueError):self.act(cid,'retry_notice')

    def test_policy_contact_cannot_change_during_dispatch(self):
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings)
        schedule(self.db,self.settings,get(self.db,cid)['due_at'])
        self.db.execute("UPDATE jobs SET state='RUNNING' WHERE kind='REMINDER'")
        with self.assertRaisesRegex(ValueError,'delivery'):
            configure_policy(self.db,{'enabled':True,'due_hours':24,'interval_hours':24,'max_reminders':2,'escalation_delay_hours':24,'escalation_email':'new@example.org'},'admin')

    def test_concurrent_identical_intake_is_idempotent(self):
        path=self.path
        def receive(_):
            db=connect(path)
            try:return ingest(db,message(),self.settings)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(receive,range(12)))
        self.assertEqual(sum(created for _,created in results),1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='ACK'").fetchone()[0],1)

    def test_core_retry_ignores_a_newer_notification_failure(self):
        cid=self.setup_case();self.act(cid,'approve')
        self.db.execute("UPDATE jobs SET state='FAILED' WHERE kind='FORWARD'")
        self.db.execute("UPDATE concerns SET status='FAILED' WHERE id=?",(cid,))
        self.db.execute("INSERT INTO jobs(concern_id,kind,state,recipient,dry_run,created_at,updated_at) VALUES(?,'ESCALATION','FAILED','admin@example.org',1,'2026-10-24T00:00:00Z','2026-10-24T00:00:00Z')",(cid,))
        self.act(cid,'retry')
        self.assertEqual(get(self.db,cid)['status'],'AWAITING_APPROVAL')


class MigrationAudit(unittest.TestCase):
    def test_concurrent_migrations_are_serialized_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'concurrent.db')
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _:migrate(path),range(8)))
            db=connect(path)
            try:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],4)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            finally:db.close()

    def test_migration_failure_rolls_back_ddl_and_retains_data(self):
        from unittest.mock import patch
        import alumni.db as persistence
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'rollback.db')
            db=connect(path)
            for file in sorted((Path(__file__).resolve().parent.parent/'migrations').glob('*.sql'))[:3]:
                db.executescript(file.read_text())
            db.execute("INSERT INTO concerns(message_id,graph_id,thread_id,sender,subject,body,received_at,updated_at) VALUES('m','g','t','s@example.org','Transcript','body','2026-10-24T00:00:00Z','2026-10-24T00:00:00Z')")
            db.close()
            def broken(sql):
                yield 'ALTER TABLE receipts ADD COLUMN graph_id TEXT;'
                raise ValueError('injected migration interruption')
            with patch.object(persistence,'statements',side_effect=broken):
                with self.assertRaises(ValueError):migrate(path)
            db=connect(path)
            try:
                self.assertEqual(db.execute('SELECT count(*) FROM concerns').fetchone()[0],1)
                self.assertNotIn('graph_id',[r[1] for r in db.execute('PRAGMA table_info(receipts)')])
                self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],3)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:db.close()
            migrate(path)

    def test_database_file_is_private_and_readiness_requires_migrations(self):
        import os,stat
        from alumni.db import schema_ready
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'private.db')
            db=connect(path)
            self.assertFalse(schema_ready(db));db.close()
            migrate(path);db=connect(path)
            self.assertTrue(schema_ready(db));db.close()
            if os.name!='nt':self.assertEqual(stat.S_IMODE(os.stat(path).st_mode)&0o077,0)
