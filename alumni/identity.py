"""Validate Entra v2 delegated API access tokens, then authorize immutable admin IDs."""
from dataclasses import dataclass
from uuid import UUID
import jwt


class AccessError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def guid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Microsoft application, tenant and admin object IDs must be UUIDs') from None


@dataclass(frozen=True)
class Admin:
    tenant_id: str
    object_id: str
    is_admin: bool = True

    @property
    def actor(self):
        return 'entra:' + self.tenant_id + ':' + self.object_id


class EntraIdentity:
    def __init__(self, settings):
        self.settings = settings
        self.configured = bool(settings.tenant_id and settings.api_audience and settings.addin_client_id and settings.admin_object_ids)
        self.jwks = None
        if self.configured:
            self.tenant = guid(settings.tenant_id)
            self.audience = guid(settings.api_audience)
            self.client = guid(settings.addin_client_id)
            self.admins = frozenset(guid(v) for v in settings.admin_object_ids)
            self.issuer = 'https://login.microsoftonline.com/' + self.tenant + '/v2.0'
            # Tenant-specific fixed URL; never follow token-supplied jku/x5u or issuers.
            self.jwks = jwt.PyJWKClient('https://login.microsoftonline.com/' + self.tenant + '/discovery/v2.0/keys',
                                       cache_jwk_set=True, lifespan=300, timeout=10)

    def authenticate(self, authorization):
        if not self.configured:
            raise AccessError('Institutional authentication is not configured. Contact UPES IT.', 503)
        if not authorization.startswith('Bearer ') or len(authorization) > 16384:
            raise AccessError('A Microsoft API access token is required')
        token = authorization[7:]
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.audience, issuer=self.issuer,
                                leeway=30, options={'strict_aud': True, 'require': ['exp', 'iat', 'nbf', 'iss', 'aud', 'tid', 'oid', 'azp', 'scp', 'ver']})
        except jwt.PyJWKClientConnectionError:
            raise AccessError('Microsoft signing keys unavailable. Retry shortly.', 503) from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AccessError('Invalid or expired Microsoft access token') from None
        if any(not isinstance(claims[name], str) for name in ('tid', 'oid', 'azp', 'scp', 'ver')):
            raise AccessError('Invalid Microsoft identity claim types')
        if claims['tid'] != self.tenant or claims['ver'] != '2.0' or claims['azp'] != self.client:
            raise AccessError('Token tenant or client is not permitted', 403)
        if 'access_as_user' not in str(claims['scp']).split():
            raise AccessError('Required delegated API scope is missing', 403)
        # Assignment authorization is checked against the database on every API request.
        return Admin(self.tenant, claims['oid'], claims['oid'] in self.admins)
