"""Outlook add-in host + same-origin, bearer-only staff API. No password dashboard."""
import json
import logging
import mimetypes
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .db import connect, schema_ready
from .identity import EntraIdentity, AccessError
from .service import concern, history, routing, bind_mailbox
from .workflow import action, configure_authority
from .worker import reconcile
from .notifications import configure_policy, set_due
from .pilot import metrics, label

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / 'static'
ASSETS = {'taskpane.html', 'taskpane.js', 'taskpane.css', 'auth.html', 'icon-16.png', 'icon-32.png', 'icon-80.png', 'icon-128.png'}


class HttpError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def integer(value, name='version'):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + ' must be a nonnegative integer')
    return value


class App:
    def __init__(self, settings):
        self.settings = settings
        self.identity = EntraIdentity(settings)
        parsed = urlsplit(settings.base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('BASE_URL must be an HTTPS origin (including localhost for Outlook)')
        self.origin = parsed.scheme + '://' + parsed.netloc

    def __call__(self, env, start_response):
        status = 200
        result = None
        binary = None
        mime = 'application/json; charset=utf-8'
        headers = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'same-origin')]
        db = None
        path = env.get('PATH_INFO', '/')
        try:
            method = env['REQUEST_METHOD']
            if path.startswith('/addin/') and path[7:] in ASSETS and method == 'GET':
                file = STATIC / path[7:]
                if not file.is_file():
                    raise HttpError(503, 'Add-in assets missing. Run npm ci and npm run build in addin/.')
                binary = file.read_bytes()
                mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                # Task panes run inside Outlook. DENY/frame-ancestors none would break them.
                headers.append(('Content-Security-Policy', "default-src 'none'; script-src 'self' https://appsforoffice.microsoft.com; style-src 'self'; img-src 'self' data:; connect-src 'self' https://login.microsoftonline.com; frame-src 'self' https://login.microsoftonline.com; frame-ancestors 'self' https://outlook.office.com https://outlook.office365.com https://outlook.cloud.microsoft https://*.officeapps.live.com; base-uri 'none'; form-action 'self' https://login.microsoftonline.com"))
            elif path == '/addin/config' and method == 'GET':
                result = {'configured': self.identity.configured, 'clientId': self.settings.addin_client_id,
                          'tenantId': self.settings.tenant_id, 'scope': 'api://' + self.settings.api_audience + '/access_as_user',
                          'mailbox': self.settings.mailbox, 'mailboxType': self.settings.mailbox_type,
                          'mailboxVerified': self.settings.mailbox_verified, 'dryRun': self.settings.dry_run}
            elif path == '/readyz' and method == 'GET':
                db = connect(self.settings.database)
                if not schema_ready(db):
                    raise HttpError(503,'Database migrations are incomplete. Run migrations before serving staff traffic.')
                result = {'ready': True, 'scope':'API and database schema; inspect worker/Graph health separately'}
            elif path == '/healthz' and method == 'GET':
                db = connect(self.settings.database)
                db.execute('SELECT 1')
                result = {'ok': True}
            elif path.startswith('/api/'):
                if env.get('HTTP_ORIGIN') and env['HTTP_ORIGIN'] != self.origin:
                    raise HttpError(403, 'Request origin is not allowed')
                # Cookies, Outlook profile emails, ID tokens and client-submitted actor names cannot authorize.
                admin = self.identity.authenticate(env.get('HTTP_AUTHORIZATION', ''))
                db = connect(self.settings.database)
                if not admin.is_admin and not db.execute('SELECT 1 FROM authority_users WHERE object_id=?',(admin.object_id,)).fetchone():
                    raise HttpError(403,'This Microsoft identity has no alumni admin or assigned authority access')
                bind_mailbox(db, self.settings.mailbox)
                data = self.read_json(env) if method == 'POST' else {}
                result = self.dispatch(db, admin, method, path, parse_qs(env.get('QUERY_STRING', ''), max_num_fields=10), data)
            elif path == '/' and method == 'GET':
                result = {'service': 'UPES Alumni Routing', 'interface': 'Open UPES Alumni Routing inside Outlook. There is no standalone staff dashboard.'}
            else:
                raise HttpError(404, 'Endpoint not found')
        except (HttpError, AccessError) as error:
            status, result = error.status, {'error': str(error)}
        except ValueError as error:
            status, result = 400, {'error': str(error)}
        except sqlite3.IntegrityError:
            status, result = 409, {'error': 'Conflicting update. Refresh the ticket before acting.'}
        except Exception:
            status, result = 500, {'error': 'Operation failed; retained records are safe. Contact the operator.'}
            log.exception('api_request_failed', extra={'path': path})
        finally:
            if db is not None:
                db.close()
        if binary is None:
            binary = json.dumps(result, ensure_ascii=True).encode()
            headers.append(('X-Frame-Options', 'DENY'))
        headers += [('Content-Type', mime), ('Content-Length', str(len(binary)))]
        from http import HTTPStatus
        log.info('http_request', extra={'path': path, 'http_status': status})
        start_response(str(status) + ' ' + HTTPStatus(status).phrase, headers)
        return [binary]

    def read_json(self, env):
        if env.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            raise HttpError(415, 'application/json is required')
        try:
            length = int(env.get('CONTENT_LENGTH') or 0)
        except ValueError:
            raise HttpError(400, 'Invalid content length')
        if length < 1 or length > 16384:
            raise HttpError(413, 'JSON request must be between 1 and 16384 bytes')
        try:
            data = json.loads(env['wsgi.input'].read(length))
        except (ValueError, UnicodeError):
            raise HttpError(400, 'Malformed JSON')
        if not isinstance(data, dict):
            raise HttpError(400, 'JSON object required')
        if 'actor' in data:
            raise HttpError(400, 'Actor is derived from the validated Microsoft identity')
        reason = data.get('reason', '')
        if not isinstance(reason, str) or len(reason) > 3000:
            raise HttpError(400, 'Reason must be text up to 3000 characters')
        return data

    def require_admin(self, user):
        if not user.is_admin:
            raise HttpError(403,'Only Alumni Admin may perform this operation')

    def ticket(self, db, cid, user):
        row=db.execute('SELECT authority_id FROM concerns WHERE id=?',(cid,)).fetchone()
        if not row:
            raise HttpError(404,'Ticket not found')
        if not user.is_admin and not db.execute('SELECT 1 FROM authority_users WHERE authority_id=? AND object_id=?',(row[0],user.object_id)).fetchone():
            raise HttpError(403,'This ticket is not assigned to your authority')
        result=concern(db,cid)
        if not user.is_admin:
            result['actions']=[a for a in result['actions'] if a=='addressed']
            result['pilot_label']=None
        return result

    def dispatch(self, db, admin, method, path, query, data):
        if path=='/api/me' and method=='GET':
            return {'is_admin':admin.is_admin,'object_id':admin.object_id,'assignments':[dict(r) for r in db.execute('SELECT a.id,a.name FROM authorities a JOIN authority_users u ON u.authority_id=a.id WHERE u.object_id=?',(admin.object_id,))]}
        if path=='/api/changes' and method=='GET':
            if admin.is_admin:
                revision=db.execute('SELECT coalesce(max(id),0) FROM audit').fetchone()[0]
            else:
                revision=db.execute('SELECT coalesce(max(a.id),0) FROM audit a JOIN concerns c ON c.id=a.concern_id JOIN authority_users u ON u.authority_id=c.authority_id WHERE u.object_id=?',(admin.object_id,)).fetchone()[0]
            return {'revision':revision}
        if method == 'GET' and path == '/api/context':
            mid = query.get('message_id', [''])[0]
            if not mid or len(mid) > 2000:
                raise ValueError('Select a received Outlook message with an internetMessageId')
            row = db.execute('SELECT concern_id FROM receipts WHERE message_id=? UNION SELECT concern_id FROM outbound_messages WHERE message_id=?', (mid,mid)).fetchall()
            if len(row)!=1:
                raise HttpError(404, 'This email has not been linked yet. Refresh after worker synchronization, or use Assigned to me / the review queue. No action was taken.')
            return self.ticket(db,row[0][0],admin)
        if method == 'GET' and path == '/api/concerns':
            state = query.get('status', ['AWAITING_APPROVAL' if admin.is_admin else 'FORWARDED'])[0]
            if state not in ('AWAITING_APPROVAL','NEEDS_REVIEW','FAILED','FORWARDED','ADDRESSED','AWAITING_CONFIRMATION','CLOSED','ALL'):
                raise ValueError('Unsupported queue')
            if not admin.is_admin and state not in ('FORWARDED','ADDRESSED','AWAITING_CONFIRMATION','CLOSED','ALL'):
                raise HttpError(403,'Authorities cannot access the admin approval queue')
            before = integer(int(query.get('before', [str(2**63-1)])[0]), 'before')
            if before > 2**63-1:
                raise ValueError('Invalid cursor')
            rows = db.execute("SELECT c.id FROM concerns c WHERE c.id<? AND (?='ALL' OR c.status=? OR (?='FAILED' AND EXISTS(SELECT 1 FROM jobs j WHERE j.concern_id=c.id AND j.state IN ('FAILED','UNCERTAIN')))) AND (?=1 OR EXISTS(SELECT 1 FROM authority_users u WHERE u.authority_id=c.authority_id AND u.object_id=?)) ORDER BY c.id DESC LIMIT 31", (before,state,state,state,int(admin.is_admin),admin.object_id)).fetchall()
            items=[self.ticket(db,row[0],admin) for row in rows[:30]]
            for item in items:item.pop('body',None)
            return {'items':items,'next_before':rows[29][0] if len(rows)>30 else None}
        if path == '/api/routing':
            self.require_admin(admin)
            if method == 'GET':
                return routing(db)
            if method == 'POST':
                aid, email = data.get('authority_id'), data.get('email')
                if not isinstance(aid, str) or not isinstance(email, str):
                    raise ValueError('Authority ID and email must be text')
                configure_authority(db, aid, email, admin.actor,data.get('department'),data.get('object_ids'))
                return routing(db)
        if path=='/api/reminder-policy' and method=='POST':
            self.require_admin(admin)
            configure_policy(db,data,admin.actor)
            return routing(db)['reminder_policy']
        if path=='/api/metrics' and method=='GET':
            self.require_admin(admin)
            return metrics(db,query.get('mode',['live'])[0],query.get('from',[None])[0],query.get('to',[None])[0])
        if path == '/api/health' and method == 'GET':
            self.require_admin(admin)
            return {'dry_run': self.settings.dry_run, 'live_mailbox_authorized': self.settings.live_authorized,
                    'blocked_reminder_concerns':db.execute("SELECT count(*) FROM concerns WHERE status='FORWARDED' AND (reminder_blocked IS NOT NULL OR escalation_blocked_reason IS NOT NULL)").fetchone()[0],
                    'notification_failures':db.execute("SELECT count(*) FROM jobs WHERE kind IN ('ACK','REMINDER','ESCALATION') AND state IN ('FAILED','UNCERTAIN')").fetchone()[0],
                    'checkpoints': dict(db.execute("SELECT name,value FROM checkpoints WHERE name NOT IN ('graph_delta')").fetchall())}
        match = re.fullmatch(r'/api/concerns/([1-9][0-9]{0,14})(?:/(actions|history|due|label))?', path)
        if match:
            cid, operation = int(match[1]), match[2]
            current=self.ticket(db,cid,admin)
            if method == 'GET' and operation is None:
                return current
            if method == 'GET' and operation == 'history':
                before = integer(int(query['before'][0]), 'before') if 'before' in query else None
                if before is not None and before > 2**63-1:
                    raise ValueError('Invalid cursor')
                return history(db, cid, before)
            if method=='POST' and operation=='due':
                self.require_admin(admin)
                set_due(db,cid,data.get('due_at'),admin.actor,integer(data.get('version')))
                return self.ticket(db,cid,admin)
            if method=='POST' and operation=='label':
                self.require_admin(admin)
                label(db,cid,data.get('category'),data.get('authority_id'),data.get('reason',''),admin.actor,integer(data.get('version')))
                return self.ticket(db,cid,admin)
            if method == 'POST' and operation == 'actions':
                command = data.get('action')
                if not admin.is_admin and command!='addressed':
                    raise HttpError(403,'Assigned authorities can only mark their own forwarded ticket addressed')
                if command == 'reconcile':
                    self.require_admin(admin)
                    job_id = integer(data.get('job_id'), 'job_id')
                    if not isinstance(data.get('sent'), bool):
                        raise ValueError('sent must be a boolean verified delivery outcome')
                    job = db.execute('SELECT concern_id FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if not job or job[0] != cid:
                        raise ValueError('Job does not belong to ticket')
                    reconcile(db, job_id, data['sent'], admin.actor, data.get('reason', ''))
                else:
                    if command not in ('approve','reject','route','addressed','followup','close','reopen','retry','retry_notice'):
                        raise ValueError('Unsupported action')
                    action(db,self.settings,cid,command,admin.actor,integer(data.get('version')),data.get('category'),data.get('reason',''),None if admin.is_admin else admin.object_id,data.get('closure_outcome','administrative'))
                return self.ticket(db,cid,admin)
        raise HttpError(404, 'Endpoint not found')
