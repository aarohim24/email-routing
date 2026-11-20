"""Explicitly labelled pilot measurements; no inferred ground truth or silent AI promotion."""
import statistics
from .classifier import PILOT_SCOPE
from .db import audit, now, transaction
from .notifications import utc


def label(db, cid, category, authority_id, rationale, actor, version):
    if category not in PILOT_SCOPE + ('UNKNOWN',):
        raise ValueError('Choose a pilot category or UNKNOWN for an out-of-scope request')
    if not isinstance(rationale,str) or not rationale.strip() or len(rationale)>3000:
        raise ValueError('A labelling rationale is required (up to 3000 characters)')
    if category=='UNKNOWN' and authority_id is not None:
        raise ValueError('UNKNOWN labels must not guess an authority')
    if category!='UNKNOWN' and (not isinstance(authority_id,str) or not db.execute('SELECT 1 FROM authorities WHERE id=?',(authority_id,)).fetchone()):
        raise ValueError('Choose the institution-verified expected authority')
    with transaction(db):
        row=db.execute('SELECT version FROM concerns WHERE id=?',(cid,)).fetchone()
        if not row or row['version']!=version:
            raise ValueError('Ticket changed. Refresh before labelling.')
        previous=db.execute('SELECT * FROM pilot_labels WHERE concern_id=?',(cid,)).fetchone()
        db.execute('INSERT INTO pilot_labels VALUES(?,?,?,?,?,?) ON CONFLICT(concern_id) DO UPDATE SET category=excluded.category,authority_id=excluded.authority_id,labelled_by=excluded.labelled_by,rationale=excluded.rationale,labelled_at=excluded.labelled_at',
                   (cid,category,authority_id,actor,rationale,now()))
        db.execute('UPDATE concerns SET version=version+1 WHERE id=?',(cid,))
        audit(db,cid,actor,'PILOT_LABELLED',{'category':category,'authority_id':authority_id,'rationale':rationale,'previous':dict(previous) if previous else None})


def percent(numerator, denominator):
    return round(100*numerator/denominator,2) if denominator else None


def metrics(db, mode='live', start=None, end=None, at=None):
    if mode not in ('live','simulated','all'):
        raise ValueError('Metrics mode must be live, simulated or all')
    lower=utc(start) if start else None
    upper=utc(end) if end else None
    stamp=utc(at or now())
    if lower and upper and lower>=upper:
        raise ValueError('Metrics start must precede end')
    cohort=[]
    for row in db.execute('SELECT c.id,c.received_at,c.status,c.due_at,c.fulfilled_at,c.reopen_count,c.unresolved_count,c.simulated,c.initial_category,c.first_routed_category,c.first_routed_authority_id,l.category expected_category,l.authority_id expected_authority FROM concerns c LEFT JOIN pilot_labels l ON l.concern_id=c.id WHERE (?=\'all\' OR c.simulated=?)',(mode,0 if mode=='live' else 1)):
        received=utc(row['received_at'])
        if (lower and received<lower) or (upper and received>=upper):
            continue
        cohort.append(row)
    total=len(cohort)
    closed=sum(r['status']=='CLOSED' for r in cohort)
    pending=total-closed
    overdue=sum(r['status']=='FORWARDED' and bool(r['due_at']) and utc(r['due_at'])<stamp for r in cohort)
    labelled=[r for r in cohort if r['expected_category'] is not None]
    routed=[r for r in labelled if r['expected_category'] in PILOT_SCOPE and r['first_routed_category'] is not None]
    correct=sum(r['first_routed_category']==r['expected_category'] and r['first_routed_authority_id']==r['expected_authority'] for r in routed)
    classification_correct=sum(r['initial_category']==r['expected_category'] for r in labelled)
    times=[]
    invalid_times=0
    for row in cohort:
        if row['status']=='CLOSED' and row['fulfilled_at']:
            hours=(utc(row['fulfilled_at'])-utc(row['received_at'])).total_seconds()/3600
            if hours>=0:times.append(hours)
            else:invalid_times+=1
    ids={r['id'] for r in cohort}
    jobs=[r for r in db.execute('SELECT concern_id,kind,state,dry_run FROM jobs') if r['concern_id'] in ids and (mode=='all' or r['dry_run']==(mode=='simulated'))]
    count=lambda kind,state:sum(r['kind']==kind and r['state']==state for r in jobs)
    return {'mode':mode,'cohort_received_from':start,'cohort_received_before':end,'as_of':stamp.isoformat(),
            'total_concerns':total,'forwarded_without_due':sum(r['status']=='FORWARDED' and not r['due_at'] for r in cohort),'pending_concerns':pending,'overdue_authority_concerns':overdue,'closed_concerns':closed,
            'closure_rate_percent':percent(closed,total),'verified_fulfilled_concerns':len(times),
            'average_turnaround_hours':round(statistics.mean(times),2) if times else None,'median_turnaround_hours':round(statistics.median(times),2) if times else None,
            'turnaround_sample_size':len(times),'excluded_invalid_timing':invalid_times,
            'labelled_concerns':len(labelled),'classification_accuracy_percent':percent(classification_correct,len(labelled)),
            'routing_accuracy_percent':percent(correct,len(routed)),'routing_evaluated_concerns':len(routed),
            'reopened_concerns':sum(r['reopen_count']>0 for r in cohort),'reopen_events':sum(r['reopen_count'] for r in cohort),
            'reopen_rate_percent':percent(sum(r['reopen_count']>0 for r in cohort),total),
            'reported_unresolved_concerns':sum(r['unresolved_count']>0 for r in cohort),'unresolved_rate_percent':percent(sum(r['unresolved_count']>0 for r in cohort),total),
            'reminders_accepted':count('REMINDER','SUCCEEDED'),'escalations_accepted':count('ESCALATION','SUCCEEDED'),
            'reminders_pending':count('REMINDER','PENDING'),'escalations_pending':count('ESCALATION','PENDING'),
            'failed_notifications':sum(r['kind'] in ('ACK','REMINDER','ESCALATION') and r['state']=='FAILED' for r in jobs),
            'uncertain_notifications':sum(r['kind'] in ('ACK','REMINDER','ESCALATION') and r['state']=='UNCERTAIN' for r in jobs),
            'definition':'Receipt-date cohort. TAT uses currently closed, explicitly verified fulfilment only; administrative closures are excluded. Routing accuracy uses the first accepted route against explicit admin labels. Missing denominators return null. Simulations are excluded by default.'}


def evaluate(classifier, labelled_cases):
    """Evaluate any candidate implementing version + classify(subject,body); never activate it."""
    confusion={}
    correct=0
    count=0
    for case in labelled_cases:
        if case['expected_category'] not in PILOT_SCOPE+('UNKNOWN',):
            raise ValueError('Invalid evaluation label')
        result=classifier.classify(case['subject'],case['body'])
        if result.category not in PILOT_SCOPE+('UNKNOWN',):
            raise ValueError('Candidate returned an unsupported category')
        key=case['expected_category']+' -> '+result.category
        confusion[key]=confusion.get(key,0)+1
        correct+=result.category==case['expected_category'];count+=1
    return {'classifier_version':classifier.version,'sample_size':count,'accuracy_percent':percent(correct,count),'confusion':confusion,'activated':False}
