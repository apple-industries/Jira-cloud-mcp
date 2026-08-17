#!/usr/bin/env python3
"""PI-177 backfill: bring existing EPRO Unit Setups' TeamViewer entries in line with the
alias/group convention the "Assign TeamViewer group + alias (manual)" rule applies.

Goes through the PRODUCTION endpoint (jobs.faceplace.co/teamviewer/assign) so a backfilled
unit gets exactly what the Jira action would give it — including the shared unattended
password from Secrets Manager (which is NOT available locally).

Only touches units where something would actually change (alias or group differs). Adds a
Jira comment on each ticket it changes so the production team knows what happened.

Dry run by default; --apply to write.

  python3 teamviewer_backfill.py                 # dry run, all candidates
  python3 teamviewer_backfill.py --apply         # do it
  python3 teamviewer_backfill.py --only EPRO-123 # single ticket
  python3 teamviewer_backfill.py --limit 5 --apply

Env: TEAMVIEWER_API_TOKEN (read-only here, for the before/after picture) +
     ~/.config/cloudflare/jobs-sync-service-token.json (CF Access service token) +
     jira-cloud-mcp/.env (JIRA_URL / JIRA_EMAIL / JIRA_API_TOKEN).
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENDPOINT = "https://jobs.faceplace.co/teamviewer/assign"
TV = "https://webapi.teamviewer.com/api/v1"
# Cloudflare's WAF bans the default Python-urllib UA on this host (error 1010).
UA = "curl/8.7.1"

HDID_F, SERIAL_F, ORGOP_F, TVGROUP_F = (
    "customfield_10067", "customfield_10068", "customfield_12246", "customfield_12283")


def _env():
    e = {}
    for line in open(os.path.join(REPO, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip().strip('"').strip("'")
    return e


ENV = _env()
JIRA = ENV["JIRA_URL"].rstrip("/")
JAUTH = base64.b64encode(f"{ENV['JIRA_EMAIL']}:{ENV['JIRA_API_TOKEN']}".encode()).decode()
CF = json.load(open(os.path.expanduser("~/.config/cloudflare/jobs-sync-service-token.json")))["result"]


def _req(url, data=None, headers=None, method=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def jira_get(path):
    return _req(f"{JIRA}{path}", headers={"Authorization": f"Basic {JAUTH}",
                                          "Accept": "application/json"})


def jira_comment(key, text):
    body = json.dumps({"body": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}).encode()
    return _req(f"{JIRA}/rest/api/3/issue/{key}/comment", data=body,
                headers={"Authorization": f"Basic {JAUTH}", "Content-Type": "application/json"},
                method="POST")


def tv_get(path):
    return _req(f"{TV}{path}", headers={"Authorization": f"Bearer {os.environ['TEAMVIEWER_API_TOKEN']}",
                                        "User-Agent": UA})


def assign(payload):
    return _req(ENDPOINT, data=json.dumps(payload).encode(),
                headers={"CF-Access-Client-Id": CF["client_id"],
                         "CF-Access-Client-Secret": CF["client_secret"],
                         "Content-Type": "application/json", "User-Agent": UA}, method="POST")


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def target_label(org, operator, override):
    if override:
        return override
    org, operator = (org or ""), (operator or "")
    if org == "Apple Photo Booth UK LTD":
        return "APB UK"
    if org == "Apple Photo Booth" or org.upper().startswith("APB") or operator.upper().startswith("APB"):
        return "APB - To Route"
    return "Newly Registered"


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--only", help="a single issue key")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-comment", action="store_true", help="skip the Jira comment")
    p.add_argument("--group-only", action="store_true",
                   help="only units whose GROUP changes; skip alias-case-only fixes (avoids "
                        "churn + pointless ticket comments for an EPRO->Epro capitalization)")
    a = p.parse_args(argv)

    jql = (f'key = {a.only}' if a.only else
           'project = EPRO AND issuetype = "Unit Setup" '
           f'AND cf[{HDID_F[12:]}] is not EMPTY AND cf[{SERIAL_F[12:]}] is not EMPTY '
           f'AND cf[{ORGOP_F[12:]}] is not EMPTY ORDER BY updated DESC')
    fields = ",".join(["key", HDID_F, SERIAL_F, ORGOP_F, TVGROUP_F])
    s, body = jira_get(f"/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}"
                       f"&maxResults=200&fields={fields}")
    if s != 200:
        print("Jira search failed:", s, body); return 1
    issues = body.get("issues", [])

    s, dv = tv_get("/devices")
    if s != 200:
        print("TeamViewer /devices failed:", s, dv); return 1
    devices = dv["devices"]
    s, gv = tv_get("/groups")
    gname = {g["id"]: g["name"] for g in gv.get("groups", [])}
    label_to_id = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "teamviewer_groups.json")))["label_to_group_id"]

    plan, skipped = [], []
    for it in issues:
        f = it["fields"]; key = it["key"]
        hd, ser = f.get(HDID_F), f.get(SERIAL_F)
        oo = f.get(ORGOP_F) or {}
        org = oo.get("value") if isinstance(oo, dict) else None
        opv = (oo.get("child") or {}).get("value") if isinstance(oo, dict) else None
        ov = f.get(TVGROUP_F)
        override = ov.get("value") if isinstance(ov, dict) else None
        k = norm(hd)
        hits = [d for d in devices
                if k and (k in norm(d.get("alias")) or k in norm(d.get("description")))]
        hits = list({d["device_id"]: d for d in hits}.values())
        if len(hits) != 1:
            skipped.append((key, f"{len(hits)} device matches for HDID {hd}")); continue
        d = hits[0]
        want_alias = f"Epro-{key.split('-')[-1]}/{ser}"
        tlabel = target_label(org, opv, override)
        tgid = label_to_id.get(tlabel)
        if not tgid:
            skipped.append((key, f"target group {tlabel!r} not in crosswalk")); continue
        changes = []
        if (d.get("alias") or "") != want_alias:
            changes.append(f"alias {d.get('alias')!r} -> {want_alias!r}")
        if d.get("groupid") != tgid:
            changes.append(f"group {gname.get(d.get('groupid'))!r} -> {tlabel!r}")
        if not changes:
            continue
        if a.group_only and d.get("groupid") == tgid:
            continue
        plan.append({"key": key, "hdid": hd, "serial": ser, "alias": want_alias,
                     "group": tlabel, "device": d["device_id"], "changes": changes})

    if a.limit:
        plan = plan[:a.limit]

    print(f"=== {'APPLY' if a.apply else 'DRY RUN'} — {len(plan)} unit(s) to change "
          f"({len(issues)} scanned, {len(skipped)} skipped) ===\n")
    for r in plan:
        print(f"{r['key']:<11} {r['serial']:<15} dev {r['device']}")
        for c in r["changes"]:
            print(f"              {c}")
    if skipped:
        print(f"\n--- skipped ({len(skipped)}) ---")
        for k, why in skipped[:12]:
            print(f"  {k}: {why}")
    if not a.apply:
        print("\n(no writes — re-run with --apply)")
        return 0

    print("\n=== applying ===")
    ok = fail = 0
    for r in plan:
        s, res = assign({"hdid": r["hdid"], "alias": r["alias"],
                         "group_name": r["group"], "apply": True})
        good = s == 200 and res.get("ok")
        print(f"  {r['key']:<11} HTTP {s} {'OK' if good else 'FAIL ' + str(res)[:160]}")
        if not good:
            fail += 1; continue
        ok += 1
        if not a.no_comment:
            note = ("🖥️ TeamViewer backfill (PI-177): this unit's TeamViewer entry was set to "
                    f"alias \"{r['alias']}\" and moved to group \"{r['group']}\", and the shared "
                    "unattended password was applied. Done in bulk to match the new "
                    "\"Assign TeamViewer group + alias (manual)\" automation — from now on that "
                    "⚡ action on the ticket does this for you during software setup, so the "
                    "manual TeamViewer rename/group step is no longer needed. No action required.")
            cs, cb = jira_comment(r["key"], note)
            if cs not in (200, 201):
                print(f"     (comment failed: HTTP {cs} {str(cb)[:120]})")
    print(f"\ndone — {ok} applied, {fail} failed")
    return 0 if not fail else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
