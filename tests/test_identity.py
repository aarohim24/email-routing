import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from alumni.config import Settings
from alumni.identity import EntraIdentity, AccessError

TENANT='11111111-1111-4111-8111-111111111111'
API='22222222-2222-4222-8222-222222222222'
CLIENT='33333333-3333-4333-8333-333333333333'
ADMIN='44444444-4444-4444-8444-444444444444'


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537,key_size=2048)

    def setUp(self):
        self.identity = EntraIdentity(Settings(tenant_id=TENANT,api_audience=API,addin_client_id=CLIENT,admin_object_ids=(ADMIN,)))
        self.identity.jwks = Mock()
        self.identity.jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.key.public_key())
        self.claims = {'iss':f'https://login.microsoftonline.com/{TENANT}/v2.0','aud':API,'tid':TENANT,'oid':ADMIN,'azp':CLIENT,'scp':'access_as_user','ver':'2.0','exp':int(time.time())+600,'iat':int(time.time()),'nbf':int(time.time())-5}

    def authenticate(self, **changes):
        claims=dict(self.claims, **changes)
        return self.identity.authenticate('Bearer '+jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'test'}))

    def test_valid_named_admin(self):
        self.assertEqual(self.authenticate().actor, f'entra:{TENANT}:{ADMIN}')

    def test_reject_claim_substitution(self):
        for field,value in [('tid',CLIENT),('aud',CLIENT),('iss','https://attacker.invalid'),('azp',API),('scp','Mail.Read'),('ver','1.0'),('exp',1),('nbf',int(time.time())+600)]:
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.authenticate(**{field:value})

    def test_reject_unsigned_or_symmetric_token(self):
        for algorithm,key in [('none',None),('HS256','not-the-signing-key-not-the-signing-key')]:
            token=jwt.encode(self.claims,key,algorithm=algorithm)
            with self.assertRaises(AccessError):
                self.identity.authenticate('Bearer '+token)

    def test_missing_claims_and_app_only(self):
        for field in ('exp','nbf','iat','scp','oid'):
            claims=dict(self.claims);claims.pop(field)
            token=jwt.encode(claims,self.key,algorithm='RS256')
            with self.assertRaises(AccessError):
                self.identity.authenticate('Bearer '+token)

    def test_nonadmin_identity_has_no_admin_role(self):
        self.assertFalse(self.authenticate(oid=CLIENT).is_admin)

    def test_unconfigured_is_closed(self):
        with self.assertRaises(AccessError) as error:
            EntraIdentity(Settings()).authenticate('Bearer any')
        self.assertEqual(error.exception.status,503)
