"""Durable, bounded notifications. None of these jobs forward original email content."""
from datetime import datetime, timezone, timedelta
from .db import audit, now, transaction

LIFECYCLE_KINDS = ('FORWARD', 'FOLLOWUP')
NOTICE_KINDS = ('ACK', 'REMINDER', 'ESCALATION')


def utc(value):
    if not isinstance(value, str):
        raise ValueError('A timezone-aware ISO due date is required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Date must include a timezone')
    return stamp.astimezone(timezone.utc)


def queue_notice(db, cid, kind, recipient, simulated, key, actor='scheduler', cycle=None):
    cur = db.execute('INSERT OR IGNORE INTO jobs(concern_id,kind,recipient,approved_by,dry_run,created_at,updated_at,delivery_key,routing_cycle) VALUES(?,?,?,?,?,?,?,?,?)',
                     (cid, kind, recipient, actor, int(simulated), now(), now(), key, cycle))
    if cur.rowcount:
        audit(db, cid, actor, kind + '_QUEUED', {'job_id': cur.lastrowid, 'recipient': recipient, 'dry_run': bool(simulated), 'delivery_key': key})
    return bool(cur.rowcount)


def cancel_pending_reminders(db, cid, actor):
    for row in db.execute("SELECT id FROM jobs WHERE concern_id=? AND kind IN ('REMINDER','ESCALATION') AND state IN ('PENDING','FAILED')", (cid,)).fetchall():
        db.execute("UPDATE jobs SET state='CANCELLED',updated_at=? WHERE id=?", (now(), row['id']))
        audit(db, cid, actor, 'NOTICE_CANCELLED', {'job_id': row['id'], 'reason': 'Unaccepted notice is obsolete after completion or due-date change'})


def start_cycle(db, cid, stamp):
    row = db.execute('SELECT * FROM concerns WHERE id=?', (cid,)).fetchone()
    policy = db.execute('SELECT * FROM reminder_policy WHERE id=1').fetchone()
    due = row['due_override_at'] or (utc(stamp) + timedelta(hours=policy['due_hours'])).isoformat()
    db.execute('UPDATE concerns SET forwarded_at=?,routing_cycle=routing_cycle+1,due_at=?,next_reminder_at=?,reminders_queued=0,escalation_queued=0,reminder_blocked=NULL,escalation_blocked_reason=NULL,addressed_at=NULL WHERE id=?', (stamp, due, due, cid))
    audit(db, cid, 'worker', 'DUE_DATE_SET', {'due_at': due, 'routing_cycle': row['routing_cycle'] + 1})


def schedule(db, settings, at=None):
    stamp = utc(at or now())
    queued = 0
    with transaction(db):
        policy = db.execute('SELECT * FROM reminder_policy WHERE id=1').fetchone()
        if not policy['enabled']:
            return 0
        for row in db.execute("SELECT c.*,a.email FROM concerns c LEFT JOIN authorities a ON a.id=c.authority_id WHERE c.status='FORWARDED' AND c.due_at IS NOT NULL").fetchall():
            # Never reinterpret simulated tickets as live mail after a deployment-mode change.
            if bool(row['simulated']) != settings.dry_run:
                continue
            due = utc(row['due_at'])
            if stamp < due:
                continue
            if row['reminders_queued'] < policy['max_reminders'] and row['next_reminder_at'] and stamp >= utc(row['next_reminder_at']):
                if not row['email']:
                    if row['reminder_blocked'] != 'Authority email missing':
                        db.execute("UPDATE concerns SET reminder_blocked='Authority email missing' WHERE id=?", (row['id'],))
                        audit(db, row['id'], 'scheduler', 'REMINDER_BLOCKED', {'reason': 'Authority email missing'})
                else:
                    key = 'reminder:%d:%d:%d' % (row['id'], row['routing_cycle'], row['reminders_queued'] + 1)
                    if queue_notice(db, row['id'], 'REMINDER', row['email'], settings.dry_run, key, cycle=row['routing_cycle']):
                        queued += 1
                        db.execute('UPDATE concerns SET reminders_queued=reminders_queued+1,next_reminder_at=?,reminder_blocked=NULL WHERE id=?', ((stamp + timedelta(hours=policy['interval_hours'])).isoformat(), row['id']))
            if not row['escalation_queued'] and stamp >= due + timedelta(hours=policy['escalation_delay_hours']):
                if not policy['escalation_email']:
                    if not row['escalation_blocked_reason']:
                        db.execute("UPDATE concerns SET escalation_blocked_reason='Escalation recipient not configured' WHERE id=?", (row['id'],))
                        audit(db, row['id'], 'scheduler', 'ESCALATION_BLOCKED', {'reason': 'Institution-verified escalation email not configured'})
                elif queue_notice(db, row['id'], 'ESCALATION', policy['escalation_email'], settings.dry_run, 'escalation:%d:%d' % (row['id'], row['routing_cycle']), cycle=row['routing_cycle']):
                    queued += 1
                    db.execute('UPDATE concerns SET escalation_queued=1,escalation_blocked_reason=NULL WHERE id=?', (row['id'],))
    return queued


