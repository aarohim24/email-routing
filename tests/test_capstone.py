import unittest
from datetime import timedelta
from unittest.mock import patch
from test_workflow import CaseFixture, message
from alumni.workflow import ingest, get, configure_authority, action
from alumni.worker import deliver_one, recover, reconcile
from alumni.graph import GraphError
from alumni.notifications import schedule, utc, set_due, configure_policy
from alumni.extraction import extract
from alumni.pilot import metrics, label, evaluate
from alumni.classifier import RuleBasedClassifier


class CapstoneTests(CaseFixture,unittest.TestCase):
    def forwarded(self):
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings)
        return cid

    def test_explicit_nullable_extraction(self):
        values=extract('Name: Somya Mathur\nEnrolment Number: 500123456\nAlumni ID: UPES/2020/123')
        self.assertEqual(values,{'alumnus_name':'Somya Mathur','enrolment_number':'500123456','alumni_number':'UPES/2020/123'})
        self.assertTrue(all(v is None for v in extract('Hi, I am Somya, student@example.org').values()))
        self.assertIsNone(extract('Name: Somya\nName: Rita')['alumnus_name'])
        self.assertTrue(all(v is None for v in extract('Name: N/A\nEnrolment Number: N/A\nAlumni ID: unknown').values()))
        cid,_=ingest(self.db,message(body='Name: Somya\nEnrollment No.: 500123456'))
        row=get(self.db,cid)
        self.assertEqual(row['alumnus_name'],'Somya');self.assertEqual(row['enrolment_number'],'500123456')
        self.assertIsNone(row['alumni_number']);self.assertEqual(row['requested_document'],'TRANSCRIPT')

    def test_ack_atomic_deduplicated_and_lifecycle_neutral(self):
        cid,_=ingest(self.db,message())
        self.assertEqual(ingest(self.db,message()),(cid,False))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='ACK'").fetchone()[0],1)
        before=get(self.db,cid)['status'];deliver_one(self.db,self.settings)
        self.assertEqual(get(self.db,cid)['status'],before)
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='ACK'").fetchone()[0],'SUCCEEDED')
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0],0)

    def test_ack_suppression_for_auto_mail(self):
        payload=message();payload['internetMessageHeaders']=[{'name':'Auto-Submitted','value':'auto-replied'}]
        ingest(self.db,payload)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertTrue(self.db.execute("SELECT 1 FROM audit WHERE event='ACK_SUPPRESSED'").fetchone())

    def test_ack_uncertainty_does_not_break_concern(self):
        self.settings.dry_run=False
        cid,_=ingest(self.db,message(),self.settings)
        class Mail:
            def draft(self,*args):return 'ack-draft'
            def send(self,*args):raise GraphError(0)
        before=get(self.db,cid)['status'];deliver_one(self.db,self.settings,Mail())
        job=self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(job['state'],'UNCERTAIN');self.assertEqual(get(self.db,cid)['status'],before)
        self.assertFalse(deliver_one(self.db,self.settings,Mail()))
        reconcile(self.db,job['id'],True,'admin','Sent Items confirms acknowledgement')
        self.assertEqual(get(self.db,cid)['status'],before)

    def test_ack_restart_is_quarantined_without_state_change(self):
        cid,_=ingest(self.db,message());before=get(self.db,cid)['status']
        self.db.execute("UPDATE jobs SET state='RUNNING'");recover(self.db)
        self.assertEqual(get(self.db,cid)['status'],before)
        self.assertEqual(self.db.execute('SELECT state FROM jobs').fetchone()[0],'UNCERTAIN')

    def test_bounded_reminders_and_configured_escalation(self):
        cid=self.forwarded();due=utc(get(self.db,cid)['due_at'])
        self.assertEqual(schedule(self.db,self.settings,(due-timedelta(seconds=1)).isoformat()),0)
        self.assertEqual(schedule(self.db,self.settings,due.isoformat()),1)
        self.assertEqual(schedule(self.db,self.settings,due.isoformat()),0)
        deliver_one(self.db,self.settings)
        self.assertEqual(get(self.db,cid)['status'],'FORWARDED')
        configure_policy(self.db,{'enabled':True,'due_hours':24,'interval_hours':24,'max_reminders':2,'escalation_delay_hours':24,'escalation_email':'admin@example.org'},'admin')
        self.assertEqual(schedule(self.db,self.settings,(due+timedelta(hours=24)).isoformat()),2)
        self.assertEqual(schedule(self.db,self.settings,(due+timedelta(hours=240)).isoformat()),0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='REMINDER'").fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='ESCALATION'").fetchone()[0],1)

    def test_missing_escalation_never_guesses_recipient(self):
        cid=self.forwarded();due=utc(get(self.db,cid)['due_at'])
        schedule(self.db,self.settings,(due+timedelta(hours=48)).isoformat())
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='ESCALATION'").fetchone()[0],0)
        self.assertTrue(self.db.execute("SELECT 1 FROM audit WHERE event='ESCALATION_BLOCKED'").fetchone())

    def test_reminder_cancellation_and_new_cycle(self):
        cid=self.forwarded();due=get(self.db,cid)['due_at'];schedule(self.db,self.settings,due)
        self.act(cid,'addressed')
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='REMINDER'").fetchone()[0],'CANCELLED')
        deliver_one(self.db,self.settings)
        ingest(self.db,message('reply','Re: Transcript','NOT RESOLVED'),self.settings)
        self.assertEqual(get(self.db,cid)['unresolved_count'],1)
        self.act(cid,'approve');deliver_one(self.db,self.settings)
        self.assertEqual(get(self.db,cid)['routing_cycle'],2)
        self.assertEqual(schedule(self.db,self.settings,get(self.db,cid)['due_at']),1)

    def test_due_date_edit_and_optimistic_lock(self):
        cid=self.forwarded();row=get(self.db,cid)
        due=(utc(row['due_at'])+timedelta(hours=48)).isoformat()
        set_due(self.db,cid,due,'admin',row['version'])
        self.assertEqual(get(self.db,cid)['due_at'],due)
        with self.assertRaises(ValueError):set_due(self.db,cid,due,'admin',row['version'])
        with self.assertRaises(ValueError):set_due(self.db,cid,'2026-10-24T12:00:00','admin',get(self.db,cid)['version'])

    def test_mode_change_never_sends_staging_reminders(self):
        cid=self.forwarded();self.settings.dry_run=False
        self.assertEqual(schedule(self.db,self.settings,get(self.db,cid)['due_at']),0)

    def test_no_automatic_scope_expansion(self):
        cid,_=ingest(self.db,message(subject='Grade card request',body='Please provide my grade card'))
        self.assertEqual(get(self.db,cid)['status'],'NEEDS_REVIEW')
        self.assertIsNone(get(self.db,cid)['requested_document'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0],0)

    def test_labels_metrics_and_empty_denominators(self):
        self.assertIsNone(metrics(self.db)['average_turnaround_hours'])
        self.assertIsNone(metrics(self.db)['routing_accuracy_percent'])
        cid=self.forwarded()
        label(self.db,cid,'TRANSCRIPT','rita','Verified request and approved matrix','admin',get(self.db,cid)['version'])
        due=get(self.db,cid)['due_at'];schedule(self.db,self.settings,due);deliver_one(self.db,self.settings)
        data=metrics(self.db,'simulated',at=(utc(due)+timedelta(seconds=1)).isoformat())
        self.assertEqual(data['routing_accuracy_percent'],100)
        self.assertEqual(data['routing_evaluated_concerns'],1)
        self.assertEqual(data['overdue_authority_concerns'],1)
        self.assertEqual(data['reminders_accepted'],1)
        self.assertEqual(metrics(self.db)['total_concerns'],0) # simulations excluded by default
        self.act(cid,'addressed');deliver_one(self.db,self.settings)
        with patch('alumni.workflow.now',return_value='2026-10-25T10:00:00+00:00'):
            ingest(self.db,message('reply','Re: Transcript','RESOLVED'),self.settings)
        data=metrics(self.db,'simulated')
        self.assertEqual(data['average_turnaround_hours'],48)
        self.assertEqual(data['median_turnaround_hours'],48)
        self.assertEqual(data['closure_rate_percent'],100)
        self.assertEqual(data['turnaround_sample_size'],1)
        self.act(cid,'reopen',reason='Student reports another problem')
        data=metrics(self.db,'simulated')
        self.assertEqual(data['reopen_rate_percent'],100)
        self.assertIsNone(data['average_turnaround_hours'])

    def test_wrong_route_labels_are_not_self_scored_as_correct(self):
        cid=self.forwarded()
        label(self.db,cid,'MIGRATION','hemant','Independent reviewer corrected concern','admin',get(self.db,cid)['version'])
        self.assertEqual(metrics(self.db,'simulated')['routing_accuracy_percent'],0)
        self.assertEqual(metrics(self.db,'simulated')['classification_accuracy_percent'],0)

    def test_administrative_closure_is_not_verified_fulfilment(self):
        cid=self.setup_case();self.act(cid,'close',reason='Duplicate request')
        data=metrics(self.db,'simulated')
        self.assertEqual(data['closure_rate_percent'],100)
        self.assertEqual(data['turnaround_sample_size'],0)
        self.assertIsNone(get(self.db,cid)['fulfilled_at'])

    def test_candidate_evaluation_does_not_activate_or_change_routes(self):
        data=evaluate(RuleBasedClassifier(),[{'subject':'Transcript','body':'','expected_category':'TRANSCRIPT'},{'subject':'Grade card','body':'','expected_category':'UNKNOWN'}])
        self.assertEqual(data['accuracy_percent'],100)
        self.assertFalse(data['activated'])

    def test_policy_validation(self):
        with self.assertRaises(ValueError):configure_policy(self.db,{'enabled':True},'admin')
        self.assertIsNone(self.db.execute("SELECT department FROM authorities WHERE id='rita'").fetchone()[0])

    def test_mixed_out_of_scope_request_stays_in_review(self):
        cid,_=ingest(self.db,message(subject='Transcript and grade card',body='Please send both'))
        self.assertEqual(get(self.db,cid)['status'],'NEEDS_REVIEW')

    def test_disabled_policy_cancels_previously_pending_notices(self):
        cid=self.forwarded();schedule(self.db,self.settings,get(self.db,cid)['due_at'])
        configure_policy(self.db,{'enabled':False,'due_hours':24,'interval_hours':24,'max_reminders':2,'escalation_delay_hours':24,'escalation_email':None},'admin')
        deliver_one(self.db,self.settings)
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='REMINDER'").fetchone()[0],'CANCELLED')

    def test_scope_and_metrics_bounds(self):
        self.setup_case()
        self.assertEqual(metrics(self.db,'simulated',start='2026-10-24T00:00:00Z')['total_concerns'],0)
        with self.assertRaises(ValueError):metrics(self.db,'simulated',start='2026-10-25T00:00:00Z',end='2026-10-23T00:00:00Z')

    def test_metadata_lookup_does_not_resend(self):
        from alumni.worker import sync_outbound
        cid=self.forwarded()
        job=self.db.execute("SELECT * FROM jobs WHERE kind='FORWARD'").fetchone()
        self.db.execute('UPDATE jobs SET dry_run=0,draft_id=? WHERE id=?',('forward-draft',job['id']))
        class Metadata:
            def sent_message_id(self,draft):return '<outbound@example.org>'
        sync_outbound(self.db,Metadata());sync_outbound(self.db,Metadata())
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbound_messages').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='FORWARD'").fetchone()[0],'SUCCEEDED')

    def test_failed_notification_retry_has_no_forward_or_state_side_effect(self):
        self.settings.dry_run=False
        cid,_=ingest(self.db,message(),self.settings)
        class Mail:
            fail=True
            def draft(self,*args):return 'ack-draft'
            def send(self,*args):
                if self.fail:raise GraphError(403)
        mail=Mail();deliver_one(self.db,self.settings,mail)
        before=get(self.db,cid)['status']
        self.act(cid,'retry_notice');mail.fail=False;deliver_one(self.db,self.settings,mail)
        self.assertEqual(get(self.db,cid)['status'],before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='ACK'").fetchone()[0],'SUCCEEDED')

    def test_intake_rollback_cannot_leave_a_false_acknowledgement(self):
        class Broken:
            version='broken-candidate'
            def classify(self,*args):raise ValueError('Classifier failed')
        with self.assertRaises(ValueError):ingest(self.db,message(),self.settings,Broken())
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM receipts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0],0)

    def test_missing_notification_addresses_are_audited_once_not_every_tick(self):
        cid=self.forwarded()
        configure_authority(self.db,'rita','','admin')
        at=(utc(get(self.db,cid)['due_at'])+timedelta(hours=48)).isoformat()
        for _ in range(5):schedule(self.db,self.settings,at)
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit WHERE event='REMINDER_BLOCKED'").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit WHERE event='ESCALATION_BLOCKED'").fetchone()[0],1)

    def test_fresh_approval_cannot_promote_a_simulated_ticket_into_live_mail(self):
        cid=self.setup_case();self.settings.dry_run=False
        with self.assertRaisesRegex(ValueError,'intake mode differs'):
            self.act(cid,'approve')
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0],0)

    def test_removed_authority_contact_cannot_receive_an_already_queued_reminder(self):
        cid=self.forwarded();schedule(self.db,self.settings,get(self.db,cid)['due_at'])
        configure_authority(self.db,'rita','','admin')
        deliver_one(self.db,self.settings)
        self.assertEqual(self.db.execute("SELECT state FROM jobs WHERE kind='REMINDER'").fetchone()[0],'CANCELLED')
