import os
from dataclasses import dataclass


def flag(name, default=False):
    value = os.getenv(name, str(default)).lower()
    if value not in ('true', 'false'):
        raise ValueError(name + ' must be true or false')
    return value == 'true'


@dataclass
class Settings:
    database: str = 'data/alumni.db'
    dry_run: bool = True
    live_authorized: bool = False
    tenant_id: str = ''
    client_id: str = ''  # Background worker registration, never exposed to the add-in.
    client_secret: str = ''
    mailbox: str = 'alumni@upes.ac.in'
    mailbox_type: str = 'unverified'
    mailbox_verified: bool = False
    api_audience: str = ''
    addin_client_id: str = ''
    admin_object_ids: tuple = ()
    base_url: str = 'https://localhost:8000'

    @classmethod
    def load(cls):
        return cls(database=os.getenv('DATABASE_PATH', 'data/alumni.db'),
                   dry_run=flag('DRY_RUN', True), live_authorized=flag('LIVE_MAILBOX_AUTHORIZED'),
                   tenant_id=os.getenv('MS_TENANT_ID', ''), client_id=os.getenv('MS_CLIENT_ID', ''),
                   client_secret=os.getenv('MS_CLIENT_SECRET', ''), mailbox=os.getenv('MAILBOX', 'alumni@upes.ac.in'),
                   mailbox_type=os.getenv('MAILBOX_TYPE', 'unverified'), mailbox_verified=flag('MAILBOX_TYPE_VERIFIED'),
                   api_audience=os.getenv('API_CLIENT_ID', ''), addin_client_id=os.getenv('ADDIN_CLIENT_ID', ''),
                   admin_object_ids=tuple(v.strip().lower() for v in os.getenv('ADMIN_OBJECT_IDS', '').split(',') if v.strip()),
                   base_url=os.getenv('BASE_URL', 'https://localhost:8000'))

    def require_live(self):
        if not self.live_authorized:
            raise ValueError('Live mailbox access is disabled; explicit institutional authorization is required')
        if not self.mailbox_verified or self.mailbox_type not in ('shared', 'standalone'):
            raise ValueError('UPES IT must verify MAILBOX_TYPE before enabling live access')
        if not all((self.tenant_id, self.client_id, self.client_secret)):
            raise ValueError('Missing Microsoft tenant ID, client ID or client secret')
