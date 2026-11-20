import fcntl
import json
import logging
import os
import time
from datetime import datetime
from .db import audit, now, transaction
from .graph import Graph, GraphError
from .workflow import get, ingest, transition
from .notifications import LIFECYCLE_KINDS, start_cycle, schedule

log = logging.getLogger(__name__)


def complete(db, job, actor='worker'):
    cid = job['concern_id']
    if job['kind'] in LIFECYCLE_KINDS:
        transition(db, cid, 'FORWARDED' if job['kind'] == 'FORWARD' else 'AWAITING_CONFIRMATION', actor, 'Simulated delivery' if job['dry_run'] else 'Microsoft Graph accepted delivery')
        if job['kind'] == 'FORWARD':
            row = get(db,cid)
            db.execute('UPDATE concerns SET first_routed_category=coalesce(first_routed_category,category),first_routed_authority_id=coalesce(first_routed_authority_id,authority_id) WHERE id=?',(cid,))
            start_cycle(db,cid,now())
    db.execute("UPDATE jobs SET state='SUCCEEDED',error=NULL,updated_at=? WHERE id=?", (now(), job['id']))
    audit(db, job['concern_id'], actor, 'DELIVERY_SUCCEEDED', {'job_id': job['id'], 'dry_run': bool(job['dry_run']), 'recipient': job['recipient']})


def recover(db):
    with transaction(db):
        for job in db.execute("SELECT * FROM jobs WHERE state='RUNNING'").fetchall():
            db.execute("UPDATE jobs SET state='UNCERTAIN',error='Worker interrupted; inspect Sent Items and draft before reconciliation',updated_at=? WHERE id=?", (now(), job['id']))
            if job['kind'] in LIFECYCLE_KINDS:
                transition(db, job['concern_id'], 'FAILED', 'worker', 'Interrupted delivery requires reconciliation')
            audit(db, job['concern_id'], 'worker', 'DELIVERY_UNCERTAIN', {'job_id': job['id']})


def deliver_one(db, settings, adapter=None):
    with transaction(db):
        job = db.execute("SELECT * FROM jobs WHERE state='PENDING' ORDER BY CASE kind WHEN 'FORWARD' THEN 0 WHEN 'FOLLOWUP' THEN 1 ELSE 2 END,id LIMIT 1").fetchone()
        if not job:
            return False
        db.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job['id']))
    sending = False
    try:
        row = get(db,job['concern_id'])
        if job['kind'] in ('REMINDER','ESCALATION') and (row['status'] != 'FORWARDED' or row['routing_cycle'] != job['routing_cycle']):
            with transaction(db):
                db.execute("UPDATE jobs SET state='CANCELLED',updated_at=? WHERE id=?",(now(),job['id']))
                audit(db,job['concern_id'],'worker','NOTICE_CANCELLED',{'job_id':job['id'],'reason':'Obsolete routing cycle or authority already addressed'})
            return True
        if job['kind'] in ('REMINDER','ESCALATION') and not db.execute('SELECT enabled FROM reminder_policy WHERE id=1').fetchone()[0]:
            with transaction(db):
                db.execute("UPDATE jobs SET state='CANCELLED',updated_at=? WHERE id=?",(now(),job['id']))
                audit(db,job['concern_id'],'worker','NOTICE_CANCELLED',{'job_id':job['id'],'reason':'Automatic reminders disabled'})
            return True
        if job['kind'] in ('REMINDER','ESCALATION'):
            current_recipient = (db.execute('SELECT email FROM authorities WHERE id=?',(row['authority_id'],)).fetchone() or [None])[0] if job['kind']=='REMINDER' else db.execute('SELECT escalation_email FROM reminder_policy WHERE id=1').fetchone()[0]
            if current_recipient != job['recipient']:
                with transaction(db):
                    db.execute("UPDATE jobs SET state='CANCELLED',updated_at=? WHERE id=?",(now(),job['id']))
                    audit(db,job['concern_id'],'worker','NOTICE_CANCELLED',{'job_id':job['id'],'reason':'Verified contact changed since scheduling'})
                return True
        if bool(job['dry_run']) != settings.dry_run:
            raise ValueError('Mode changed since approval; new approval required')
        if not job['dry_run']:
            graph = adapter or Graph(settings)
            draft_id = job['draft_id'] or graph.draft(job, get(db, job['concern_id']))
            with transaction(db):
                db.execute('UPDATE jobs SET draft_id=?,updated_at=? WHERE id=?', (draft_id, now(), job['id']))
                audit(db, job['concern_id'], 'worker', 'DRAFT_CREATED', {'job_id': job['id'], 'draft_id': draft_id})
            sending = True
            graph.send(draft_id)
        with transaction(db):
            complete(db, job)
        log.info('delivery_succeeded', extra={'job_id': job['id'], 'concern_id': job['concern_id']})
    except Exception as error:
        # 4xx responses (except timeout) explicitly reject delivery; network/5xx are ambiguous.
        uncertain = sending and not (isinstance(error, GraphError) and 400 <= error.status < 500 and error.status != 408)
        state = 'UNCERTAIN' if uncertain else 'FAILED'
        # Do not serialize arbitrary adapter exceptions, which may contain credentials or content.
        detail = str(error) if isinstance(error, (GraphError, ValueError)) else type(error).__name__
        with transaction(db):
            db.execute('UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?', (state, detail, now(), job['id']))
            if job['kind'] in LIFECYCLE_KINDS:
                transition(db, job['concern_id'], 'FAILED', 'worker', detail)
            audit(db, job['concern_id'], 'worker', 'DELIVERY_' + state, {'job_id': job['id'], 'error': detail})
        log.error('delivery_failed', extra={'job_id': job['id'], 'concern_id': job['concern_id'], 'delivery_state': state})
    return True


