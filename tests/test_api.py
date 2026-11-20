import io
import json
import unittest
from types import SimpleNamespace
from test_workflow import CaseFixture, message
from alumni.api import App
from alumni.identity import AccessError, Admin
from alumni.workflow import get, ingest
from alumni.worker import deliver_one
from alumni.service import bind_mailbox


class ApiTests(CaseFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app=App(self.settings)
        # Test-only substitution. Production has no fake-token or cookie authentication mode.
        def auth(header):
            if header!='Bearer test-access':
                raise AccessError('Unauthorized')
            return Admin('test-tenant','test-admin')
        self.app.identity=SimpleNamespace(authenticate=auth,configured=True)

    def request(self,path,method='GET',data=None,query='',token='test-access',origin=None,content_type='application/json'):
        raw=json.dumps(data).encode() if data is not None else b''
        env={'PATH_INFO':path,'REQUEST_METHOD':method,'QUERY_STRING':query,'HTTP_AUTHORIZATION':'Bearer '+token,'CONTENT_TYPE':content_type,'CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        if origin:env['HTTP_ORIGIN']=origin
        result={}
        def start(status,headers):result.update(status=int(status.split()[0]),headers=dict(headers))
        body=b''.join(self.app(env,start))
        result['body']=json.loads(body) if result['headers']['Content-Type'].startswith('application/json') else body
        return result

    def test_selected_email_and_no_ingest_from_client(self):
        cid=self.setup_case()
        result=self.request('/api/context',query='message_id=m1')
        self.assertEqual(result['body']['id'],cid)
        self.assertEqual(result['body']['authority']['name'],'Rita Godiyal')
        self.assertEqual(self.request('/api/context',query='message_id=missing')['status'],404)
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0],1)

    def test_unauthorized_and_cross_origin(self):
        self.assertEqual(self.request('/api/routing',token='fake')['status'],401)
        self.assertEqual(self.request('/api/routing',origin='https://attacker.invalid')['status'],403)
        self.assertNotIn('Access-Control-Allow-Origin',self.request('/api/routing')['headers'])

    def test_approval_and_duplicate_request(self):
        cid=self.setup_case()
        payload={'action':'approve','version':get(self.db,cid)['version']}
        self.assertEqual(self.request(f'/api/concerns/{cid}/actions','POST',payload)['status'],200)
        self.assertEqual(self.request(f'/api/concerns/{cid}/actions','POST',payload)['status'],400)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs WHERE kind="FORWARD"').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT actor FROM audit WHERE event='APPROVED'").fetchone()[0],'entra:test-tenant:test-admin')
        deliver_one(self.db,self.settings)
        self.assertEqual(self.request(f'/api/concerns/{cid}')['body']['status'],'FORWARDED')

    def test_actor_spoof_and_invalid_json_types(self):
        cid=self.setup_case()
        for payload in [{'action':'approve','version':True},{'action':'approve','version':get(self.db,cid)['version'],'actor':'someone-else'},{'action':'route','version':get(self.db,cid)['version'],'reason':[]},[]]:
            self.assertEqual(self.request(f'/api/concerns/{cid}/actions','POST',payload)['status'],400)
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs WHERE kind="FORWARD"').fetchone()[0],0)

    def test_pending_and_history(self):
        cid=self.setup_case()
        self.assertEqual(self.request('/api/concerns')['body']['items'][0]['id'],cid)
        events=self.request(f'/api/concerns/{cid}/history')['body']['items']
        self.assertTrue(any(event['event']=='ADMIN_NOTIFICATION' and event['details']['channel']=='outlook_pending_approvals' for event in events))
        self.assertEqual(self.request('/api/concerns',query='status=invalid')['status'],400)

    def test_reply_context_resolves_original_ticket(self):
        cid=self.setup_case();self.act(cid,'approve');deliver_one(self.db,self.settings)
        self.act(cid,'addressed');deliver_one(self.db,self.settings)
        ingest(self.db,message('reply','Re: transcript','RESOLVED'))
        self.assertEqual(self.request('/api/context',query='message_id=reply')['body']['id'],cid)

    def test_routing_and_mode(self):
        result=self.request('/api/routing','POST',{'authority_id':'hemant','email':'migration@example.org'})
        self.assertEqual(result['status'],200)
        self.assertEqual(self.request('/api/health')['body']['dry_run'],True)
        self.assertEqual(self.request('/login')['status'],404)
        self.assertIn('no standalone',self.request('/')['body']['interface'])

    def test_database_cannot_switch_mailboxes(self):
        bind_mailbox(self.db,'alumni@upes.ac.in')
        with self.assertRaises(ValueError):bind_mailbox(self.db,'other@example.org')

    def test_live_requires_verified_mailbox_type(self):
        self.settings.live_authorized=True
        with self.assertRaisesRegex(ValueError,'verify MAILBOX_TYPE'):
            self.settings.require_live()

    def test_readiness_distinguishes_liveness_from_missing_migration(self):
        self.assertEqual(self.request('/readyz',token='')['status'],200)
        self.db.execute('DELETE FROM schema_migrations WHERE version=4')
        self.assertEqual(self.request('/healthz',token='')['status'],200)
        response=self.request('/readyz',token='')
        self.assertEqual(response['status'],503)
        self.assertIn('migrations',response['body']['error'])
