import json
import time

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings

SCOPES = ["https://www.googleapis.com/auth/calendar"]
STATE_MAX_AGE = 600  # 10 minutes

# Store the Flow object between auth URL generation and callback so PKCE
# code_verifier is preserved across the two requests.
_pending_flows: dict[str, Flow] = {}


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY)


def make_state_token(discord_id: str) -> str:
    return _signer().dumps(discord_id, salt="oauth-state")


def verify_state_token(token: str) -> str | None:
    try:
        return _signer().loads(token, salt="oauth-state", max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def build_auth_url(state: str) -> str:
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    _pending_flows[state] = flow
    return auth_url


def exchange_code(state: str, authorization_response_url: str) -> str:
    """Exchange authorization code for credentials using the original Flow object."""
    flow = _pending_flows.pop(state, None)
    if flow is None:
        raise ValueError("No pending OAuth flow found — link may have already been used or expired")
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials.to_json()


def _make_flow() -> Flow:
    redirect_uri = f"{settings.WEB_HOST}/auth/google/callback"
    if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        client_config = {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    return Flow.from_client_secrets_file(settings.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri)
