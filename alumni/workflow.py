import json
import re
from datetime import datetime
from .classifier import RuleBasedClassifier, PILOT_SCOPE, plain, resolution_reply
from .extraction import extract
from .config import Settings
from .notifications import queue_notice, cancel_pending_reminders
from .db import audit, now, transaction

CATEGORIES = PILOT_SCOPE
TRANSITIONS = {
    'RECEIVED': {'IDENTIFIED', 'NEEDS_REVIEW'},
    'IDENTIFIED': {'AWAITING_APPROVAL', 'NEEDS_REVIEW'},
    'AWAITING_APPROVAL': {'FORWARDED', 'NEEDS_REVIEW', 'FAILED', 'CLOSED'},
    'NEEDS_REVIEW': {'AWAITING_APPROVAL', 'CLOSED', 'REOPENED'},
    'FORWARDED': {'ADDRESSED', 'NEEDS_REVIEW'},
    'ADDRESSED': {'AWAITING_CONFIRMATION', 'FAILED', 'NEEDS_REVIEW'},
    'AWAITING_CONFIRMATION': {'CLOSED', 'REOPENED', 'NEEDS_REVIEW'},
    'REOPENED': {'AWAITING_APPROVAL', 'NEEDS_REVIEW'},
    'FAILED': {'AWAITING_APPROVAL', 'ADDRESSED', 'FORWARDED', 'AWAITING_CONFIRMATION', 'NEEDS_REVIEW', 'CLOSED'},
    'CLOSED': {'REOPENED'},
}


def email_address(value):
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
        raise ValueError('A valid single email address is required')
    return value


def get(db, cid):
    row = db.execute('SELECT * FROM concerns WHERE id=?', (cid,)).fetchone()
    if not row:
        raise ValueError('Concern not found')
    return row


def transition(db, cid, target, actor, reason):
    row = get(db, cid)
    if target not in TRANSITIONS[row['status']]:
        raise ValueError('Invalid transition: %s → %s' % (row['status'], target))
    db.execute('UPDATE concerns SET status=?,version=version+1,updated_at=? WHERE id=?', (target, now(), cid))
    if target in ('ADDRESSED','CLOSED','REOPENED'):
        cancel_pending_reminders(db,cid,actor)
    if target == 'ADDRESSED':
        db.execute('UPDATE concerns SET addressed_at=? WHERE id=?',(now(),cid))
    elif target == 'CLOSED':
        db.execute('UPDATE concerns SET closed_at=? WHERE id=?',(now(),cid))
    elif target == 'REOPENED':
        db.execute('UPDATE concerns SET reopen_count=reopen_count+1,closed_at=NULL,fulfilled_at=NULL,due_at=NULL,due_override_at=NULL,next_reminder_at=NULL WHERE id=?',(cid,))
    audit(db, cid, actor, 'STATUS_CHANGED', {'from': row['status'], 'to': target, 'reason': reason})


def route(db, category):
    return db.execute('SELECT a.* FROM routes r JOIN authorities a ON a.id=r.authority_id WHERE r.category=?', (category,)).fetchone()


def propose(db, cid, category, actor):
    if category not in CATEGORIES:
        raise ValueError('Select a supported category')
    authority = route(db, category)
    db.execute('UPDATE concerns SET category=?,authority_id=?,requested_document=?,version=version+1 WHERE id=?',
               (category, authority['id'] if authority else None, category, cid))
    audit(db, cid, actor, 'ROUTING_PROPOSED', {'category': category, 'authority': dict(authority) if authority else None})
    target = 'AWAITING_APPROVAL' if authority and authority['email'] else 'NEEDS_REVIEW'
    if get(db, cid)['status'] != target:
        transition(db, cid, target, actor, 'Routing configured' if target == 'AWAITING_APPROVAL' else 'Missing authority email')
    if target == 'AWAITING_APPROVAL':
        audit(db, cid, 'system', 'ADMIN_NOTIFICATION', {'channel': 'outlook_pending_approvals', 'ticket_id': cid})


