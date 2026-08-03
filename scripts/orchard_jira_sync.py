#!/usr/bin/env python3
"""orchard_jira_sync — one job to sync all Orchard reference data into Jira.

Modules (run in this order; products depends on content-codes for the reference):
  content-codes  -> Assets object type "Content Code" (ORCH schema)   [platform.content_code]
  products       -> Assets object type "Product"      (ORCH schema)   [unit.product]
  org-operator   -> Back Office Org/Operator cascade FIELD options     [unit.organization/operator]
                    (delegates to sync_backoffice_org_operator.py — unchanged)

Assets objects are upserted (idempotent) keyed on the "Orchard ID" attribute.

Env (loaded from jira-cloud-mcp/.env and orchard-mcp/.env):
  JIRA_EMAIL, JIRA_API_TOKEN     — Jira/Assets basic auth
  ASSETS_WORKSPACE_ID            — Assets workspace (persisted in .env)
  ADMIN_DB_URL                   — Orchard read-only Postgres (BI replica)

Usage:
  python scripts/orchard_jira_sync.py                       # dry-run, all modules
  python scripts/orchard_jira_sync.py --apply               # apply, all modules
  python scripts/orchard_jira_sync.py --only content-codes,products   # subset
  python scripts/orchard_jira_sync.py --only products --apply
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                       # jira-cloud-mcp
DEV = os.path.dirname(REPO)                        # ~/dev


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(os.path.join(REPO, ".env"))
_load_env(os.path.join(DEV, "orchard-mcp", ".env"))

EMAIL = os.environ.get("JIRA_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")
WORKSPACE = os.environ.get("ASSETS_WORKSPACE_ID", "")
ADMIN_DB_URL = os.environ.get("ADMIN_DB_URL", "")
AUTH = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
ASSETS_BASE = f"https://api.atlassian.com/jsm/assets/workspace/{WORKSPACE}/v1"


def _schema_map() -> dict:
    """Load the ORCH schema id-map (canonical copy in jira-infrastructure)."""
    for p in (os.path.join(HERE, "orch-schema.json"),
              os.path.join(DEV, "jira-infrastructure", "authored", "assets", "orch-schema.json")):
        if os.path.exists(p):
            return json.load(open(p))
    sys.exit("orch-schema.json not found (jira-cloud-mcp/scripts or jira-infrastructure/authored/assets)")


MAP = _schema_map()
CC = MAP["objectTypes"]["contentCode"]
PROD = MAP["objectTypes"]["product"]

# Accumulates per-module results for the run-status file (read by the beta-lab status page).
_SUMMARY: list[dict] = []
STATUS_FILE = os.environ.get("ORCHARD_SYNC_STATUS_FILE", os.path.join(HERE, ".last_sync.json"))


# ---------- Orchard (source) ----------
def orchard_rows(sql: str) -> list[list[str]]:
    if not ADMIN_DB_URL:
        sys.exit("ADMIN_DB_URL not set (orchard-mcp/.env)")
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10",
           "PGOPTIONS": "-c statement_timeout=30000 -c default_transaction_read_only=on"}
    out = subprocess.run(["psql", ADMIN_DB_URL, "-At", "-F", "\t", "-c", sql],
                         capture_output=True, text=True, env=env, timeout=60)
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr[:500]}")
    return [line.split("\t") for line in out.stdout.splitlines() if line]


# ---------- Assets (target) ----------
def _assets(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ASSETS_BASE + path, data=data, method=method, headers={
        "Authorization": f"Basic {AUTH}", "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")


def aql_all(object_type_id: str) -> list[dict]:
    """Every object of a type, with attributes, paged."""
    out, page = [], 1
    while True:
        d = _assets("POST", f"/object/aql?page={page}&resultPerPage=100&includeAttributes=true",
                    {"qlQuery": f"objectTypeId = {object_type_id}"})
        vals = d.get("values") or d.get("objectEntries") or []
        out.extend(vals)
        if len(vals) < 100:
            break
        page += 1
    return out


def attr_value(obj: dict, attr_id: str):
    for a in obj.get("attributes", []):
        if str(a.get("objectTypeAttributeId")) == str(attr_id):
            for v in a.get("objectAttributeValues", []):
                if v.get("value") is not None:
                    return v["value"]
                ref = v.get("referencedObject") or {}
                if ref.get("id"):
                    return ref["id"]
    return None


def _attrs(pairs: dict) -> list[dict]:
    """pairs {attribute_id: value}; skips None. Values are stringified."""
    return [{"objectTypeAttributeId": str(aid),
             "objectAttributeValues": [{"value": str(val)}]}
            for aid, val in pairs.items() if val is not None and val != ""]


def upsert(object_type_id: str, orchard_id_attr: str, existing: dict,
           orchard_id: str, pairs: dict, apply: bool) -> tuple[str, str]:
    """Create or full-update an object keyed on Orchard ID. Returns (action, object_id)."""
    obj_id = existing.get(orchard_id)
    attrs = _attrs(pairs)
    if obj_id:
        if apply:
            _assets("PUT", f"/object/{obj_id}", {"attributes": attrs})
        return "update", obj_id
    if apply:
        res = _assets("POST", "/object/create", {"objectTypeId": object_type_id, "attributes": attrs})
        return "create", res.get("id", "")
    return "create", ""


def _existing_map(object_type_id: str, orchard_id_attr: str) -> dict:
    """{orchard_id: assets_object_id} for a type."""
    m = {}
    for o in aql_all(object_type_id):
        oid = attr_value(o, orchard_id_attr)
        if oid:
            m[str(oid)] = o["id"]
    return m


# ---------- modules ----------
def sync_content_codes(apply: bool) -> dict:
    """Returns {content_code_orchard_id: assets_object_id} for the products module."""
    rows = orchard_rows(
        "SELECT id, code, name, coalesce(licensed::text,'false'), coalesce(experience_type,'') "
        "FROM platform.content_code ORDER BY code")
    existing = _existing_map(CC["id"], CC["attributes"]["Orchard ID"])
    a = CC["attributes"]
    creates = updates = 0
    cc_map = dict(existing)
    for oid, code, name, licensed, exp in rows:
        pairs = {a["Name"]: name, a["Code"]: code,
                 a["Licensed"]: "true" if licensed in ("t", "true", "True") else "false",
                 a["Orchard ID"]: oid}
        if exp:
            pairs[a["Experience Type"]] = exp
        action, obj_id = upsert(CC["id"], a["Orchard ID"], existing, oid, pairs, apply)
        if obj_id:
            cc_map[oid] = obj_id
        creates += action == "create"
        updates += action == "update"
    print(f"[content-codes] source={len(rows)} existing={len(existing)} "
          f"-> create={creates} update={updates}" + ("" if apply else "  (dry-run)"), file=sys.stderr)
    _SUMMARY.append({"module": "content-codes", "source": len(rows), "existing": len(existing),
                     "create": creates, "update": updates, "ok": True})
    return cc_map


def sync_products(apply: bool, cc_map: dict | None = None) -> None:
    if cc_map is None:  # standalone run: resolve CC objects by their Orchard ID
        cc_map = _existing_map(CC["id"], CC["attributes"]["Orchard ID"])
    rows = orchard_rows(
        "SELECT id, code, name, coalesce(default_content_code_id::text,'') "
        "FROM unit.product ORDER BY code")
    existing = _existing_map(PROD["id"], PROD["attributes"]["Orchard ID"])
    a = PROD["attributes"]
    creates = updates = 0
    missing_ref = []
    for oid, code, name, dcc in rows:
        pairs = {a["Name"]: name, a["Code"]: code, a["Orchard ID"]: oid}
        if dcc:
            ref = cc_map.get(dcc)
            if ref:
                pairs[a["Default Content Code"]] = ref
            else:
                missing_ref.append(code)
        action, _ = upsert(PROD["id"], a["Orchard ID"], existing, oid, pairs, apply)
        creates += action == "create"
        updates += action == "update"
    print(f"[products]      source={len(rows)} existing={len(existing)} "
          f"-> create={creates} update={updates}" + ("" if apply else "  (dry-run)"), file=sys.stderr)
    # Only a real problem once content codes exist; in a first dry-run the map is empty by design.
    warn = missing_ref if (missing_ref and (apply or cc_map)) else []
    if warn:
        print(f"[products]      WARN default-content-code not found for: {warn} "
              f"(run content-codes first)", file=sys.stderr)
    _SUMMARY.append({"module": "products", "source": len(rows), "existing": len(existing),
                     "create": creates, "update": updates, "warnings": warn, "ok": not warn})


def sync_unit_product(apply: bool) -> None:
    """Auto-populate the Unit Setup 'Orchard Product' asset from each serial's product code
    (delegates to backfill_orchard_product.py — reuses the proven REST write). Idempotent:
    only touches open Unit Setups where the asset is empty."""
    script = os.path.join(HERE, "backfill_orchard_product.py")
    cmd = [sys.executable, script] + (["--apply"] if apply else [])
    print(f"[unit-product]  delegating to backfill_orchard_product.py"
          f"{' --apply' if apply else ' (dry-run)'}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    for ln in tail:
        print(f"[unit-product]  {ln}", file=sys.stderr)
    _SUMMARY.append({"module": "unit-product", "delegated": True,
                     "returncode": proc.returncode, "tail": tail, "ok": proc.returncode == 0})


def sync_sim_types(apply: bool) -> None:
    """Reconcile the 'SIM Type' field options to Orchard's SimType enum values
    (delegates to sync_sim_type_field.py). Near-static — cheap/idempotent to run nightly."""
    script = os.path.join(HERE, "sync_sim_type_field.py")
    cmd = [sys.executable, script] + (["--apply"] if apply else [])
    print(f"[sim-types]     delegating to sync_sim_type_field.py"
          f"{' --apply' if apply else ' (dry-run)'}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    for ln in tail:
        print(f"[sim-types]     {ln}", file=sys.stderr)
    _SUMMARY.append({"module": "sim-types", "delegated": True,
                     "returncode": proc.returncode, "tail": tail, "ok": proc.returncode == 0})


def sync_org_operator(apply: bool) -> None:
    """Delegate to the existing, proven field-sync script (unchanged)."""
    script = os.path.join(HERE, "sync_backoffice_org_operator.py")
    cmd = [sys.executable, script] + (["--apply"] if apply else [])
    print(f"[org-operator]  delegating to sync_backoffice_org_operator.py"
          f"{' --apply' if apply else ' (dry-run)'}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    for ln in tail:
        print(f"[org-operator]  {ln}", file=sys.stderr)
    _SUMMARY.append({"module": "org-operator", "delegated": True,
                     "returncode": proc.returncode, "tail": tail, "ok": proc.returncode == 0})


MODULES = {"content-codes": None, "products": None, "unit-product": None,
           "sim-types": None, "org-operator": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute writes (default: dry-run)")
    ap.add_argument("--only", default="", help="comma list: content-codes,products,org-operator")
    args = ap.parse_args()
    if not WORKSPACE:
        sys.exit("ASSETS_WORKSPACE_ID not set")
    only = [m.strip() for m in args.only.split(",") if m.strip()] or list(MODULES)
    bad = [m for m in only if m not in MODULES]
    if bad:
        sys.exit(f"unknown module(s): {bad}; valid: {list(MODULES)}")

    print(f"=== orchard_jira_sync {'APPLY' if args.apply else 'DRY-RUN'} | modules={only} ===",
          file=sys.stderr)
    cc_map = None
    err = None
    try:
        if "content-codes" in only:
            cc_map = sync_content_codes(args.apply)
        if "products" in only:
            sync_products(args.apply, cc_map)
        if "unit-product" in only:
            sync_unit_product(args.apply)
        if "sim-types" in only:
            sync_sim_types(args.apply)
        if "org-operator" in only:
            sync_org_operator(args.apply)
    except SystemExit as e:
        err = str(e)
    except Exception as e:  # noqa: BLE001 — record any failure in the status file
        err = f"{type(e).__name__}: {e}"

    status = {
        "finishedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry-run",
        "modules": only,
        "results": _SUMMARY,
        "error": err,
        "ok": err is None and all(m.get("ok", True) for m in _SUMMARY),
    }
    try:
        json.dump(status, open(STATUS_FILE, "w"), indent=2)
        print(f"=== wrote status -> {STATUS_FILE} (ok={status['ok']}) ===", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"WARN could not write status file: {e}", file=sys.stderr)
    if err:
        sys.exit(err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
