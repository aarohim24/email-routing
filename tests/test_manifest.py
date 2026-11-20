import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from test_workflow import CaseFixture, message
from alumni.db import migrate, connect
from alumni.workflow import ingest

spec=importlib.util.spec_from_file_location('render_manifest',Path(__file__).resolve().parent.parent/'scripts/render_manifest.py')
manifest=importlib.util.module_from_spec(spec);spec.loader.exec_module(manifest)


class ManifestTests(unittest.TestCase):
    def test_deployment_manifest(self):
        root=ET.fromstring(manifest.render('https://localhost:8000','11111111-1111-4111-8111-111111111111'))
        ns={'o':'http://schemas.microsoft.com/office/appforoffice/1.1','v':'http://schemas.microsoft.com/office/mailappversionoverrides/1.1'}
        self.assertEqual(root.find('o:Permissions',ns).text,'ReadItem')
        self.assertEqual(root.find('.//v:SupportsPinning',ns).text,'true')
        self.assertEqual(root.find('.//v:SupportsSharedFolders',ns).text,'true')
        self.assertEqual(root.find('o:DisplayName',ns).attrib['DefaultValue'],'UPES Alumni Routing')

    def test_reject_untrusted_manifest_origins(self):
        for origin in ('http://example.org','https://user:secret@example.org','https://example.org/path','https://example.org?query=1'):
            with self.assertRaises(ValueError):manifest.render(origin,'11111111-1111-4111-8111-111111111111')


class MigrationTests(CaseFixture,unittest.TestCase):
    def test_upgrade_preserves_existing_concern_and_idempotency(self):
        # Recreate a populated v1 database, then run the real incremental migrator.
        self.db.close()
        legacy=str(Path(self.temp.name)/'legacy.db')
        self.db=connect(legacy)
        sql=(Path(__file__).resolve().parent.parent/'migrations/001_initial.sql').read_text()
        self.db.executescript(sql)
        self.db.execute("INSERT INTO concerns(message_id,graph_id,thread_id,sender,subject,body,received_at,updated_at,status) VALUES('m1','gm1','t1','student@example.org','Transcript','Body','2026-10-23T10:00:00Z','2026-10-23T10:00:00Z','NEEDS_REVIEW')")
        cid=1
        self.db.execute("INSERT INTO receipts VALUES('m1',1,'{}','2026-10-23T10:00:00Z')")
        migrate(legacy)
        self.assertEqual(ingest(self.db,message()),(cid,False))
        self.assertEqual(self.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],4)
        self.assertEqual(self.db.execute('SELECT count(*) FROM concerns').fetchone()[0],1)
