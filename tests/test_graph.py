import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from alumni.config import Settings
from alumni.graph import Graph, GraphError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph(Settings(live_authorized=True, mailbox_type='standalone', mailbox_verified=True, tenant_id='test-tenant', client_id='test-client', client_secret='not-a-real-secret'))
        self.graph.token = 'test-token'
        self.graph.expires = 10**12

    def test_retries_bounded_reads(self):
        with patch('alumni.graph.open_url', side_effect=urllib.error.URLError('offline')) as network, patch('time.sleep') as sleep:
            with self.assertRaises(GraphError):
                self.graph.request('GET', 'users/test/messages')
            self.assertEqual(network.call_count, 4)
            self.assertEqual(sleep.call_count, 3)

    def test_does_not_retry_ambiguous_send(self):
        with patch('alumni.graph.open_url', side_effect=urllib.error.URLError('offline')) as network, patch('time.sleep') as sleep:
            with self.assertRaises(GraphError):
                self.graph.send('draft')
            self.assertEqual(network.call_count, 1)
            sleep.assert_not_called()

    def test_throttle_backoff(self):
        error = urllib.error.HTTPError('https://graph.microsoft.com',429,'throttled',{'Retry-After':'2'},None)
        with patch('alumni.graph.open_url', side_effect=[error, Response(b'{}')]) as network, patch('time.sleep') as sleep:
            self.graph.send('draft')
            self.assertEqual(network.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_rejects_foreign_cursor_before_auth(self):
        with patch('alumni.graph.open_url') as network:
            with self.assertRaises(ValueError):
                self.graph.delta('https://attacker.invalid/steal')
            network.assert_not_called()

    def test_forward_preserves_context_and_explicit_recipient(self):
        calls = []
        def request(method, path, payload):
            calls.append((method, path, payload))
            return {'id': 'draft'}
        with patch.object(self.graph, 'request', side_effect=request):
            self.graph.draft({'kind':'FORWARD','recipient':'authority@example.org'}, {'graph_id':'original/id','id':1,'sender':'student@example.org'})
        method, path, payload = calls[0]
        self.assertIn('original%2Fid/createForward', path)
        self.assertEqual(payload['message']['toRecipients'][0]['emailAddress']['address'], 'authority@example.org')
        self.assertEqual(payload['message']['replyTo'][0]['emailAddress']['address'], 'student@example.org')
        self.assertEqual(payload['message']['ccRecipients'], [])
        self.assertEqual(payload['message']['bccRecipients'], [])

    def test_acknowledgement_is_a_reply_with_reference_not_fulfilment(self):
        calls=[]
        def request(method,path,payload=None):
            calls.append((method,path,payload));return {'id':'ack-draft'}
        with patch.object(self.graph,'request',side_effect=request):
            result=self.graph.draft({'kind':'ACK','recipient':'student@example.org'},{'id':42,'graph_id':'source'})
        self.assertEqual(result,'ack-draft')
        self.assertIn('/createReply',calls[0][1])
        self.assertIn('#42',calls[0][2]['comment'])
        self.assertIn('does not approve or issue',calls[0][2]['comment'])
        self.assertEqual(calls[1][2]['toRecipients'][0]['emailAddress']['address'],'student@example.org')

    def test_reminder_has_no_original_body_or_attachments(self):
        with patch.object(self.graph,'request',return_value={'id':'notice'}) as request:
            self.graph.draft({'kind':'REMINDER','recipient':'authority@example.org'},{'id':42,'graph_id':'source','due_at':'2026-10-24T00:00:00Z','body':'PRIVATE ORIGINAL CONTENT'})
        payload=request.call_args.args[2]
        self.assertEqual(payload['body']['contentType'],'text')
        self.assertNotIn('PRIVATE ORIGINAL CONTENT',json.dumps(payload))
        self.assertNotIn('attachments',payload)
        self.assertNotIn('createForward',request.call_args.args[1])

    def test_redirect_cannot_relay_a_bearer_token(self):
        from alumni.graph import NoRedirect
        import urllib.request
        request=urllib.request.Request('https://graph.microsoft.com/v1.0/users/test/messages',headers={'Authorization':'Bearer private-token'})
        with self.assertRaises(urllib.error.HTTPError):
            NoRedirect().redirect_request(request,None,302,'redirect',{},'https://attacker.invalid/')

    def test_oauth_redirect_is_refused_without_secret_retry(self):
        self.graph.token=None
        error=urllib.error.HTTPError('https://login.microsoftonline.com',302,'redirect',{},None)
        with patch('alumni.graph.open_url',side_effect=error) as network:
            with self.assertRaises(GraphError):self.graph.access_token()
            self.assertEqual(network.call_count,1)
