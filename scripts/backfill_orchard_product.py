#!/usr/bin/env python3
"""Backfill the EPRO Unit Setup "Orchard Product" Assets field (customfield_12249) from the
serial number's product code, so PI-167's asset-driven import works on existing units.

Matching (PI-178 interim bridge): PRIMARY = map the human-set "Photo Booth Product" (cf10065)
to a product code via PBP_MAP (authoritative, set at order time, available before the serial).
FALLBACK = the serial's product SUFFIX (Product whose Code the serial ENDS WITH, longest match).
Preferring Photo Booth Product also corrects units whose serial product-code was mistyped
(logged as MISMATCH). Units matching neither are reported, not guessed.

Targets: open EPRO Unit Setups (statusCategory != Done) with the Orchard Product field empty
and either a Serial Number or a Photo Booth Product set.

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
CF = "customfield_12249"   # Orchard Product (Assets object)
PBP = "customfield_10065"  # legacy "Photo Booth Product" select

# Interim bridge (PI-178): derive Orchard Product from the human-set "Photo Booth Product"
# (authoritative, set at order time), preferring it over the serial suffix. The legacy field
# conflates product + content; here we map only the PRODUCT half (content overrides — MLB /
# NASCAR / NHL — are handled by the PI-178 Content Code field, not this bridge).
# Deliberately OMITTED (fall back to serial / stay unmatched, per PI-178 review):
#   - Game, Other            : CS-only / no Orchard product (Game runs for operator games).
#   - Magazine Me, Movie Scene Photo Booth : legacy options being removed from the field.
#   - Marvel Outdoor         : should be a NEW "MOD" product — create it in Orchard first, then map.
PBP_MAP = {
    "Card Creator": "PMC", "Deluxe": "DLX", "Disney Card Creator": "DMC",
    "Marvel Adventure Lab": "MAL",
    "MLB": "PHO", "MLB - PHOTOMA": "PHO", "MLB- Deluxe": "DLX", "MLB- Theme Park": "THM",
    "Movie Booth (PHOTOMA)": "MBP", "NASCAR - PHOTOMA": "PHO",
    "Photo Studio": "PSD", "Photo Studio Deluxe": "PSD", "Photo Studio Prism": "PSP",
    "Photo2Go": "P2G", "PHOTOMA": "PHO", "PHOTOMA - NHL": "PHO", "PHOTOMA Mini": "PHM",
    "PHOTOMA Mini - Card": "PMC", "PHOTOMA Outdoor": "PHO", "Pix Place": "PIX",
    "Royale": "ROY", "Ruby": "RBY", "Sapphire": "SAP", "Scene Machine": "SMM",
    "Star Wars Galactic ID": "GID", "The Disney Photo Booth": "DIS", "Theme Park": "THM",
    "Wedding Booth": "WED",
}


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
               'AND cf[12249] is EMPTY AND ("Serial Number" is not EMPTY OR cf[10065] is not EMPTY)')
    out, token = [], None
    while True:
        body = {"jql": jql, "maxResults": 100,
                "fields": ["customfield_10068", PBP, CF, "summary"]}
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
        pbp_raw = f.get(PBP)
        pbp = pbp_raw.get("value") if isinstance(pbp_raw, dict) else None
        code_pbp = PBP_MAP.get(pbp)
        code_serial = match_code(serial, list(codes))
        code = code_pbp or code_serial
        src = "PhotoBoothProduct" if code_pbp else "serial"
        if code_pbp and code_serial and code_pbp != code_serial:
            print(f"  MISMATCH {key}: Photo Booth Product {pbp!r}->{code_pbp} but serial {serial!r}->{code_serial} "
                  f"(using {code_pbp} — serial code may be mistyped)", file=sys.stderr)
        if not code:
            unmatched += 1
            print(f"  UNMATCHED {key}: photoBoothProduct={pbp!r} serial={serial!r} (no product match)", file=sys.stderr)
            continue
        matched += 1
        obj_id = codes[code]
        via = f"[{src}] {pbp or serial}"
        if args.apply:
            # cmdb-object-cf write wants the Assets globalId (workspaceId:objectId), not bare id
            _req("PUT", f"{JU}/rest/api/3/issue/{key}",
                 {"fields": {CF: [{"id": f"{WS}:{obj_id}"}]}})
            wrote += 1
            print(f"  SET {key}: {via} -> {code} (obj {obj_id})", file=sys.stderr)
        else:
            print(f"  would set {key}: {via} -> {code} (obj {obj_id})", file=sys.stderr)
    print(f"\nmatched={matched} unmatched={unmatched} wrote={wrote} already-set(skipped)={skipped}"
          + ("" if args.apply else "  (dry-run)"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
