#!/usr/bin/env python3
"""Backfill the EPRO Unit Setup "Orchard Product" Assets field (customfield_12249) from the
serial number's product code, so PI-167's asset-driven import works on existing units.

Matching: the serial encodes the product as its SUFFIX (e.g. ...MBP, ...DLX). Orchard product
codes are 2-4 chars (MB=2, most=3, UNKN=4), so we match the Product whose Code the serial
ENDS WITH, preferring the longest match. Serials that match no product are reported, not guessed.

Targets: open EPRO Unit Setups (statusCategory != Done) with a Serial Number set and the
Orchard Product field empty.

Env (jira-cloud-mcp/.env): JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, ASSETS_WORKSPACE_ID.

Usage:
  python scripts/backfill_orchard_product.py                 # dry-run, all matching open units
  python scripts/backfill_orchard_product.py --one EPRO-4025 # dry-run a single issue
  python scripts/backfill_orchard_product.py --one EPRO-4025 --apply   # write that one (verify shape)
  python scripts/backfill_orchard_product.py --apply         # write the full batch
"""
from __future__ import annotations
import argparse, base64, json, os, sys, urllib.request, urllib.error

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
JU = os.environ["JIRA_URL"].rstrip("/")
EM = os.environ["JIRA_EMAIL"]; TK = os.environ["JIRA_API_TOKEN"]
WS = os.environ["ASSETS_WORKSPACE_ID"]
AUTH = base64.b64encode(f"{EM}:{TK}".encode()).decode()
HDRS = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json", "Accept": "application/json"}

PROD_OT = "50"          # Product object type id (see orch-schema.json)
PROD_CODE_ATTR = "178"  # Product.Code attribute id
CF = "customfield_12249"


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read().decode()[:600]}")


def product_code_map() -> dict:
    """{code: assets_object_id} for every Orchard Product object."""
    base = f"https://api.atlassian.com/jsm/assets/workspace/{WS}/v1"
    out, page = {}, 1
    while True:
        d = _req("POST", f"{base}/object/aql?page={page}&resultPerPage=100&includeAttributes=true",
                 {"qlQuery": f"objectTypeId = {PROD_OT}"})
        vals = d.get("values") or d.get("objectEntries") or []
        for o in vals:
            code = None
            for a in o.get("attributes", []):
                if str(a.get("objectTypeAttributeId")) == PROD_CODE_ATTR:
                    vv = a.get("objectAttributeValues") or []
                    code = vv[0].get("value") if vv else None
            if code:
                out[code] = o["id"]
        if len(vals) < 100:
            break
        page += 1
    return out


def match_code(serial: str, codes: list[str]) -> str | None:
    """Longest product code that the serial ends with."""
    cands = [c for c in codes if serial and serial.endswith(c)]
    return max(cands, key=len) if cands else None


def find_targets(one: str | None) -> list[dict]:
    if one:
        jql = f'key = {one}'
    else:
        jql = ('project = EPRO AND issuetype = "Unit Setup" AND statusCategory != Done '
               f'AND "Serial Number" is not EMPTY AND cf[12249] is EMPTY')
    out, token = [], None
    while True:
        body = {"jql": jql, "maxResults": 100,
                "fields": ["customfield_10068", CF, "summary"]}
        if token:
            body["nextPageToken"] = token
        d = _req("POST", f"{JU}/rest/api/3/search/jql", body)
        out.extend(d.get("issues", []))
        token = d.get("nextPageToken")
        if not token:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--one", default="")
    args = ap.parse_args()

    codes = product_code_map()
    print(f"Orchard products: {len(codes)} codes", file=sys.stderr)
    targets = find_targets(args.one or None)
    print(f"Candidate Unit Setups: {len(targets)}", file=sys.stderr)

    matched = unmatched = wrote = skipped = 0
    for it in targets:
        key = it["key"]; f = it.get("fields", {})
        if f.get(CF):
            skipped += 1
            continue
        serial = (f.get("customfield_10068") or "").strip()
        code = match_code(serial, list(codes))
        if not code:
            unmatched += 1
            print(f"  UNMATCHED {key}: serial={serial!r} (no product code is a suffix)", file=sys.stderr)
            continue
        matched += 1
        obj_id = codes[code]
        if args.apply:
            # cmdb-object-cf write wants the Assets globalId (workspaceId:objectId), not bare id
            _req("PUT", f"{JU}/rest/api/3/issue/{key}",
                 {"fields": {CF: [{"id": f"{WS}:{obj_id}"}]}})
            wrote += 1
            print(f"  SET {key}: {serial} -> {code} (obj {obj_id})", file=sys.stderr)
        else:
            print(f"  would set {key}: {serial} -> {code} (obj {obj_id})", file=sys.stderr)
    print(f"\nmatched={matched} unmatched={unmatched} wrote={wrote} already-set(skipped)={skipped}"
          + ("" if args.apply else "  (dry-run)"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