def ingest(db, payload, settings=None, classifier=None):
    """Receipt and all workflow changes are committed atomically, before cursor advancement."""
    settings = settings or Settings()
    classifier = classifier or RuleBasedClassifier()
    mid = payload.get('internetMessageId')
    gid = payload.get('id')
    if not all(isinstance(v,str) and v for v in (mid,gid)):
        raise ValueError('Email lacks required stable identifiers')
    def duplicate():
        existing=db.execute('SELECT concern_id FROM receipts WHERE graph_id=?',(gid,)).fetchone()
        if existing:
            return existing[0]
        existing=db.execute('SELECT concern_id,graph_id FROM receipts WHERE message_id=?',(mid,)).fetchone()
        if existing:
            raise ValueError('Message-ID collision or unverifiable legacy receipt: retain the page for manual reconciliation')
        return None
    previous=duplicate()
    if previous is not None:
        return previous,False
    thread = payload.get('conversationId')
    sender = email_address(payload.get('from', {}).get('emailAddress', {}).get('address', ''))
    if not all(isinstance(v, str) and v for v in (mid, gid, thread, payload.get('receivedDateTime'))):
        raise ValueError('Email lacks required stable identifiers or timestamp')
    stamp = datetime.fromisoformat(payload['receivedDateTime'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Email timestamp must include timezone')
    subject = str(payload.get('subject') or '')[:2000]
    body_data = payload.get('body') or {}
    body = plain(str(body_data.get('content') or ''), body_data.get('contentType', 'text'))
    if len(body) > 2_000_000:
        raise ValueError('Email exceeds safe processing size; ingestion paused for manual intervention')
    headers = {str(h.get('name','')).strip().lower():str(h.get('value','')).strip().lower() for h in (payload.get('internetMessageHeaders') or []) if isinstance(h,dict)}
    automated = (headers.get('auto-submitted','no') != 'no' or 'x-autoreply' in headers or 'x-autorespond' in headers or headers.get('return-path')=='<>' or sender.lower()==settings.mailbox.lower() or bool(re.match(r'^(automatic reply|out of office|undeliverable):',subject,re.I)))
    with transaction(db):
        existing=duplicate()
        if existing is not None:
            return existing,False
        # Only correlate replies to a waiting case from its original sender.
        candidates = db.execute("SELECT * FROM concerns WHERE thread_id=? AND lower(sender)=lower(?) AND status='AWAITING_CONFIRMATION'", (thread, sender)).fetchall()
        cid = None
        if len(candidates) == 1:
            case = candidates[0]
            cid = case['id']
            result = None if automated else resolution_reply(body)
            target = 'CLOSED' if result is True else 'REOPENED' if result is False else 'NEEDS_REVIEW'
            transition(db, cid, target, 'email', 'Student response: ' + mid)
            if result is True:
                db.execute('UPDATE concerns SET fulfilled_at=? WHERE id=?',(now(),cid))
            elif result is False:
                db.execute('UPDATE concerns SET unresolved_count=unresolved_count+1 WHERE id=?',(cid,))
            audit(db, cid, 'email', 'CONFIRMATION_RECEIVED', {'message_id': mid, 'body': body, 'automated':automated})
            if target == 'REOPENED':
                propose(db, cid, case['category'], 'system')
        else:
            cur = db.execute('INSERT INTO concerns(message_id,graph_id,thread_id,sender,subject,body,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                             (mid, gid, thread, sender, subject, body, payload['receivedDateTime'], now()))
            cid = cur.lastrowid
            audit(db, cid, 'system', 'EMAIL_RECEIVED', {'message_id': mid})
            result = classifier.classify(subject, body)
            if result.category not in CATEGORIES + ('UNKNOWN',):
                raise ValueError('Classifier returned a category outside pilot scope')
            fields = extract(body)
            db.execute('UPDATE concerns SET alumnus_name=?,enrolment_number=?,alumni_number=?,initial_category=?,classifier_version=?,simulated=? WHERE id=?',
                       (fields['alumnus_name'],fields['enrolment_number'],fields['alumni_number'],result.category,classifier.version,int(settings.dry_run),cid))
            audit(db,cid,'extractor:labelled-fields-v1','STRUCTURED_EXTRACTED',fields)
            audit(db, cid, 'classifier:' + classifier.version, 'CLASSIFIED', result.__dict__)
            if result.category == 'UNKNOWN':
                transition(db, cid, 'NEEDS_REVIEW', 'system', result.reason)
            else:
                transition(db, cid, 'IDENTIFIED', 'system', result.reason)
                propose(db, cid, result.category, 'system')
            db.execute('UPDATE concerns SET initial_authority_id=authority_id WHERE id=?',(cid,))
            if automated:
                audit(db,cid,'intake','ACK_SUPPRESSED',{'reason':'Automated or self-generated mail; avoid acknowledgement loops'})
            else:
                queue_notice(db,cid,'ACK',sender,settings.dry_run,'ack:%d'%cid,actor='intake')
        db.execute('INSERT INTO receipts(message_id,concern_id,payload,created_at,graph_id) VALUES(?,?,?,?,?)', (mid, cid, json.dumps(payload), now(),gid))
        return cid, True


def action(db, settings, cid, command, actor, version, category=None, reason='', authority_object_id=None, closure_outcome='administrative'):
    with transaction(db):
        row = get(db, cid)
        if row['version'] != version:
            raise ValueError('This concern changed. Refresh before acting.')
        if authority_object_id is not None:
            assignment = db.execute('SELECT 1 FROM authority_users WHERE authority_id=? AND object_id=?',(row['authority_id'],authority_object_id)).fetchone()
            if not assignment or command != 'addressed' or row['status'] != 'FORWARDED' or not reason.strip():
                raise ValueError('Assigned authorities may only mark their own forwarded concern addressed, with completion evidence')
        active = db.execute("SELECT 1 FROM jobs WHERE concern_id=? AND ((kind IN ('FORWARD','FOLLOWUP') AND state IN ('PENDING','RUNNING','UNCERTAIN')) OR (kind IN ('REMINDER','ESCALATION') AND state='RUNNING'))", (cid,)).fetchone()
        if active:
            raise ValueError('A delivery is pending or uncertain; reconcile it before changing the concern')
        if command == 'route':
            if row['status'] not in ('NEEDS_REVIEW', 'AWAITING_APPROVAL', 'FAILED', 'REOPENED'):
                raise ValueError('Routing cannot change in this state')
            if not reason.strip():
                raise ValueError('Routing change requires a reason')
            audit(db, cid, actor, 'ROUTING_CHANGED', {'previous_category': row['category'], 'reason': reason})
            propose(db, cid, category, actor)
        elif command == 'approve':
            if row['status'] != 'AWAITING_APPROVAL':
                raise ValueError('Only a pending approval may be approved')
            authority = route(db, row['category'])
            if not authority or not authority['email'] or authority['id'] != row['authority_id']:
                raise ValueError('Missing or changed authority; review routing first')
            recipient = email_address(authority['email'])
            audit(db, cid, actor, 'APPROVED', {'recipient': recipient, 'version': version, 'dry_run': settings.dry_run})
            enqueue(db, settings, cid, 'FORWARD', recipient, actor, version)
        elif command == 'reject':
            if row['status'] != 'AWAITING_APPROVAL' or not reason.strip():
                raise ValueError('Reject requires pending approval and a reason')
            audit(db, cid, actor, 'REJECTED', {'reason': reason})
            transition(db, cid, 'NEEDS_REVIEW', actor, reason)
        elif command == 'addressed':
            transition(db, cid, 'ADDRESSED', actor, reason or 'Authority addressed concern')
            enqueue(db, settings, cid, 'FOLLOWUP', row['sender'], actor, version)
        elif command == 'followup':
            if row['status'] != 'ADDRESSED':
                raise ValueError('Follow-up requires an addressed concern')
            enqueue(db, settings, cid, 'FOLLOWUP', row['sender'], actor, version)
        elif command == 'reopen':
            if row['status'] not in ('CLOSED', 'AWAITING_CONFIRMATION', 'NEEDS_REVIEW') or not reason.strip():
                raise ValueError('Reopen requires closed, confirmation or review status and a reason')
            transition(db, cid, 'REOPENED', actor, reason)
            if row['category'] in CATEGORIES:
                propose(db, cid, row['category'], actor)
            else:
                transition(db, cid, 'NEEDS_REVIEW', actor, 'Select category')
        elif command == 'close':
            if row['status'] not in ('AWAITING_CONFIRMATION', 'NEEDS_REVIEW', 'FAILED', 'AWAITING_APPROVAL') or not reason.strip():
                raise ValueError('Close requires a confirmation/review/failure/pending case and a reason')
            if closure_outcome not in ('resolved','administrative'):
                raise ValueError('Choose resolved or administrative closure')
            transition(db, cid, 'CLOSED', actor, reason)
            if closure_outcome == 'resolved':
                db.execute('UPDATE concerns SET fulfilled_at=? WHERE id=?',(now(),cid))
            audit(db,cid,actor,'CLOSURE_RECORDED',{'outcome':closure_outcome,'evidence':reason})
        elif command == 'retry_notice':
            job = db.execute("SELECT * FROM jobs WHERE concern_id=? AND kind IN ('ACK','REMINDER','ESCALATION') AND state='FAILED' ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
            if not job:
                raise ValueError('No safely retryable failed notification')
            if job['kind'] != 'ACK' and row['status'] != 'FORWARDED':
                raise ValueError('Do not retry obsolete authority reminders')
            db.execute("UPDATE jobs SET state='PENDING',error=NULL,updated_at=? WHERE id=?",(now(),job['id']))
            db.execute('UPDATE concerns SET version=version+1 WHERE id=?',(cid,))
            audit(db,cid,actor,'NOTICE_RETRY_REQUESTED',{'job_id':job['id']})
        elif command == 'retry':
            if row['status'] != 'FAILED':
                raise ValueError('Only failed delivery may be retried')
            job = db.execute("SELECT * FROM jobs WHERE concern_id=? AND kind IN ('FORWARD','FOLLOWUP') ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            if not job or job['state'] != 'FAILED':
                raise ValueError('No safely retryable failed job')
            transition(db, cid, 'AWAITING_APPROVAL' if job['kind'] == 'FORWARD' else 'ADDRESSED', actor, 'Retry requested')
            # Forwarding always needs a fresh approval; follow-up retries can be queued directly.
            if job['kind'] == 'FOLLOWUP':
                enqueue(db, settings, cid, 'FOLLOWUP', row['sender'], actor, version)
        else:
            raise ValueError('Unsupported action')


def enqueue(db, settings, cid, kind, recipient, actor, version):
    if bool(get(db,cid)['simulated']) != settings.dry_run:
        raise ValueError('Ticket intake mode differs from this deployment. Keep simulation and live pilot databases separate; do not promote staged tickets into live delivery.')
    db.execute('INSERT INTO jobs(concern_id,kind,recipient,approved_by,approved_version,dry_run,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
               (cid, kind, recipient, actor, version, int(settings.dry_run), now(), now()))
    db.execute('UPDATE concerns SET version=version+1,updated_at=? WHERE id=?', (now(), cid))
    audit(db, cid, actor, 'DELIVERY_QUEUED', {'kind': kind, 'recipient': recipient, 'dry_run': settings.dry_run})


def configure_authority(db, aid, email, actor, department=None, object_ids=None):
    email = email_address(email) if email.strip() else None
    if department is not None and (not isinstance(department,str) or len(department)>150):
        raise ValueError('Department must be text up to 150 characters, or null')
    if object_ids is not None:
        from .identity import guid
        if not isinstance(object_ids,list) or len(object_ids)>20:
            raise ValueError('Supply up to 20 institution-verified Entra user object IDs')
        object_ids=sorted(set(guid(value) for value in object_ids))
    with transaction(db):
        if not db.execute('SELECT 1 FROM authorities WHERE id=?', (aid,)).fetchone():
            raise ValueError('Unknown authority')
        if db.execute("SELECT 1 FROM jobs j JOIN concerns c ON c.id=j.concern_id WHERE c.authority_id=? AND ((j.kind IN ('FORWARD','FOLLOWUP') AND j.state IN ('PENDING','RUNNING','UNCERTAIN')) OR (j.kind IN ('REMINDER','ESCALATION') AND j.state='RUNNING'))", (aid,)).fetchone():
            raise ValueError('Reconcile pending deliveries for this authority before changing its email')
        db.execute('UPDATE authorities SET email=? WHERE id=?', (email, aid))
        if department is not None:
            db.execute('UPDATE authorities SET department=? WHERE id=?',(department.strip() or None,aid))
        if object_ids is not None:
            db.execute('DELETE FROM authority_users WHERE authority_id=?',(aid,))
            db.executemany('INSERT INTO authority_users VALUES(?,?)',[(aid,value) for value in object_ids])
        audit(db, None, actor, 'AUTHORITY_CONFIGURED', {'authority': aid, 'email': email, 'department':department, 'object_ids':object_ids})
        for row in db.execute("SELECT * FROM concerns WHERE authority_id=? AND status='AWAITING_APPROVAL'", (aid,)).fetchall():
            transition(db, row['id'], 'NEEDS_REVIEW', actor, 'Authority configuration changed; routing review required')
