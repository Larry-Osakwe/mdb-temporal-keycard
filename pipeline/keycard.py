"""Keycard credential plumbing: just-in-time secrets instead of env vars.

Keycard mode is opt-in: set KEYCARD_ZONE_URL (plus KEYCARD_CLIENT_ID /
KEYCARD_CLIENT_SECRET for the worker's application credential) and the three
upstream secrets move out of .env entirely:

- Inside Temporal activities, credentials come from the grant decorator: each
  activity declares its resources with @grant, the worker's KeycardInterceptor
  mints them fresh per execution, and access(resource) returns them. That is
  the only minting path activities use.
- Outside activities there is exactly one other path, mint_secret(): the
  OpenAI key at worker startup (the OpenAI Agents plugin builds its client
  before any activity exists) and the non-worker processes that share
  pipeline.clients (the index-creation script, the HTTP APIs). Every call
  mints fresh; there is no cache.

The worker's own identity comes from keycardai.oauth's discover_credential
convention. This demo runs on localhost with a client secret; on a platform
that issues workload identity the same discovery picks up the platform token
file instead (see the README's "last secret" section). App-as-itself minting
with an assertion credential is pending SDK support, so mint_secret carries
the same ClientSecret requirement as the interceptor's client-credentials
path for now.

Without KEYCARD_ZONE_URL everything falls back to the .env values, so the
repo keeps working exactly as before.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from keycardai.oauth import Client


def keycard_enabled() -> bool:
    return bool(settings.keycard_zone_url)


def export_credential_env() -> None:
    """Seed the process environment from settings for the Keycard SDK.

    pydantic-settings reads .env into the settings object without touching
    os.environ, but the SDK's discover_credential() reads the environment.
    """
    import os

    if settings.keycard_client_id:
        os.environ.setdefault("KEYCARD_CLIENT_ID", settings.keycard_client_id)
    if settings.keycard_client_secret:
        os.environ.setdefault("KEYCARD_CLIENT_SECRET", settings.keycard_client_secret)


@lru_cache(maxsize=1)
def _zone_client() -> "Client":
    """One zone client per process, authenticated the way the interceptor is.

    The credential comes from the SDK's discover_credential convention. Only a
    ClientSecret can do app-as-itself minting today (the interceptor documents
    the same restriction), so anything else fails with a pointed error rather
    than a confusing 401.
    """
    from keycardai.oauth import Client
    from keycardai.oauth.server import ClientSecret, discover_credential

    export_credential_env()
    credential = discover_credential()
    if credential is None:
        raise RuntimeError(
            "Keycard mode needs an application credential; set KEYCARD_CLIENT_ID "
            "and KEYCARD_CLIENT_SECRET alongside KEYCARD_ZONE_URL."
        )
    if not isinstance(credential, ClientSecret):
        raise RuntimeError(
            "App-as-itself minting outside activities requires a ClientSecret "
            f"credential; discovered {type(credential).__name__}. Workload "
            "identity support for this path is pending in the SDK."
        )
    # The credential carries its auth strategy; reuse it rather than rebuilding.
    return Client(settings.keycard_zone_url, auth=credential.auth)


def mint_secret(resource: str) -> str:
    """Mint the vault-held secret for `resource` (client-credentials grant).

    Per-call mint, no cache: rotation in Keycard propagates immediately, and
    the callers are one-shot (worker startup, infra scripts, API startup).
    """
    return _zone_client().client_credentials_grant(resource=resource).access_token


def activity_grant_token(resource: str) -> str | None:
    """The credential the KeycardInterceptor minted for this activity execution.

    Selects by resource, since activities here grant several (Atlas plus
    Voyage). Returns None outside an activity (the trigger/agent HTTP APIs and
    infra scripts share pipeline.clients but run outside Temporal) and for
    resources the running activity's grant does not declare.
    """
    try:
        from keycardai.temporal import access

        return access(resource).access_token
    except Exception:
        return None


def mongodb_uri() -> str:
    """The Atlas connection string: per-activity mint inside activities, a
    fresh service mint everywhere else, .env outside Keycard mode."""
    if not keycard_enabled():
        return settings.mongodb_uri
    return activity_grant_token(
        settings.keycard_mongodb_resource
    ) or mint_secret(settings.keycard_mongodb_resource)


def voyage_api_key() -> str:
    """Per-activity grant (embed/rerank/search declare Voyage); a fresh
    service mint only for callers outside a granted activity."""
    if not keycard_enabled():
        return settings.voyage_api_key
    return activity_grant_token(
        settings.keycard_voyage_resource
    ) or mint_secret(settings.keycard_voyage_resource)


def openai_api_key() -> str:
    if not keycard_enabled():
        return settings.openai_api_key
    return mint_secret(settings.keycard_openai_resource)