def configure_policy(db, values, actor):
    from .workflow import email_address
    required = ('enabled','due_hours','interval_hours','max_reminders','escalation_delay_hours','escalation_email')
    if set(values) != set(required):
        raise ValueError('Supply all reminder policy fields')
    if not isinstance(values['enabled'], bool):
        raise ValueError('enabled must be boolean')
    for name, lower, upper in [('due_hours',1,8760),('interval_hours',1,8760),('max_reminders',0,20),('escalation_delay_hours',0,8760)]:
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(name + ' is outside its permitted integer range')
    address = values['escalation_email']
    if address is not None and not isinstance(address, str):
        raise ValueError('Escalation email must be text or null')
    address = email_address(address) if address and address.strip() else None
    with transaction(db):
        if db.execute("SELECT 1 FROM jobs WHERE kind IN ('REMINDER','ESCALATION') AND state='RUNNING'").fetchone():
            raise ValueError('A reminder delivery is in progress. Retry the policy update after delivery completes.')
        db.execute('UPDATE reminder_policy SET enabled=?,due_hours=?,interval_hours=?,max_reminders=?,escalation_delay_hours=?,escalation_email=? WHERE id=1',
                   (int(values['enabled']),values['due_hours'],values['interval_hours'],values['max_reminders'],values['escalation_delay_hours'],address))
        audit(db,None,actor,'REMINDER_POLICY_CHANGED',dict(values,escalation_email=address))


def set_due(db, cid, value, actor, version):
    from .workflow import get
    due = utc(value).isoformat() if value is not None else None
    with transaction(db):
        row = get(db,cid)
        if row['version'] != version:
            raise ValueError('Ticket changed. Refresh before changing the due date.')
        if row['status'] not in ('AWAITING_APPROVAL','NEEDS_REVIEW','FORWARDED'):
            raise ValueError('Due dates may change only before routing or while awaiting authority completion')
        if db.execute("SELECT 1 FROM jobs WHERE concern_id=? AND state='RUNNING'", (cid,)).fetchone():
            raise ValueError('Wait for the active delivery before changing its due date')
        cancel_pending_reminders(db,cid,actor)
        if row['status']=='FORWARDED' and due is None:
            policy=db.execute('SELECT due_hours FROM reminder_policy WHERE id=1').fetchone()
            due=(utc(row['forwarded_at'])+timedelta(hours=policy[0])).isoformat()
        db.execute('UPDATE concerns SET due_override_at=?,due_at=?,next_reminder_at=?,version=version+1,updated_at=? WHERE id=?', (value and utc(value).isoformat(),due,due,now(),cid))
        audit(db,cid,actor,'DUE_DATE_CHANGED',{'due_at':due,'previous_due_at':row['due_at']})
