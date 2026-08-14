#!/usr/bin/env python3
"""PI-177 helper: set a booth's TeamViewer Computers & Contacts entry — alias, group,
and the saved (client-side) unattended password — matched by HDID.

Works on UNASSIGNED devices (these are C&C-entry properties; no managed-device cost).
The classic TeamViewer API has no server-side HDID filter, so we fetch /devices once and
match the HDID against each entry's alias + description (normalized). A UNIQUE match is
required — 0 or >1 matches is an error (never guess which booth).

Env:
  TEAMVIEWER_API_TOKEN        personal user script token (C&C view+edit, Group view)
  TEAMVIEWER_SHARED_PASSWORD  optional; the shared unattended password to save client-side

CLI (dry-run by default; add --apply to write):
  python3 teamviewer_assign.py --hdid 6479_A781_3A50_0240 \
      --alias "Epro-4031/032600002PHO" --group-id g371213835 [--apply]

Library (used by the beta-lab /teamviewer/assign route):
  from teamviewer_assign import assign, TVError
  assign(hdid=..., alias=..., group_id=..., group_name=None, password=..., apply=True)
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = "https://webapi.teamviewer.com/api/v1"


class TVError(Exception):
    pass


def _token():
    t = os.environ.get("TEAMVIEWER_API_TOKEN", "").strip()
    if not t:
        raise TVError("TEAMVIEWER_API_TOKEN not set")
    return t


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {_token()}",
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return e.code, parsed


def _norm(s):
    """Uppercase + strip non-alphanumerics, so '6479_A781_..' matches 'HDID: 6479 A781 ..'."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def list_devices():
    code, body = _req("GET", "/devices")
    if code != 200:
        raise TVError(f"GET /devices failed: {code} {body}")
    return body.get("devices", [])


def find_by_hdid(devices, hdid):
    key = _norm(hdid)
    if len(key) < 4:
        raise TVError(f"HDID '{hdid}' too short/empty to match safely")
    hits = [d for d in devices
            if key in _norm(d.get("alias")) or key in _norm(d.get("description"))]
    uniq = list({d["device_id"]: d for d in hits}.values())
    if not uniq:
        raise TVError(f"no TeamViewer device matches HDID '{hdid}'")
    if len(uniq) > 1:
        raise TVError("HDID '%s' matched %d devices: %s" % (
            hdid, len(uniq), ", ".join(f"{d['device_id']}({d.get('alias')})" for d in uniq)))
    return uniq[0]


def _load_crosswalk():
    """Curated {label -> group_id} map (teamviewer_groups.json, sibling file). Authoritative,
    dedup-safe: the TV account has duplicate group names, so we never resolve by live name."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teamviewer_groups.json")
    try:
        return json.load(open(path)).get("label_to_group_id", {})
    except Exception:
        return {}


def resolve_group_id(group_id, group_name):
    if group_id:
        return group_id
    if not group_name:
        return None
    # 1) curated crosswalk (the Jira field options map to exact ids here)
    xwalk = _load_crosswalk()
    if group_name in xwalk:
        return xwalk[group_name]
    # 2) fallback: live name lookup (unique-or-error)
    code, body = _req("GET", "/groups")
    if code != 200:
        raise TVError(f"GET /groups failed: {code} {body}")
    matches = [g for g in body.get("groups", []) if g.get("name") == group_name]
    if len(matches) != 1:
        raise TVError(f"group '{group_name}' not in the curated crosswalk and resolved to "
                      f"{len(matches)} live groups (names are duplicated; add it to "
                      f"teamviewer_groups.json or pass group_id)")
    return matches[0]["id"]


def default_group_label(org, operator):
    """UK/US -> TeamViewer group default from the Back Office org/operator. Mirrors the
    PI-177 rule. The Jira 'TeamViewer Group' field OVERRIDES this; we only compute the
    default here when that field is empty. See teamviewer-automation memory for the rule."""
    org = (org or "").strip()
    operator = (operator or "").strip()
    if org == "Apple Photo Booth UK LTD":
        return "APB UK"
    if org == "Apple Photo Booth" or org.upper().startswith("APB") or operator.upper().startswith("APB"):
        return "APB - To Route"
    return "Newly Registered"


def assign(hdid, alias=None, group_id=None, group_name=None, org=None, operator=None,
           password=None, apply=False):
    """Find the device by HDID and set alias/group/password. Group precedence:
    explicit group_id > group_name (the Jira 'TeamViewer Group' override field) >
    computed default from org/operator. Returns a compact result (password masked).
    apply=False => dry run (no write)."""
    # A4J renders an empty select as "" (and occasionally "null"); treat those as unset so
    # a blank 'TeamViewer Group' field falls through to the computed default.
    if isinstance(group_name, str) and group_name.strip().lower() in ("", "null", "none"):
        group_name = None
    computed = None
    if not group_id and not group_name and (org or operator):
        computed = default_group_label(org, operator)
        group_name = computed
    devices = list_devices()
    dev = find_by_hdid(devices, hdid)
    gid = resolve_group_id(group_id, group_name)
    payload = {}
    if alias:
        payload["alias"] = alias
    if gid:
        payload["groupid"] = gid
    if password:
        payload["password"] = password
    if not payload:
        raise TVError("nothing to change (need at least one of alias/group/password)")
    result = {
        "device_id": dev["device_id"],
        "teamviewer_id": dev.get("teamviewer_id"),
        "matched_alias": dev.get("alias"),
        "from_group": dev.get("groupid"),
        "group_label": group_name,
        "group_source": "computed-default" if computed else "field/explicit",
        "changes": {k: ("***" if k == "password" else v) for k, v in payload.items()},
        "applied": False,
    }
    if apply:
        code, body = _req("PUT", f"/devices/{dev['device_id']}", payload)
        if code not in (200, 204):
            raise TVError(f"PUT /devices/{dev['device_id']} failed: {code} {body}")
        result["applied"] = True
        result["http"] = code
    return result


def _main(argv):
    p = argparse.ArgumentParser(description="Set TeamViewer alias/group/password by HDID")
    p.add_argument("--hdid", required=True)
    p.add_argument("--alias")
    p.add_argument("--group-id")
    p.add_argument("--group-name")
    p.add_argument("--org", help="Back Office org (for computed default when no group given)")
    p.add_argument("--operator", help="Back Office operator (for computed default)")
    p.add_argument("--password", help="or set TEAMVIEWER_SHARED_PASSWORD")
    p.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    a = p.parse_args(argv)
    pw = a.password or os.environ.get("TEAMVIEWER_SHARED_PASSWORD") or None
    try:
        res = assign(hdid=a.hdid, alias=a.alias, group_id=a.group_id, group_name=a.group_name,
                     org=a.org, operator=a.operator, password=pw, apply=a.apply)
    except TVError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 2
    res["ok"] = True
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
