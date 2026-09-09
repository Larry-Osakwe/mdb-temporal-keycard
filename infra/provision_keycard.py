"""One-shot Keycard zone provisioning for the demo (run by a signed-in human).

Creates, in the zone named by KEYCARD_PROVISION_ZONE_ID:

  1. an application for the Temporal worker, with a client-secret credential
  2. three resources on the zone's Keycard Vault provider:
       KEYCARD_MONGODB_RESOURCE  -> vaulted MongoDB connection string
       KEYCARD_VOYAGE_RESOURCE   -> vaulted Voyage API key
       KEYCARD_OPENAI_RESOURCE   -> vaulted OpenAI API key
  3. the three resources as dependencies of the application

then prints the .env block to paste (client secret included once, never logged).

Auth: KEYCARD_ADMIN_TOKEN env var, or the signed-in Keycard CLI session token
from the macOS keychain (a permission prompt may appear; that is this script
asking, on your machine).

Secrets: MONGODB_URI / VOYAGE_API_KEY / OPENAI_API_KEY env vars, or interactive
prompts. A blank value skips that resource so you can provision in stages.

Usage:  uv run python -m infra.provision_keycard
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("KEYCARD_API_BASE", "https://api.keycard.ai")
ZONE_ID = os.environ.get("KEYCARD_PROVISION_ZONE_ID", "bsq01zgq46reqv1l2fj7hgjhgt")
APP_NAME = os.environ.get("KEYCARD_PROVISION_APP_NAME", "temporal-pipeline-worker")

RESOURCES = [
    # (env var for the secret value, resource identifier env, default identifier, name)
    ("MONGODB_URI", "KEYCARD_MONGODB_RESOURCE", "https://cluster.mongodb.net", "MongoDB Atlas"),
    ("VOYAGE_API_KEY", "KEYCARD_VOYAGE_RESOURCE", "https://api.voyageai.com", "Voyage AI"),
    ("OPENAI_API_KEY", "KEYCARD_OPENAI_RESOURCE", "https://api.openai.com", "OpenAI"),
]


def admin_token() -> str:
    tok = os.environ.get("KEYCARD_ADMIN_TOKEN", "")
    if tok:
        return tok
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Keycard CLI", "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception as e:
        sys.exit(f"No KEYCARD_ADMIN_TOKEN and no CLI keychain token readable ({e}). "
                 "Sign in with `keycard auth signin` or export KEYCARD_ADMIN_TOKEN.")


def api(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text[:300]}


def must(status: int, payload: dict, what: str) -> dict:
    if status >= 300:
        sys.exit(f"{what} failed: HTTP {status} {json.dumps(payload)[:300]}")
    return payload


def find_items(payload: dict) -> list[dict]:
    if isinstance(payload, list):
        return payload
    for key in ("items", "data", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def main() -> None:
    token = admin_token()

    # Sanity: can we see the zone?
    status, zone = api(token, "GET", f"/zones/{ZONE_ID}")
    must(status, zone, f"GET zone {ZONE_ID} (is the token an org admin session, and API base {API_BASE} right?)")
    print(f"zone: {zone.get('name', ZONE_ID)}")

    # Vault provider (per-zone; created by the platform, but create one if absent).
    status, provs = api(token, "GET", f"/zones/{ZONE_ID}/providers?type=keycard-vault")
    must(status, provs, "list providers")
    vaults = [p for p in find_items(provs) if p.get("type") == "keycard-vault"]
    if vaults:
        vault_id = vaults[0]["id"]
    else:
        status, created = api(token, "POST", f"/zones/{ZONE_ID}/providers",
                              {"name": "Keycard Vault", "type": "keycard-vault"})
        vault_id = must(status, created, "create vault provider")["id"]
    print(f"vault provider: {vault_id}")

    # Application (reuse by name if it exists).
    status, apps = api(token, "GET", f"/zones/{ZONE_ID}/applications")
    must(status, apps, "list applications")
    existing = [a for a in find_items(apps) if a.get("name") == APP_NAME]
    if existing:
        app = existing[0]
        print(f"application (existing): {app['id']}")
    else:
        status, app = api(token, "POST", f"/zones/{ZONE_ID}/applications", {"name": APP_NAME})
        app = must(status, app, "create application")
        print(f"application: {app['id']}")

    # Client-secret credential (always minted fresh; old ones stay valid unless revoked).
    status, cred = api(token, "POST", f"/zones/{ZONE_ID}/application-credentials",
                       {"application_id": app["id"], "type": "password"})
    cred = must(status, cred, "create application credential")
    client_id, client_secret = cred.get("identifier"), cred.get("password")
    if not (client_id and client_secret):
        sys.exit(f"credential response missing identifier/password: keys={sorted(cred)}")
    print(f"client credential: {client_id}")

    env_lines = [
        f"KEYCARD_ZONE_URL=https://{ZONE_ID}.keycard.cloud",
        f"KEYCARD_CLIENT_ID={client_id}",
        f"KEYCARD_CLIENT_SECRET={client_secret}",
    ]

    for secret_env, ident_env, default_ident, name in RESOURCES:
        identifier = os.environ.get(ident_env, default_ident)
        value = os.environ.get(secret_env) or getpass.getpass(f"{name} secret ({secret_env}, blank to skip): ")
        if not value:
            print(f"skipped: {name}")
            continue

        status, found = api(token, "GET",
                            f"/zones/{ZONE_ID}/resources?filter[identifier]={urllib.parse.quote(identifier, safe='')}")
        must(status, found, f"list resources for {identifier}")
        hits = [r for r in find_items(found) if r.get("identifier") == identifier]
        if hits:
            res = hits[0]
            print(f"resource (existing): {identifier} -> {res['id']}")
        else:
            status, res = api(token, "POST", f"/zones/{ZONE_ID}/resources",
                              {"name": name, "identifier": identifier, "credential_provider_id": vault_id})
            res = must(status, res, f"create resource {identifier}")
            print(f"resource: {identifier} -> {res['id']}")

        status, secret = api(token, "POST", f"/zones/{ZONE_ID}/secrets",
                             {"name": f"{name} credential", "entity_id": res["id"],
                              "data": {"type": "token", "token": value}})
        must(status, secret, f"vault secret for {identifier}")
        print(f"vaulted: {identifier} (secret {secret.get('id', '?')})")

        status, dep = api(token, "PUT",
                          f"/zones/{ZONE_ID}/applications/{app['id']}/dependencies/{res['id']}")
        if status >= 300 and status != 409:
            must(status, dep, f"dependency {identifier}")
        print(f"dependency: {APP_NAME} -> {identifier}")

        env_lines.append(f"{ident_env}={identifier}")

    print("\nAdd to .env (client secret shown once):\n")
    print("\n".join(env_lines))


if __name__ == "__main__":
    main()
