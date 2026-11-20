"""Microsoft Graph adapter. OAuth client credentials; no mailbox passwords."""
import json
import random
import re
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not relay Authorization headers or OAuth client-secret form bodies.
        raise urllib.error.HTTPError(req.full_url,code,'Redirect refused',headers,fp)


open_url = urllib.request.build_opener(NoRedirect()).open


class GraphError(Exception):
    def __init__(self, status, code='graph_error'):
        self.status = status
        super().__init__('Microsoft Graph %s (%s)' % (status, code))


class Graph:
    origin = 'https://graph.microsoft.com/v1.0/'

    def __init__(self, settings):
        settings.require_live()
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', settings.tenant_id):
            raise ValueError('Invalid tenant ID')
        self.settings = settings
        self.token = None
        self.expires = 0
        self.user = 'users/' + urllib.parse.quote(settings.mailbox, safe='')

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        form = urllib.parse.urlencode({'client_id': self.settings.client_id, 'client_secret': self.settings.client_secret,
                                      'grant_type': 'client_credentials', 'scope': 'https://graph.microsoft.com/.default'}).encode()
        request = urllib.request.Request('https://login.microsoftonline.com/' + self.settings.tenant_id + '/oauth2/v2.0/token', data=form)
        try:
            with open_url(request, timeout=30) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise GraphError(error.code, 'oauth_failed') from None
        self.token = data['access_token']
        self.expires = time.time() + max(0, int(data['expires_in']) - 120)
        return self.token

    def request(self, method, path, payload=None):
        url = path if path.startswith('https://') else self.origin + path
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or not parsed.path.startswith('/v1.0/'):
            raise ValueError('Refusing untrusted Graph pagination URL')
        for attempt in range(4):
            request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else (b'' if method == 'POST' else None), method=method,
                headers={'Authorization': 'Bearer ' + self.access_token(), 'Content-Type': 'application/json',
                         'Prefer': 'IdType="ImmutableId", outlook.body-content-type="text"'})
            try:
                with open_url(request, timeout=30) as response:
                    data = response.read()
                    return json.loads(data) if data else {}
            except urllib.error.HTTPError as error:
                # Only safe reads and explicit throttling responses retry. Never retry an ambiguous send.
                retryable = error.code == 429 or (method == 'GET' and error.code in (500, 502, 503, 504))
                if retryable and attempt < 3:
                    delay = error.headers.get('Retry-After', '')
                    time.sleep(min(30, float(delay) if delay.isdigit() else 2 ** attempt + random.random()))
                    continue
                if error.code == 401:
                    self.token = None
                raise GraphError(error.code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == 'GET' and attempt < 3:
                    time.sleep(2 ** attempt + random.random())
                    continue
                raise GraphError(0, 'connection_failed') from None

    def delta(self, cursor=None, since=None):
        if not cursor and not since:
            raise ValueError('MS_INGEST_SINCE is required for the first mailbox synchronization')
        if since:
            parsed_since = datetime.fromisoformat(since.replace('Z', '+00:00'))
            if parsed_since.tzinfo is None:
                raise ValueError('MS_INGEST_SINCE must include a timezone')
        # Avoid Graph's 5,000-item cap on filtered delta queries. Apply cutoff locally.
        query = urllib.parse.urlencode({'$select': 'id,internetMessageId,conversationId,from,subject,body,receivedDateTime,internetMessageHeaders', 'changeType': 'created'}) if not cursor else ''
        return self.request('GET', cursor or self.user + '/mailFolders/inbox/messages/delta?' + query)

    def draft(self, job, concern):
        path = self.user + '/messages/' + urllib.parse.quote(concern['graph_id'], safe='')
        recipient = [{'emailAddress': {'address': job['recipient']}}]
        if job['kind'] == 'FORWARD':
            return self.request('POST', path + '/createForward', {
                'message': {'toRecipients': recipient, 'ccRecipients': [], 'bccRecipients': [], 'replyTo': [{'emailAddress': {'address': concern['sender']}}]},
                'comment': 'UPES alumni concern #%d. Approved routing. Original sender: %s' % (concern['id'], concern['sender'])})['id']
        if job['kind'] in ('REMINDER','ESCALATION'):
            text = 'UPES concern #%d remains awaiting authority completion. Due: %s. Open UPES Alumni Routing in Outlook, select Assigned to me, and record completion evidence when addressed.' % (concern['id'],concern['due_at'])
            return self.request('POST',self.user+'/messages',{'subject':'UPES concern #%d — %s' % (concern['id'],job['kind'].lower()),'body':{'contentType':'text','content':text},'toRecipients':recipient,'ccRecipients':[],'bccRecipients':[]})['id']
        if job['kind'] not in ('ACK','FOLLOWUP'):
            raise ValueError('Unsupported delivery kind')
        comment = ('We have received your request. Your UPES reference is #%d. Alumni staff will review the concern and its routing. This acknowledgement does not approve or issue any official document.' % concern['id']) if job['kind']=='ACK' else ('UPES concern #%d has been marked addressed. Has your concern been resolved?\n\nReply with exactly RESOLVED or NOT RESOLVED (remove quoted text). Other replies will be reviewed by an administrator.' % concern['id'])
        # Reply draft keeps the institution's existing conversation thread.
        draft = self.request('POST', path + '/createReply', {'comment':comment})
        self.request('PATCH', self.user + '/messages/' + urllib.parse.quote(draft['id'], safe=''), {'toRecipients': recipient, 'ccRecipients': [], 'bccRecipients': []})
        return draft['id']

    def send(self, draft_id):
        self.request('POST', self.user + '/messages/' + urllib.parse.quote(draft_id, safe='') + '/send')

    def sent_message_id(self, draft_id):
        message = self.request('GET',self.user+'/messages/'+urllib.parse.quote(draft_id,safe='')+'?$select=internetMessageId,isDraft')
        return message.get('internetMessageId') if message.get('isDraft') is False else None
