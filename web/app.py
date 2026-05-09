import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request

# Allow HTTP redirects in local dev. In production WEB_HOST should be https://
# and this flag should not be set.
if os.getenv("WEB_HOST", "").startswith("http://"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
from fastapi.responses import HTMLResponse

from config import settings
from db import database as db
from web.oauth import build_auth_url, exchange_code, make_state_token, verify_state_token

logger = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None)

_discord_client = None


def set_discord_client(client):
    global _discord_client
    _discord_client = client


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/auth/google")
async def auth_google(state: str):
    """Redirect user to Google OAuth consent screen."""
    discord_id = verify_state_token(state)
    if not discord_id:
        raise HTTPException(400, "Invalid or expired state token")
    url = build_auth_url(state)
    return HTMLResponse(
        f'<html><head><meta http-equiv="refresh" content="0;url={url}"></head>'
        f'<body>Redirecting to Google...</body></html>'
    )


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str | None = None,
                                state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<html><body>Authorization denied: {error}. You can close this tab.</body></html>")

    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    discord_id = verify_state_token(state)
    if not discord_id:
        raise HTTPException(400, "Invalid or expired state token — please start registration again")

    # Fly.io (and most reverse proxies) terminate TLS and forward HTTP internally,
    # so request.url has http:// even though the client used https://.
    # oauthlib enforces https on the authorization_response, so fix the scheme.
    callback_url = str(request.url)
    if callback_url.startswith("http://") and settings.WEB_HOST.startswith("https://"):
        callback_url = "https://" + callback_url[len("http://"):]

    try:
        token_json = exchange_code(state, callback_url)
    except Exception as e:
        logger.exception("Token exchange failed")
        raise HTTPException(500, f"Failed to exchange authorization code: {e}")

    conn = db.get_connection()
    try:
        db.store_google_token(conn, discord_id, token_json)
        logger.info(f"Stored Google token for {discord_id}")
    finally:
        conn.close()

    # Sync calendar list now that we have a valid token
    try:
        from services import calendar_service
        conn2 = db.get_connection()
        try:
            user_ctx = db.get_user(conn2, discord_id)
            if user_ctx:
                calendar_service.sync_user_calendars(conn2, user_ctx)
        finally:
            conn2.close()
    except Exception:
        logger.exception(f"Calendar list sync failed for {discord_id}")

    # DM the user to complete onboarding
    if _discord_client:
        asyncio.create_task(_send_onboarding_dm(discord_id))

    return HTMLResponse(
        "<html><body>"
        "<h2>Connected!</h2>"
        "<p>Your Google Calendar is linked. You can close this tab and return to Discord.</p>"
        "</body></html>"
    )


async def _send_onboarding_dm(discord_id: str):
    try:
        user = await _discord_client.fetch_user(int(discord_id))
        await user.send(
            "Google Calendar connected! A couple quick setup questions:\n\n"
            "1. What's your name?\n"
            "2. What's your timezone? (e.g. America/New_York, America/Chicago, America/Los_Angeles, Europe/London)"
        )
    except Exception:
        logger.exception(f"Failed to send onboarding DM to {discord_id}")
