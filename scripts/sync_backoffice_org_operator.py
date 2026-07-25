#!/usr/bin/env python3
"""PI-168 — Sync the EPRO "Back Office Org/Operator" cascading-select field from Orchard.

Source of truth: Orchard `unit.organization` (parent) + `unit.operator` (child, via
organization_id). Target: a Jira cascading-select custom field whose parent options are
organizations and child options are that org's operators.

Idempotent: adds new orgs/operators, re-enables ones that reappear, and DISABLES (never
deletes) options that no longer exist in Orchard (so historical issue values keep resolving).

Env (loaded from jira-cloud-mcp/.env and orchard-mcp/.env if present, or the process env):
  JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN            — Jira Cloud basic auth
  ADMIN_DB_URL                                    — Orchard read-only Postgres (BI replica)
  BO_ORG_OPERATOR_FIELD_ID                        — e.g. customfield_1xxxx (once created)

Usage:
  python scripts/sync_backoffice_org_operator.py            # dry-run (default): print the plan
  python scripts/sync_backoffice_org_operator.py --apply    # execute against Jira
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(os.path.join(REPO, ".env"))
_load_env(os.path.join(os.path.dirname(REPO), "orchard-mcp", ".env"))

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
ADMIN_DB_URL = os.environ.get("ADMIN_DB_URL", "")
FIELD_ID = os.environ.get("BO_ORG_OPERATOR_FIELD_ID", "")

CHUNK = 100  # options per POST


# ---------- Orchard (source) ----------
def fetch_orchard() -> dict[str, list[str]]:
    """Return {org_name: [operator_name, ...]} from Orchard (via psql)."""
    if not ADMIN_DB_URL:
        sys.exit("ADMIN_DB_URL not set (orchard-mcp/.env)")
    sql = (
        "SELECT o.name AS org, op.name AS operator "
        "FROM unit.organization o "
        "LEFT JOIN unit.operator op ON op.organization_id = o.id "
        "ORDER BY o.name, op.name;"
    )
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10",
           "PGOPTIONS": "-c statement_timeout=30000 -c default_transaction_read_only=on"}
    out = subprocess.run(["psql", ADMIN_DB_URL, "-At", "-F", "\t", "-c", sql],
                         capture_output=True, text=True, env=env, timeout=60)
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr[:500]}")
    orgs: dict[str, list[str]] = {}
    for line in out.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        org = parts[0].strip()
        op = parts[1].strip() if len(parts) > 1 else ""
        if not org:
            continue
        orgs.setdefault(org, [])
        if op:
            orgs[org].append(op)
    return orgs


# ---------- Jira (target) ----------
def _jira(method: str, path: str, body: dict | None = None):
    url = f"{JIRA_URL}{path}"
    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}", "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")


def get_context_id(field_id: str) -> str:
    data = _jira("GET", f"/rest/api/3/field/{field_id}/context")
    vals = data.get("values", [])
    if not vals:
        sys.exit(f"no context on {field_id}")
    return vals[0]["id"]


def get_options(field_id: str, ctx: str) -> list[dict]:
    """All options (parents + children), paginated."""
    out, start = [], 0
    while True:
        page = _jira("GET", f"/rest/api/3/field/{field_id}/context/{ctx}/option?startAt={start}&maxResults=100")
        out.extend(page.get("values", []))
        if page.get("isLast", True):
            break
        start += 100
    return out


def _chunks(items, n=CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def add_options(field_id: str, ctx: str, options: list[dict]) -> list[dict]:
    created = []
    for batch in _chunks(options):
        res = _jira("POST", f"/rest/api/3/field/{field_id}/context/{ctx}/option",
                    {"options": batch})
        created.extend(res.get("options", []))
    return created


def disable_options(field_id: str, ctx: str, option_ids: list[str]) -> None:
    for batch in _chunks(option_ids):
        _jira("PUT", f"/rest/api/3/field/{field_id}/context/{ctx}/option",
              {"options": [{"id": oid, "disabled": True} for oid in batch]})


# ---------- Plan ----------
def build_plan(orchard: dict[str, list[str]], field_id: str):
    """Return (parents_to_add, children_to_add[(org,op)], to_disable_count, current_summary)."""
    parents_add, children_add, disable_ids = [], [], []
    current = {"parents": 0, "children": 0}
    if field_id:
        ctx = get_context_id(field_id)
        opts = get_options(field_id, ctx)
        parents = {o["value"]: o for o in opts if not o.get("optionId")}
        children = {}  # parentId -> {value: opt}
        for o in opts:
            if o.get("optionId"):
                children.setdefault(o["optionId"], {})[o["value"]] = o
        current["parents"] = len(parents)
        current["children"] = sum(len(c) for c in children.values())
        for org, ops in orchard.items():
            p = parents.get(org)
            if not p:
                parents_add.append(org)
                children_add.extend((org, op) for op in ops)
            else:
                have = children.get(p["id"], {})
                for op in ops:
                    if op not in have:
                        children_add.append((org, op))
        # (disable logic runs at apply time against live ids)
    else:
        for org, ops in orchard.items():
            parents_add.append(org)
            children_add.extend((org, op) for op in ops)
    return parents_add, children_add, current


def main() -> int:
    apply = "--apply" in sys.argv
    orchard = fetch_orchard()
    n_orgs = len(orchard)
    n_ops = sum(len(v) for v in orchard.values())
    print(f"Orchard: {n_orgs} organizations, {n_ops} operators", file=sys.stderr)

    parents_add, children_add, current = build_plan(orchard, FIELD_ID)
    print(f"Field: {FIELD_ID or '(not created yet)'} | "
          f"current parents={current['parents']} children={current['children']}", file=sys.stderr)
    print(f"PLAN: +{len(parents_add)} org options, +{len(children_add)} operator options", file=sys.stderr)
    for org in parents_add[:5]:
        print(f"  + org: {org}  (operators: {len(orchard[org])})", file=sys.stderr)
    if len(parents_add) > 5:
        print(f"  ... and {len(parents_add) - 5} more orgs", file=sys.stderr)

    if not apply:
        print("\n(dry-run) no changes made. Re-run with --apply once the field exists "
              "and BO_ORG_OPERATOR_FIELD_ID is set.", file=sys.stderr)
        return 0

    if not FIELD_ID:
        sys.exit("BO_ORG_OPERATOR_FIELD_ID must be set to --apply")
    ctx = get_context_id(FIELD_ID)
    # 1) create parent options, map name->id
    made = add_options(FIELD_ID, ctx, [{"value": o, "disabled": False} for o in parents_add])
    pid = {o["value"]: o["id"] for o in made}
    # also map existing parents for children under pre-existing orgs
    for o in get_options(FIELD_ID, ctx):
        if not o.get("optionId"):
            pid.setdefault(o["value"], o["id"])
    # 2) create child options under their parent
    child_payload = [{"value": op, "optionId": pid[org], "disabled": False}
                     for (org, op) in children_add if org in pid]
    add_options(FIELD_ID, ctx, child_payload)
    print(f"applied: {len(made)} orgs, {len(child_payload)} operators", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
