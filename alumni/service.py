"""Read models shared by Outlook API commands; no delivery operations in this module."""
import json
from .db import transaction
from .workflow import get, CATEGORIES


def bind_mailbox(db, mailbox):
    with transaction(db):
        current = db.execute("SELECT value FROM deployment_config WHERE key='mailbox'").fetchone()
        if current and current[0] != mailbox.lower():
            raise ValueError('Database belongs to another mailbox. Use a separate database.')
        db.execute("INSERT OR IGNORE INTO deployment_config VALUES('mailbox',?)", (mailbox.lower(),))


def available_actions(db, row):
    if db.execute("SELECT 1 FROM jobs WHERE concern_id=? AND ((kind IN ('FORWARD','FOLLOWUP') AND state IN ('PENDING','RUNNING','UNCERTAIN')) OR (kind IN ('REMINDER','ESCALATION') AND state='RUNNING'))", (row['id'],)).fetchone():
        return []
    state = row['status']
    commands = []
    if state == 'AWAITING_APPROVAL':
        commands += ['approve', 'reject']
    if state in ('NEEDS_REVIEW', 'AWAITING_APPROVAL', 'FAILED', 'REOPENED'):
        commands += ['route']
    if state == 'FORWARDED':
        commands += ['addressed']
    if state == 'ADDRESSED':
        commands += ['followup']
    if state == 'FAILED':
        commands += ['retry']
    if state in ('CLOSED', 'AWAITING_CONFIRMATION', 'NEEDS_REVIEW'):
        commands += ['reopen']
    if state in ('AWAITING_CONFIRMATION', 'NEEDS_REVIEW', 'FAILED', 'AWAITING_APPROVAL'):
        commands += ['close']
    if db.execute("SELECT 1 FROM jobs WHERE concern_id=? AND kind IN ('ACK','REMINDER','ESCALATION') AND state='FAILED'",(row['id'],)).fetchone():
        commands += ['retry_notice']
    return commands


def concern(db, cid):
    row = get(db, cid)
    result = dict(row)
    authority = db.execute('SELECT id,name,email,department FROM authorities WHERE id=?', (row['authority_id'],)).fetchone()
    result['authority'] = dict(authority) if authority else None
    result['actions'] = available_actions(db, row)
    result['revision'] = db.execute('SELECT coalesce(max(id),0) FROM audit WHERE concern_id=?',(cid,)).fetchone()[0]
    result['pilot_label'] = dict(label) if (label := db.execute('SELECT * FROM pilot_labels WHERE concern_id=?',(cid,)).fetchone()) else None
    result['deliveries'] = [dict(j) for j in db.execute('SELECT id,kind,state,recipient,draft_id,error,dry_run,created_at FROM jobs WHERE concern_id=? ORDER BY id DESC', (cid,))]
    # History is paginated separately so even long-lived tickets retain a complete accessible audit.
    return result


def history(db, cid, before=None):
    get(db, cid)
    rows = db.execute('SELECT * FROM audit WHERE concern_id=? AND id<? ORDER BY id DESC LIMIT 51', (cid, before or 2**63-1)).fetchall()
    return {'items': [dict(r, details=json.loads(r['details'])) for r in rows[:50]],
            'next_before': rows[49]['id'] if len(rows) > 50 else None}


def routing(db):
    return {'categories': list(CATEGORIES), 'authorities': [dict(a) for a in db.execute('SELECT * FROM authorities ORDER BY name')],
            'routes': [dict(r) for r in db.execute('SELECT * FROM routes ORDER BY category')],
            'authority_users':[dict(r) for r in db.execute('SELECT * FROM authority_users ORDER BY authority_id,object_id')],
            'reminder_policy':dict(db.execute('SELECT * FROM reminder_policy WHERE id=1').fetchone())}
