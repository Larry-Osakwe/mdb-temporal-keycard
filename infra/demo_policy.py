"""Deny or restore the worker's access to one vaulted resource, live, for the demo.

  uv run python -m infra.demo_policy deny      # drop the dependency: the next mint is denied
  uv run python -m infra.demo_policy restore   # add it back: the next mint succeeds
  uv run python -m infra.demo_policy status    # show whether the dependency exists

Defaults to the MongoDB resource (KEYCARD_MONGODB_RESOURCE in .env). Pick
another with --resource <identifier>, e.g. the Voyage or OpenAI identifier.

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
import os
import sys
import urllib.parse

from .provision_keycard import APP_NAME, ZONE_ID, api, env_values, find_items, must


def resolve_app() -> dict:
    apps = must(api("GET", f"/zones/{ZONE_ID}/applications"), "list applications")
    hits = [a for a in find_items(apps) if a.get("name") == APP_NAME]
    if not hits:
        sys.exit(f"application {APP_NAME!r} not found in zone {ZONE_ID}; run provision_keycard first")
    return hits[0]


def resolve_resource(identifier: str) -> dict:
    found = must(
        api("GET", f"/zones/{ZONE_ID}/resources?filter[identifier]="
                   + urllib.parse.quote(identifier, safe="")),
        f"list resources for {identifier}",
    )
    hits = [r for r in find_items(found) if r.get("identifier") == identifier]
    if not hits:
        sys.exit(f"resource {identifier!r} not found in zone {ZONE_ID}")
    return hits[0]


def has_dependency(app_id: str, res_id: str) -> bool | None:
    """True/False when the API tells us; None when the list endpoint is unavailable."""
    deps = api("GET", f"/zones/{ZONE_ID}/applications/{app_id}/dependencies")
    if deps.get("_failed"):
        return None
    return any(
        d.get("id") == res_id or d.get("resource_id") == res_id for d in find_items(deps)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["deny", "restore", "status"])
    parser.add_argument(
        "--resource",
        default=os.environ.get("KEYCARD_MONGODB_RESOURCE") or env_values().get("KEYCARD_MONGODB_RESOURCE"),
        help="resource identifier (default: KEYCARD_MONGODB_RESOURCE)",
    )
    args = parser.parse_args()
    if not args.resource:
        sys.exit("no resource identifier: set KEYCARD_MONGODB_RESOURCE in .env or pass --resource")

    app = resolve_app()
    res = resolve_resource(args.resource)
    path = f"/zones/{ZONE_ID}/applications/{app['id']}/dependencies/{res['id']}"
    print(f"application: {APP_NAME} ({app['id']})")
    print(f"resource:    {args.resource} ({res['id']})")

    if args.action == "deny":
        out = api("DELETE", path)
        if out.get("_failed") and out.get("status") != 404:
            must(out, "remove dependency")
        print("dependency removed: the worker's next mint for this resource is DENIED")
    elif args.action == "restore":
        out = api("PUT", path)
        if out.get("_failed") and out.get("status") != 409:
            must(out, "add dependency")
        print("dependency restored: the worker's next mint for this resource succeeds")
    else:
        state = has_dependency(app["id"], res["id"])
        if state is None:
            print("dependency: unknown (list endpoint unavailable); use deny/restore, both are idempotent")
        else:
            print(f"dependency: {'PRESENT (access allowed)' if state else 'ABSENT (access denied)'}")


if __name__ == "__main__":
    main()
