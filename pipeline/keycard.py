"""Keycard credential plumbing: just-in-time secrets instead of env vars.

Keycard mode is opt-in: set KEYCARD_ZONE_URL (plus KEYCARD_CLIENT_ID /
KEYCARD_CLIENT_SECRET for the worker's application credential) and the three
upstream secrets move out of .env entirely:

- MongoDB Atlas: the connection string lives in Keycard's vault behind the
  resource named by KEYCARD_MONGODB_RESOURCE. Every Temporal activity carries
  @grant(mongodb_resource), so the worker's KeycardInterceptor mints the
  credential fresh for each activity execution and access() returns it inside
  the activity. Nothing credential-shaped enters workflow history, which
  Temporal persists and replays.
- Voyage AI and OpenAI: their API keys live in the vault behind their own
  resources and are minted here with a plain client-credentials grant, cached
  until shortly before expiry. OpenAI is minted once at worker startup because
  the OpenAI Agents plugin builds its client before any activity runs.

Without KEYCARD_ZONE_URL everything falls back to the .env values, so the
repo keeps working exactly as before.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from keycardai.oauth import Client


def keycard_enabled() -> bool:
    return bool(settings.keycard_zone_url)


@lru_cache(maxsize=1)
def _oauth_client() -> "Client":
    """One zone client per process; its pooled transport is reused across mints."""
    import os

    from keycardai.oauth import BasicAuth, Client

    client_id = os.environ.get("KEYCARD_CLIENT_ID", "")
    client_secret = os.environ.get("KEYCARD_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise RuntimeError(
            "Keycard mode needs KEYCARD_CLIENT_ID and KEYCARD_CLIENT_SECRET "
            "alongside KEYCARD_ZONE_URL."
        )
    return Client(settings.keycard_zone_url, auth=BasicAuth(client_id, client_secret))


_cache_lock = threading.Lock()
_secret_cache: dict[str, tuple[str, float]] = {}


def service_secret(resource: str) -> str:
    """Mint the vault-held secret for `resource` (client-credentials grant).

    Cached until 60 seconds before expiry, so rotation in Keycard propagates
    without a worker restart. Thread-safe: activities run in a thread pool.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _secret_cache.get(resource)
        if hit and hit[1] > now:
            return hit[0]
    token = _oauth_client().client_credentials_grant(resource=resource)
    expires = now + max((token.expires_in or 300) - 60, 30)
    with _cache_lock:
        _secret_cache[resource] = (token.access_token, expires)
    return token.access_token


def activity_grant_token() -> str | None:
    """The credential the KeycardInterceptor minted for this activity execution.

    Returns None outside an activity (the trigger/agent HTTP APIs and infra
    scripts share pipeline.clients but run outside Temporal).
    """
    try:
        from keycardai.temporal import access

        return access().access_token
    except Exception:
        return None


def mongodb_uri() -> str:
    """The Atlas connection string: per-activity mint inside activities, a
    cached service mint everywhere else, .env outside Keycard mode."""
    if not keycard_enabled():
        return settings.mongodb_uri
    return activity_grant_token() or service_secret(settings.keycard_mongodb_resource)


def voyage_api_key() -> str:
    if not keycard_enabled():
        return settings.voyage_api_key
    return service_secret(settings.keycard_voyage_resource)


def openai_api_key() -> str:
    if not keycard_enabled():
        return settings.openai_api_key
    return service_secret(settings.keycard_openai_resource)
