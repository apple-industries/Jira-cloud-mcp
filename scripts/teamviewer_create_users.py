#!/usr/bin/env python3
"""Create TeamViewer user accounts for new staff, copying an existing user's permissions.

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
  python3 teamviewer_create_users.py
  python3 teamviewer_create_users.py --apply
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


def main(argv):
    p = argparse.ArgumentParser(description="Create TeamViewer users from a reference user's permissions")
    p.add_argument("--apply", action="store_true", help="actually create (default: dry run)")
    p.add_argument("--ref-user", default=DEFAULT_REF, help=f"copy permissions from (default {DEFAULT_REF})")
    p.add_argument("--user", action="append", metavar="NAME:EMAIL",
                   help="user to create; repeatable. Defaults to the two new UK support reps.")
    p.add_argument("--password", help="temp password (default: TEAMVIEWER_NEW_USER_PASSWORD from provisioning.env)")
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

    # ?email= returns only {id,name,email} — the full record (incl. permissions) needs /users/{id}
    s, u = api(token, "GET", f"/users?email={a.ref_user}")
    if s != 200:
        print(f"ERROR: lookup of reference user failed: HTTP {s} {u}"); return 1
    stub = (u.get("users") or [None])[0]
    if not stub:
        print(f"ERROR: reference user {a.ref_user} not found"); return 1
    s, ref = api(token, "GET", f"/users/{stub['id']}")
    if s != 200:
        print(f"ERROR: fetching reference user detail failed: HTTP {s} {ref}"); return 1
    perms = ref.get("permissions")

    print(f"reference : {ref['name']} <{a.ref_user}>")
    print(f"  permissions -> {perms!r}")
    print(f"  license     -> {ref.get('activated_license_name')} / {ref.get('activated_subLicense_name')}")
    print(f"mode      : {'APPLY' if a.apply else 'DRY RUN'}\n")

    created = skipped = failed = 0
    for name, email in users:
        s, ex = api(token, "GET", f"/users?email={email}")
        if ex.get("users"):
            print(f"SKIP   {name:<12} {email:<38} already exists ({ex['users'][0]['id']})")
            skipped += 1
            continue
        if not a.apply:
            print(f"WOULD  {name:<12} {email:<38} perms={perms!r}")
            continue
        s, r = api(token, "POST", "/users",
                   {"email": email, "name": name, "password": password,
                    "permissions": perms, "language": "en"})
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
