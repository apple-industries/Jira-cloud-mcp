#!/usr/bin/env python3
"""Create TeamViewer user accounts for new staff, assigning a user ROLE.

TeamViewer deprecated the legacy "permissions" string on POST /users — creation now
requires userRoleId. Run with --list-roles to see what is available.

Uses a DEDICATED provisioning token, deliberately separate from the automation token:

  ~/.config/teamviewer/api.env          TEAMVIEWER_API_TOKEN
      The booth-automation token (C&C + groups). It is deployed in AWS Secrets Manager
      and used by the beta-lab /teamviewer/assign endpoint. Do NOT add user-management
      scopes to it — that would let the booth automation mint TeamViewer accounts.

  ~/.config/teamviewer/provisioning.env TEAMVIEWER_PROVISIONING_TOKEN
      A short-lived admin script token with "User management: create users".
      Local only, never deployed. Delete it in the console when onboarding is done.

This script refuses to run if the two tokens are the same value, so the separation
can't silently collapse.

Dry run by default:
  python3 teamviewer_create_users.py --list-roles
  python3 teamviewer_create_users.py --role "Tech support"
  python3 teamviewer_create_users.py --role "Tech support" --apply
  python3 teamviewer_create_users.py --ref-user jimalbert@faceplacephoto.com --apply
  python3 teamviewer_create_users.py --user "TECH SUPXY:someone@appleindustries.com" --apply

Existing users are skipped, so re-running is safe.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://webapi.teamviewer.com/api/v1"
UA = "curl/8.7.1"  # Cloudflare/TeamViewer dislike the default Python-urllib UA
CFG = os.path.expanduser("~/.config/teamviewer")
PROV_ENV = os.path.join(CFG, "provisioning.env")
AUTO_ENV = os.path.join(CFG, "api.env")

DEFAULT_USERS = [
    ("TECH SUPIM", "IlyasMohamed@appleindustries.com"),
    ("TECH SUPRL", "RobertLewis@appleindustries.com"),
]
DEFAULT_REF = "jonathanmccool@faceplacephoto.com"


def load_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def api(token, method, path, body=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}



def ci(d, *names):
    """Case-insensitive key lookup — TeamViewer returns 'Roles'/'Name'/'Id' (capitalised)
    from /userroles but lowercase keys elsewhere."""
    if not isinstance(d, dict):
        return None
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def role_name(r):
    return ci(r, "Name", "roleName") or "?"


def role_id(r):
    return ci(r, "Id", "userRoleId", "roleId")


def perm_summary(r, limit=6):
    """Compact 'what does this role actually allow' line."""
    perms = ci(r, "Permissions") or {}
    on = [k for k, v in perms.items() if v is True]
    if not on:
        return "(no permissions enabled)"
    return f"{len(on)} enabled: " + ", ".join(on[:limit]) + (" …" if len(on) > limit else "")


def find_user_role(api_fn, token, email, roles):
    """Best-effort: which role is this user assigned? Tries the user record first, then
    each role's account list. Returns (role_id, role_name, how) or (None, None, why)."""
    s, u = api_fn(token, "GET", f"/users?email={email}")
    stub = (u.get("users") or [None])[0]
    if not stub:
        return None, None, f"user {email} not found"
    s, det = api_fn(token, "GET", f"/users/{stub['id']}")
    for k, v in (det or {}).items():
        if "role" in k.lower() and v:
            match = next((r for r in roles if str(role_id(r)) == str(v)), None)
            return str(v), (role_name(match) if match else "(unknown)"), f"user record field {k!r}"
    for r in roles:
        rid = role_id(r)
        s, acc = api_fn(token, "GET", f"/userroles/{rid}/accounts")
        if s != 200:
            continue
        ids = json.dumps(acc)
        if stub["id"] in ids or email.lower() in ids.lower():
            return rid, role_name(r), "role account list"
    return None, None, ("could not determine the role from the API "
                        f"(user {stub['id']} not found in any role's account list)")