def reconcile(db, job_id, sent, actor, reason):
    if not reason.strip():
        raise ValueError('Document the Sent Items/draft evidence before reconciling')
    with transaction(db):
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['state'] != 'UNCERTAIN':
            raise ValueError('Only uncertain deliveries can be reconciled')
        audit(db, job['concern_id'], actor, 'DELIVERY_RECONCILED', {'job_id': job_id, 'sent': sent, 'evidence': reason})
        if sent:
            complete(db, job, actor)
        else:
            db.execute("UPDATE jobs SET state='FAILED',error='Admin confirmed not sent',updated_at=? WHERE id=?", (now(), job_id))


def poll(db, graph, since=None, settings=None):
    cursor = db.execute("SELECT value FROM checkpoints WHERE name='graph_delta'").fetchone()
    current_id = None
    try:
        if not since:
            raise ValueError('MS_INGEST_SINCE is required for mailbox ingestion')
        cutoff = datetime.fromisoformat(since.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            raise ValueError('MS_INGEST_SINCE must include a timezone')
        with transaction(db):
            previous = db.execute("SELECT value FROM checkpoints WHERE name='ingestion_since'").fetchone()
            if previous and previous[0] != since:
                raise ValueError('Ingestion cutoff changed; restore the configured start timestamp and review before resuming')
            db.execute("INSERT OR IGNORE INTO checkpoints VALUES('ingestion_since',?)", (since,))
        page = graph.delta(cursor[0] if cursor else None, since)
        for message in page.get('value', []):
            current_id = str(message.get('id', 'missing-id'))[:256]
            if '@removed' in message:
                with transaction(db):
                    audit(db, None, 'worker', 'MAILBOX_ITEM_REMOVED', {'graph_id': message.get('id'), 'action': 'Verify mailbox rules do not move/delete unprocessed mail'})
                continue
            if cutoff and message.get('receivedDateTime') and datetime.fromisoformat(message['receivedDateTime'].replace('Z', '+00:00')) < cutoff:
                continue
            ingest(db, message, settings)
        link = page.get('@odata.nextLink') or page.get('@odata.deltaLink')
        if not link:
            raise ValueError('Graph delta response missing checkpoint; cursor not advanced')
        with transaction(db):
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES('graph_delta',?)", (link,))
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES('last_ingestion',?)", (now(),))
            db.execute("DELETE FROM checkpoints WHERE name='ingestion_error'")
        return '@odata.nextLink' in page
    except Exception as error:
        detail = str(error) if isinstance(error, (GraphError, ValueError)) else type(error).__name__
        if current_id:
            detail += ' [Graph message: ' + current_id + ']'
        with transaction(db):
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES('ingestion_error',?)", (detail,))
            audit(db, None, 'worker', 'INGESTION_FAILED', {'error': detail, 'action': 'Fix error then resume; cursor retained'})
        raise


def run(db, settings):
    from .service import bind_mailbox
    bind_mailbox(db, settings.mailbox)
    # Deployment is intentionally single-host/single-worker. File lock prevents concurrent recoveries.
    with open(settings.database + '.worker.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another worker owns this database')
        recover(db)
        graph = Graph(settings) if settings.live_authorized else None
        while True:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES('worker_heartbeat',?)", (now(),))
            if graph:
                try:
                    for _ in range(10):
                        if not poll(db, graph, os.getenv('MS_INGEST_SINCE'),settings):
                            break
                except Exception:
                    log.error('ingestion_failed_cursor_retained')
            try:
                schedule(db,settings)
                if graph:
                    sync_outbound(db,graph)
            except Exception:
                log.exception('scheduler_or_outbound_sync_failed')
            for _ in range(20):
                if not deliver_one(db, settings, graph):
                    break
            time.sleep(15)


def sync_outbound(db, graph):
    """Retry only safe metadata reads after accepted sends; never resend for missing metadata."""
    for job in db.execute("SELECT j.* FROM jobs j WHERE j.state='SUCCEEDED' AND j.dry_run=0 AND j.draft_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM outbound_messages o WHERE o.job_id=j.id) ORDER BY coalesce(j.outbound_checked_at,''),j.id LIMIT 20").fetchall():
        db.execute('UPDATE jobs SET outbound_checked_at=? WHERE id=?',(now(),job['id']))
        try:
            mid = graph.sent_message_id(job['draft_id'])
            if mid:
                with transaction(db):
                    db.execute('INSERT OR IGNORE INTO outbound_messages VALUES(?,?,?)',(mid,job['concern_id'],job['id']))
        except Exception:
            log.warning('outbound_metadata_pending',extra={'job_id':job['id']})
