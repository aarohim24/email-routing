"""Run a complete synthetic pilot with no credentials, network, persistent DB or auth bypass."""
import json
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alumni.config import Settings
from alumni.db import connect, migrate
from alumni.workflow import ingest, get, action, configure_authority
from alumni.worker import deliver_one
from alumni.notifications import configure_policy, schedule, utc
from alumni.pilot import label, metrics


def run():
    results=[]
    def check(condition,name):
        if not condition:
            raise RuntimeError('FAILED: '+name)
        results.append(name)
    with tempfile.TemporaryDirectory(prefix='upes-offline-') as directory:
        path=str(Path(directory)/'synthetic.db')
        settings=Settings(database=path,dry_run=True,live_authorized=False,mailbox='sandbox@demo.invalid')
        migrate(path);migrate(path)
        db=connect(path)
        try:
            configure_authority(db,'rita','authority@demo.invalid','synthetic-admin',object_ids=['55555555-5555-4555-8555-555555555555'])
            configure_policy(db,{'enabled':True,'due_hours':24,'interval_hours':24,'max_reminders':2,'escalation_delay_hours':24,'escalation_email':'escalation@demo.invalid'},'synthetic-admin')
            def mail(mid,subject,body,thread='synthetic-thread'):
                return {'id':'provider-'+mid,'internetMessageId':mid,'conversationId':thread,'subject':subject,
                        'from':{'emailAddress':{'address':'student@demo.invalid'}},
                        'receivedDateTime':datetime.now(timezone.utc).isoformat(),'body':{'contentType':'text','content':body}}
            original=mail('original','Transcript request','Name: Synthetic Alumni\nEnrolment Number: DEMO001\nPlease provide my transcript.')
            def act(cid,command,**kwargs):
                action(db,settings,cid,command,'synthetic-admin',get(db,cid)['version'],**kwargs)
            def drain():
                for _ in range(30):
                    if not deliver_one(db,settings):return
                raise RuntimeError('Outbox did not drain within 30 jobs')
            with patch('alumni.worker.Graph',side_effect=RuntimeError('Network adapter must never be constructed during this walkthrough')):
                cid,created=ingest(db,original,settings)
                check(created and get(db,cid)['status']=='AWAITING_APPROVAL','Intake classifies and waits for admin approval')
                check(ingest(db,original,settings)==(cid,False),'Duplicate intake creates no new ticket or acknowledgement')
                check(get(db,cid)['enrolment_number']=='DEMO001','Explicit identity fields are extracted')
                check(get(db,cid)['alumni_number'] is None,'Missing identity remains NULL')
                unknown,_=ingest(db,mail('unknown','Grade card request','Please provide my grade card.','other-thread'),settings)
                check(get(db,unknown)['status']=='NEEDS_REVIEW','Out-of-scope request enters review')
                drain()
                check(db.execute("SELECT count(*) FROM jobs WHERE kind='ACK' AND state='SUCCEEDED'").fetchone()[0]==2,'Receipt acknowledgements are simulated once each')
                check(db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0]==0,'Worker never forwards before approval')
                version=get(db,cid)['version'];act(cid,'approve')
                try:
                    action(db,settings,cid,'approve','synthetic-admin',version)
                except ValueError:
                    pass
                else:
                    raise RuntimeError('Duplicate approval was accepted')
                drain()
                check(get(db,cid)['status']=='FORWARDED','Approved forwarding is simulated')
                due=utc(get(db,cid)['due_at'])
                check(schedule(db,settings,(due-timedelta(seconds=1)).isoformat())==0,'No early reminder')
                check(schedule(db,settings,due.isoformat())==1,'First due reminder reserved')
                check(schedule(db,settings,due.isoformat())==0,'Repeated scheduler tick cannot duplicate reminder')
                drain()
                check(schedule(db,settings,(due+timedelta(hours=24)).isoformat())==2,'Second reminder and configured escalation reserved')
                drain()
                check(schedule(db,settings,(due+timedelta(days=5)).isoformat())==0,'Reminder/escalation cap survives later ticks')
                act(cid,'addressed',reason='Synthetic authority completion evidence',authority_object_id='55555555-5555-4555-8555-555555555555')
                drain()
                check(get(db,cid)['status']=='AWAITING_CONFIRMATION','Assigned authority completion queues student follow-up')
                ingest(db,mail('negative','Re: Transcript','NOT RESOLVED'),settings)
                check(get(db,cid)['status']=='AWAITING_APPROVAL','Negative confirmation reopens and requires fresh approval')
                check(db.execute("SELECT count(*) FROM jobs WHERE kind='FORWARD'").fetchone()[0]==1,'Reopening does not forward automatically')
                act(cid,'approve');drain();act(cid,'addressed',reason='Synthetic second completion');drain()
                ingest(db,mail('positive','Re: Transcript','RESOLVED'),settings)
                check(get(db,cid)['status']=='CLOSED','Positive confirmation closes ticket')
                label(db,cid,'TRANSCRIPT','rita','Reviewed synthetic original request','synthetic-admin',get(db,cid)['version'])
                data=metrics(db,'simulated')
                check(data['routing_accuracy_percent']==100 and data['routing_evaluated_concerns']==1,'Routing accuracy uses explicit reviewed sample')
                check(data['closure_rate_percent']==50 and data['reopened_concerns']==1,'Closure/reopen metrics match two-case fixture')
                check(data['reminders_accepted']==2 and data['escalations_accepted']==1,'Metrics count accepted simulated notices accurately')
                check(metrics(db)['total_concerns']==0,'Live metrics exclude this simulated fixture')
                check(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall(),'Database integrity and foreign keys remain valid')
            return {'result':'PASS','checks_passed':len(results),'checks':results,'safety':{'dry_run':True,'live_authorized':False,'credentials_used':False,'external_adapter_constructed':False,'temporary_database_removed_after_exit':True},'simulated_counts':{'tickets':2,'approved_forwards':2,'acknowledgements':2,'reminders':2,'escalations':1,'closed':1,'needs_review':1}}
        finally:
            db.close()


if __name__=='__main__':
    print(json.dumps(run(),indent=2))
