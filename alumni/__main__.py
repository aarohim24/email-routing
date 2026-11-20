import argparse
import json
from .config import Settings
from .db import connect, migrate
from .logging_config import configure_logging


def main():
    parser = argparse.ArgumentParser(description='UPES alumni routing operations')
    parser.add_argument('command', choices=['migrate', 'serve', 'worker', 'demo', 'evaluate-baseline'])
    parser.add_argument('--mode',choices=['live','simulated','all'],default='live')
    args = parser.parse_args()
    configure_logging()
    settings = Settings.load()
    migrate(settings.database)
    if args.command == 'evaluate-baseline':
        from .pilot import evaluate
        from .classifier import RuleBasedClassifier
        db=connect(settings.database)
        try:
            rows=db.execute("SELECT c.subject,c.body,l.category expected_category FROM concerns c JOIN pilot_labels l ON l.concern_id=c.id WHERE (?='all' OR c.simulated=?)",(args.mode,0 if args.mode=='live' else 1)).fetchall()
            print(json.dumps(dict(evaluate(RuleBasedClassifier(),rows),mode=args.mode),sort_keys=True))
        finally:
            db.close()
    elif args.command == 'serve':
        from wsgiref.simple_server import make_server
        from .api import App
        print('Local API: http://127.0.0.1:8000. Outlook requires a trusted HTTPS reverse proxy at BASE_URL.')
        make_server('127.0.0.1', 8000, App(settings)).serve_forever()
    elif args.command == 'worker':
        from .worker import run
        db = connect(settings.database)
        try:
            run(db, settings)
        finally:
            db.close()
    elif args.command == 'demo':
        if not settings.dry_run or settings.live_authorized:
            raise SystemExit('Demo requires DRY_RUN=true and LIVE_MAILBOX_AUTHORIZED=false')
        from .workflow import configure_authority, ingest
        db = connect(settings.database)
        try:
            configure_authority(db, 'rita', 'rita@demo.invalid', 'demo-setup')
            configure_authority(db, 'hemant', 'hemant@demo.invalid', 'demo-setup')
            subjects = ['Request for academic transcript', 'Migration certificate request', 'Provisional degree request', 'Degree verification for employer', 'Medium of instruction certificate', 'Collect my degree', 'Need help with my alumni account', 'Transcript and migration certificate']
            from .db import now
            for i, subject in enumerate(subjects):
                ingest(db, {'id': 'demo-graph-%d' % i, 'internetMessageId': '<demo-%d@demo.invalid>' % i, 'conversationId': 'demo-thread-%d' % i,
                            'from': {'emailAddress': {'address': 'student%d@demo.invalid' % i}}, 'subject': subject,
                            'body': {'contentType': 'text', 'content': 'Dear Alumni Team,\n\n' + subject + '. Please guide me through the next steps.\n\nThank you,\nDemo Student'}, 'receivedDateTime': now()})
            print('Created 8 synthetic concerns. Addresses use the reserved .invalid domain. Use a separate demo database.')
        finally:
            db.close()


if __name__ == '__main__':
    main()