def main(argv):
    p = argparse.ArgumentParser(description="Create TeamViewer users from a reference user's permissions")
    p.add_argument("--apply", action="store_true", help="actually create (default: dry run)")
    p.add_argument("--ref-user", default=DEFAULT_REF, help=f"copy permissions from (default {DEFAULT_REF})")
    p.add_argument("--user", action="append", metavar="NAME:EMAIL",
                   help="user to create; repeatable. Defaults to the two new UK support reps.")
    p.add_argument("--password", help="temp password (default: TEAMVIEWER_NEW_USER_PASSWORD from provisioning.env)")
    p.add_argument("--role", help="user role NAME to assign (see --list-roles)")
    p.add_argument("--role-id", help="user role ID to assign (overrides --role)")
    p.add_argument("--list-roles", action="store_true", help="list available user roles and exit")
    p.add_argument("--copy-role-from", metavar="EMAIL",
                   help="assign whatever role this existing user has (e.g. jonathanmccool@faceplacephoto.com)")
    a = p.parse_args(argv)

    prov = load_env(PROV_ENV)
    token = prov.get("TEAMVIEWER_PROVISIONING_TOKEN", "").strip()
    if not token:
        print(f"ERROR: no TEAMVIEWER_PROVISIONING_TOKEN in {PROV_ENV}\n"
              f"  Create a script token at login.teamviewer.com -> profile -> Edit profile -> Apps\n"
              f"  with 'User management: create users', paste it into that file, then re-run.")
        return 1
    auto = load_env(AUTO_ENV).get("TEAMVIEWER_API_TOKEN", "").strip()
    if auto and auto == token:
        print("ERROR: the provisioning token is the SAME as the deployed automation token in\n"
              f"  {AUTO_ENV}. Keep them separate — the automation token is deployed in AWS and\n"
              "  must not carry user-management scopes. Mint a separate admin token.")
        return 1

    password = a.password or prov.get("TEAMVIEWER_NEW_USER_PASSWORD", "").strip()
    if not password:
        print("ERROR: no password (set TEAMVIEWER_NEW_USER_PASSWORD in provisioning.env or pass --password)")
        return 1

    users = []
    for spec in (a.user or []):
        if ":" not in spec:
            print(f"ERROR: --user must be NAME:EMAIL, got {spec!r}"); return 1
        name, email = spec.split(":", 1)
        users.append((name.strip(), email.strip()))
    users = users or DEFAULT_USERS

    # TeamViewer deprecated the legacy "permissions" string on POST /users:
    #   "Legacy permissions are deprecated, please use the userRoleId parameter"
    # so creation now needs a user ROLE. Roles live at GET /userroles (needs a user-
    # management scope, which is why this uses the provisioning token, not api.env's).
    s, roles_body = api(token, "GET", "/userroles")
    roles = ci(roles_body, "Roles", "userRoles") or []
    if s != 200:
        print(f"ERROR: GET /userroles failed: HTTP {s} {roles_body}\n"
              "  The provisioning token needs a user-management scope that can read roles.")
        return 1

    if a.copy_role_from and not (a.role or a.role_id):
        rid, rname, how = find_user_role(api, token, a.copy_role_from, roles)
        if not rid:
            print(f"ERROR: {how}\n  Pick one explicitly with --role/--role-id (see --list-roles).")
            return 1
        print(f"copying role from {a.copy_role_from}: {rname} [{rid}]  (via {how})")
        a.role_id = rid

    if a.list_roles or not (a.role or a.role_id):
        if not roles:
            print("No roles parsed. Raw GET /userroles response:")
            print("  " + json.dumps(roles_body)[:1500])
            print("\nIf that payload is genuinely empty, no user roles are defined in the tenant "
                  "yet —\ncreate one in the Management Console (Company administration -> User "
                  "roles), then re-run.")
            return 1
        print(f"available user roles ({len(roles)}):")
        for r in roles:
            print(f"  {str(role_id(r)):<38} {role_name(r)}")
            print(f"  {'':<38} {perm_summary(r)}")
        if a.list_roles:
            return 0
        print("\nERROR: pick one with --role \"<name>\" (or --role-id <id>) and re-run.")
        return 1

    # NB: distinct local names — role_id()/role_name() are module-level helpers and
    # assigning to those names here would shadow them inside main().
    if a.role_id:
        sel_id = a.role_id
        sel_name = next((role_name(r) for r in roles if str(role_id(r)) == a.role_id), "(unknown)")
    else:
        matches = [r for r in roles if role_name(r).strip().lower() == a.role.strip().lower()]
        if len(matches) != 1:
            print(f"ERROR: --role {a.role!r} matched {len(matches)} roles. Available:")
            for r in roles:
                print(f"  {role_name(r)}")
            return 1
        sel_id, sel_name = role_id(matches[0]), role_name(matches[0])

    # reference user is now informational only (its legacy permissions can't be assigned)
    s, u = api(token, "GET", f"/users?email={a.ref_user}")
    stub = (u.get("users") or [None])[0]
    if stub:
        s, ref = api(token, "GET", f"/users/{stub['id']}")
        print(f"reference : {ref.get('name')} <{a.ref_user}>  (legacy perms {ref.get('permissions')!r}, "
              f"license {ref.get('activated_license_name')} / {ref.get('activated_subLicense_name')})")
    print(f"role      : {sel_name}  [{sel_id}]")
    print(f"mode      : {'APPLY' if a.apply else 'DRY RUN'}\n")

    created = skipped = failed = 0
    for name, email in users:
        s, ex = api(token, "GET", f"/users?email={email}")
        if ex.get("users"):
            print(f"SKIP   {name:<12} {email:<38} already exists ({ex['users'][0]['id']})")
            skipped += 1
            continue
        if not a.apply:
            print(f"WOULD  {name:<12} {email:<38} role={sel_name!r}")
            continue
        s, r = api(token, "POST", "/users",
                   {"email": email, "name": name, "password": password,
                    "userRoleId": sel_id, "language": "en"})
        if s == 200 and r.get("id"):
            print(f"CREATE {name:<12} {email:<38} -> {r['id']}")
            created += 1
        else:
            print(f"FAIL   {name:<12} {email:<38} HTTP {s} {r}")
            failed += 1

    if a.apply:
        print(f"\ncreated={created} skipped={skipped} failed={failed}")
        if created:
            print("\nNEXT: 1) have each user reset the temp password immediately\n"
                  "      2) share the booth groups with them, or they will see an EMPTY\n"
                  "         Computers & Contacts list (groups are shared per-user).")
    else:
        print("\n(no changes — re-run with --apply)")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
