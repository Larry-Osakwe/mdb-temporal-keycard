"""Deny or restore the worker's access to one vaulted resource, live, for the demo.

  uv run python -m infra.demo_policy deny      # drop the dependency: the next mint is denied
  uv run python -m infra.demo_policy restore   # add it back: the next mint succeeds
  uv run python -m infra.demo_policy status    # show whether the dependency exists

Defaults to the MongoDB resource (KEYCARD_MONGODB_RESOURCE in .env). Pick
another with --resource <identifier>, e.g. the Voyage or OpenAI identifier.
The agent UI's Keycard switch calls the same functions through agent/api.py.

Rides the signed-in Keycard CLI session (`keycard auth signin`) through
`keycard agent api`, exactly like provision_keycard.py, so nothing here
touches tokens. Prints ids and statuses only.

Removing a dependency is the policy statement "this application may not be
issued this credential": the worker's next @grant for the resource fails
with the non-retryable KeycardAccessDenied, and the agent's tools report the
denial to the model. Restoring takes effect on the very next mint, because
every tool call mints its own credential.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

from .provision_keycard import APP_NAME, ZONE_ID, api, env_values, find_items


class PolicyError(RuntimeError):
    """A Keycard API call failed; the message is safe to show a user."""


def _ok(payload: dict, what: str) -> dict:
    if payload.get("_failed"):
        raise PolicyError(f"{what} failed: {json.dumps(payload)[:300]}")
    return payload


def default_resource() -> str | None:
    return os.environ.get("KEYCARD_MONGODB_RESOURCE") or env_values().get("KEYCARD_MONGODB_RESOURCE")


def resolve_app() -> dict:
    apps = _ok(api("GET", f"/zones/{ZONE_ID}/applications"), "list applications")
    hits = [a for a in find_items(apps) if a.get("name") == APP_NAME]
    if not hits:
        raise LookupError(f"application {APP_NAME!r} not found in zone {ZONE_ID}; run provision_keycard first")
    return hits[0]


def resolve_resource(identifier: str) -> dict:
    found = _ok(
        api("GET", f"/zones/{ZONE_ID}/resources?filter[identifier]="
                   + urllib.parse.quote(identifier, safe="")),
        f"list resources for {identifier}",
    )
    hits = [r for r in find_items(found) if r.get("identifier") == identifier]
    if not hits:
        raise LookupError(f"resource {identifier!r} not found in zone {ZONE_ID}")
    return hits[0]


def has_dependency(app_id: str, res_id: str) -> bool | None:
    """True/False when the API tells us; None when the list endpoint is unavailable."""
    deps = api("GET", f"/zones/{ZONE_ID}/applications/{app_id}/dependencies")
    if deps.get("_failed"):
        return None
    return any(
        d.get("id") == res_id or d.get("resource_id") == res_id for d in find_items(deps)
    )


def access_state(identifier: str) -> dict:
    """Whether the worker application may currently be issued this resource's credential."""
    app = resolve_app()
    res = resolve_resource(identifier)
    return {
        "application": APP_NAME,
        "application_id": app["id"],
        "resource": identifier,
        "resource_id": res["id"],
        "allowed": has_dependency(app["id"], res["id"]),
    }


def set_access(identifier: str, allowed: bool) -> dict:
    """Add (allow) or remove (deny) the dependency. Idempotent in both directions."""
    app = resolve_app()
    res = resolve_resource(identifier)
    path = f"/zones/{ZONE_ID}/applications/{app['id']}/dependencies/{res['id']}"
    if allowed:
        out = api("PUT", path)
        if out.get("_failed") and out.get("status") != 409:
            _ok(out, "add dependency")
    else:
        out = api("DELETE", path)
        if out.get("_failed") and out.get("status") != 404:
            _ok(out, "remove dependency")
    return {
        "application": APP_NAME,
        "application_id": app["id"],
        "resource": identifier,
        "resource_id": res["id"],
        "allowed": allowed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["deny", "restore", "status"])
    parser.add_argument("--resource", default=default_resource(),
                        help="resource identifier (default: KEYCARD_MONGODB_RESOURCE)")
    args = parser.parse_args()
    if not args.resource:
        sys.exit("no resource identifier: set KEYCARD_MONGODB_RESOURCE in .env or pass --resource")

    try:
        if args.action == "status":
            state = access_state(args.resource)
        else:
            state = set_access(args.resource, allowed=(args.action == "restore"))
    except (LookupError, PolicyError) as e:
        sys.exit(str(e))

    print(f"application: {state['application']} ({state['application_id']})")
    print(f"resource:    {state['resource']} ({state['resource_id']})")
    if args.action == "deny":
        print("dependency removed: the worker's next mint for this resource is DENIED")
    elif args.action == "restore":
        print("dependency restored: the worker's next mint for this resource succeeds")
    elif state["allowed"] is None:
        print("dependency: unknown (list endpoint unavailable); use deny/restore, both are idempotent")
    else:
        print(f"dependency: {'PRESENT (access allowed)' if state['allowed'] else 'ABSENT (access denied)'}")


if __name__ == "__main__":
    main()
