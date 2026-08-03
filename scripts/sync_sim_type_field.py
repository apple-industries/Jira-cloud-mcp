#!/usr/bin/env python3
"""Reconcile the Jira "SIM Type" field (customfield_10192) options to Orchard's SimType
ENUM VALUES, so the "Assign SIM to Unit" automation sends a value the import accepts.

SimType is a code enum (no DB table), so the source is a curated list of the known enum
values, UNIONed with any value actually in use in unit.unit_sim (so a newly-used type can't
be missed). Desired options are enabled; existing options not in the desired set are DISABLED
(never deleted — historical ticket values keep resolving). Near-static; run quarterly.

Env (jira-cloud-mcp/.env): JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN. ADMIN_DB_URL (orchard-mcp/.env)
optional — used to union in in-use types.

Usage:
  python scripts/sync_sim_type_field.py            # dry-run
  python scripts/sync_sim_type_field.py --apply
"""
from __future__ import annotations
import argparse, base64, json, os, subprocess, sys, urllib.request, urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = os.path.dirname(REPO)


def _load_env(p):
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(os.path.join(REPO, ".env"))
_load_env(os.path.join(DEV, "orchard-mcp", ".env"))

JU = os.environ["JIRA_URL"].rstrip("/")
EM = os.environ["JIRA_EMAIL"]; TK = os.environ["JIRA_API_TOKEN"]
ADMIN_DB_URL = os.environ.get("ADMIN_DB_URL", "")
AUTH = base64.b64encode(f"{EM}:{TK}".encode()).decode()
HDRS = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json", "Accept": "application/json"}

FIELD = "customfield_10192"
# Known Orchard SimType enum values (from the feign constant / Orchard SIM Type dropdown).
CURATED = ["FLO_LIVE_STANDARD", "KORE_RUSH_GLOBAL", "KORE_SUPER_SIM", "SIMON_IOT"]


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(JU + path, data=data, method=method, headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")


def in_use_types() -> list[str]:
    if not ADMIN_DB_URL:
        return []
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10",
           "PGOPTIONS": "-c statement_timeout=15000 -c default_transaction_read_only=on"}
    out = subprocess.run(["psql", ADMIN_DB_URL, "-At", "-c",
                          "SELECT DISTINCT type FROM unit.unit_sim WHERE type IS NOT NULL"],
                         capture_output=True, text=True, env=env, timeout=30)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()] if out.returncode == 0 else []


def main() -> int:
    apply = "--apply" in sys.argv
    desired = sorted(set(CURATED) | set(in_use_types()))
    print(f"desired SIM Type values ({len(desired)}): {desired}", file=sys.stderr)

    ctx = _req("GET", f"/rest/api/3/field/{FIELD}/context")
    context_id = ctx["values"][0]["id"]
    opts, start = [], 0
    while True:
        page = _req("GET", f"/rest/api/3/field/{FIELD}/context/{context_id}/option?startAt={start}&maxResults=100")
        opts.extend(page.get("values", []))
        if page.get("isLast", True):
            break
        start += 100
    by_val = {o["value"]: o for o in opts}

    to_add = [v for v in desired if v not in by_val]
    to_disable = [o for o in opts if o["value"] not in desired and not o.get("disabled")]
    to_reenable = [o for o in opts if o["value"] in desired and o.get("disabled")]

    print(f"current options: {[o['value'] for o in opts]}", file=sys.stderr)
    print(f"  + add:      {to_add}", file=sys.stderr)
    print(f"  ~ disable:  {[o['value'] for o in to_disable]}", file=sys.stderr)
    print(f"  ^ reenable: {[o['value'] for o in to_reenable]}", file=sys.stderr)

    if not apply:
        print("(dry-run) no changes.", file=sys.stderr)
        return 0

    if to_add:
        _req("POST", f"/rest/api/3/field/{FIELD}/context/{context_id}/option",
             {"options": [{"value": v, "disabled": False} for v in to_add]})
    changes = [{"id": o["id"], "value": o["value"], "disabled": True} for o in to_disable] \
        + [{"id": o["id"], "value": o["value"], "disabled": False} for o in to_reenable]
    if changes:
        _req("PUT", f"/rest/api/3/field/{FIELD}/context/{context_id}/option", {"options": changes})
    print(f"applied: +{len(to_add)} added, {len(to_disable)} disabled, {len(to_reenable)} re-enabled",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
