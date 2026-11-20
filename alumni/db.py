import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        # Set the file mode before SQLite creates WAL/SHM companions.
        if str(path) != ':memory:':
            Path(path).chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        for attempt in range(5):
            try:
                db.execute('PRAGMA journal_mode=WAL')
                break
            except sqlite3.OperationalError as error:
                if 'locked' not in str(error).lower() or attempt==4:
                    raise
                time.sleep(0.05 * 2**attempt)
        return db
    except Exception:
        db.close()
        raise


def statements(sql):
    """Split using SQLite's parser boundary test, preserving quoted semicolons/triggers."""
    buffer=''
    for char in sql:
        buffer+=char
        if char==';' and sqlite3.complete_statement(buffer):
            yield buffer
            buffer=''
    if buffer.strip():
        yield buffer


def migrate(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = connect(path)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
        for file in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
            version = int(file.name.split('_')[0])
            # Recheck under SQLite's write lock. executescript would implicitly commit
            # this transaction, so execute complete statements individually instead.
            with transaction(db):
                if not db.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
                    for statement in statements(file.read_text()):
                        db.execute(statement)
    finally:
        db.close()


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise


def audit(db, cid, actor, event, details):
    db.execute('INSERT INTO audit(concern_id,actor,event,details,created_at) VALUES(?,?,?,?,?)',
               (cid, actor, event, json.dumps(details, sort_keys=True), now()))


def schema_ready(db):
    required={int(p.name.split('_')[0]) for p in (Path(__file__).parent.parent/'migrations').glob('*.sql')}
    try:
        applied={row[0] for row in db.execute('SELECT version FROM schema_migrations')}
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError:
        return False
    return required.issubset(applied) and {'concerns','jobs','receipts','audit','authority_users','reminder_policy','pilot_labels'}.issubset(tables)
