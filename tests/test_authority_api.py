import unittest
from types import SimpleNamespace
import test_api
from test_workflow import CaseFixture
from alumni.identity import Admin, AccessError
from alumni.workflow import configure_authority, get
from alumni.worker import deliver_one

AUTHORITY='55555555-5555-4555-8555-555555555555'
OTHER='66666666-6666-4666-8666-666666666666'


class AuthorityApiTests(CaseFixture,unittest.TestCase):
    request=test_api.ApiTests.request

    def setUp(self):
        super().setUp()
        from alumni.api import App
        self.app=App(self.settings)
        def auth(header):
            if header=='Bearer test-access':return Admin('test-tenant','test-admin',True)
            if header=='Bearer authority':return Admin('test-tenant',AUTHORITY,False)
            if header=='Bearer other':return Admin('test-tenant',OTHER,False)
            raise AccessError('Unauthorized')
        self.app.identity=SimpleNamespace(authenticate=auth,configured=True)
        configure_authority(self.db,'rita','registrar@example.org','admin',object_ids=[AUTHORITY])

    def forwarded(self):
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings);return cid

    def test_assignment_only_completion_and_admin_visibility(self):
        cid=self.forwarded()
        result=self.request(f'/api/concerns/{cid}',token='authority')
        self.assertEqual(result['body']['actions'],['addressed'])
        result=self.request(f'/api/concerns/{cid}/actions','POST',{'action':'addressed','version':get(self.db,cid)['version'],'reason':'Document delivered, department reference 123'},token='authority')
        self.assertEqual(result['status'],200)
        self.assertEqual(self.request(f'/api/concerns/{cid}')['body']['status'],'ADDRESSED')
        self.assertTrue(get(self.db,cid)['addressed_at'])
        self.assertTrue(self.db.execute("SELECT 1 FROM jobs WHERE kind='FOLLOWUP' AND state='PENDING'").fetchone())
        self.assertGreater(self.request('/api/changes')['body']['revision'],0)

    def test_authority_cannot_approve_reroute_or_read_unassigned(self):
        cid=self.forwarded()
        for action in ('approve','route','close','reopen','reconcile'):
            result=self.request(f'/api/concerns/{cid}/actions','POST',{'action':action,'version':get(self.db,cid)['version'],'reason':'Attempt'},token='authority')
            self.assertEqual(result['status'],403)
        for path in ('/api/routing','/api/metrics','/api/health'):
            self.assertEqual(self.request(path,token='authority')['status'],403)
        self.assertEqual(self.request(f'/api/concerns/{cid}',token='other')['status'],403)
        configure_authority(self.db,'hemant','migration@example.org','admin',object_ids=[OTHER])
        self.assertEqual(self.request(f'/api/concerns/{cid}',token='other')['status'],403)
        self.assertEqual(self.request('/api/concerns',token='authority',query='status=AWAITING_APPROVAL')['status'],403)

    def test_completion_requires_evidence_and_current_assignment(self):
        cid=self.forwarded()
        result=self.request(f'/api/concerns/{cid}/actions','POST',{'action':'addressed','version':get(self.db,cid)['version']},token='authority')
        self.assertEqual(result['status'],400)
        configure_authority(self.db,'rita','registrar@example.org','admin',object_ids=[])
        self.assertEqual(self.request(f'/api/concerns/{cid}',token='authority')['status'],403)

    def test_forwarded_email_context_and_assigned_queue(self):
        cid=self.forwarded()
        job=self.db.execute("SELECT id FROM jobs WHERE kind='FORWARD'").fetchone()[0]
        self.db.execute("INSERT INTO outbound_messages VALUES('<forward@example.org>',?,?)",(cid,job))
        result=self.request('/api/context',query='message_id=%3Cforward%40example.org%3E',token='authority')
        self.assertEqual(result['body']['id'],cid)
        items=self.request('/api/concerns',token='authority')['body']['items']
        self.assertEqual([item['id'] for item in items],[cid])
        self.assertFalse(self.request('/api/me',token='authority')['body']['is_admin'])

    def test_metrics_due_and_labels_are_admin_only(self):
        cid=self.forwarded()
        data=self.request('/api/metrics',query='mode=simulated')['body']
        self.assertEqual(data['pending_concerns'],1)
        payload={'version':get(self.db,cid)['version'],'due_at':'2026-12-01T00:00:00Z'}
        self.assertEqual(self.request(f'/api/concerns/{cid}/due','POST',payload,token='authority')['status'],403)
        self.assertEqual(self.request(f'/api/concerns/{cid}/due','POST',payload)['status'],200)
        payload={'version':get(self.db,cid)['version'],'category':'TRANSCRIPT','authority_id':'rita','reason':'Reviewed original email'}
        self.assertEqual(self.request(f'/api/concerns/{cid}/label','POST',payload)['status'],200)
