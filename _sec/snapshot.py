"""QUARTERLY snapshot of the PRODUCT+OPS initiative portfolio (Primary Theme x Horizon),
written to one accumulating Confluence page in PRODEV.

Source of truth = a Confluence content property (JSON history of all quarters). Each run
upserts the current quarter and fully regenerates the page body: accumulating trend charts
(Horizon by quarter, Primary Theme by quarter) + the latest-quarter matrix. Standalone +
cron-safe; idempotent per calendar quarter."""
import asyncio, json, sys
from datetime import datetime, timezone
from collections import Counter, defaultdict
from lib import c

PT, HZ = "customfield_11012", "customfield_11014"
HZ_ORDER = ["Run", "Grow", "Transform"]
THEMES = ["Content Ecosystem", "Operator Tooling", "Consumer Experience", "Data & Intelligence",
          "Service Health, Reliability & Maintenance", "Compliance, Privacy & Risk",
          "Market & Geographic Expansion", "Platform Monetization", "Licensed Partnerships",
          "Product Quality & Polish", "Efficiency & Cost Optimization"]
SPACE_KEY = "PRODEV"
PAGE_TITLE = "Initiative Theme & Horizon — Quarterly Snapshots"
PROP_KEY = "themeHorizonHistory"
JQL = "project IN (PRODUCT, OPS) AND issuetype = Initiative ORDER BY key ASC"
SITE = "https://apple-industries.atlassian.net"
INTRO = ("<p>Auto-generated <strong>quarterly</strong> snapshot of the PRODUCT + OPS initiative "
         "portfolio by Primary Theme and Horizon. Charts accumulate each quarter. Maintained by "
         "<code>jira-cloud-mcp/_sec/snapshot.py</code> (run end of each quarter via launchd).</p>")


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quarter_of(dt):
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


async def compute():
    res = await c.get("/search/jql", jql=JQL, maxResults=200, fields=f"key,{PT},{HZ}")
    rows = [{"P": (it["fields"].get(PT) or {}).get("value"),
             "H": (it["fields"].get(HZ) or {}).get("value")} for it in res["issues"]]
    horizon = {k: 0 for k in HZ_ORDER}
    for r in rows:
        if r["H"] in horizon:
            horizon[r["H"]] += 1
    primary = {t: 0 for t in THEMES}
    mat = {t: {k: 0 for k in HZ_ORDER} for t in THEMES}
    for r in rows:
        if r["P"] in primary:
            primary[r["P"]] += 1
            if r["H"] in mat[r["P"]]:
                mat[r["P"]][r["H"]] += 1
    return {"total": len(rows), "horizon": horizon, "primary": primary, "matrix": mat}


def chart_macro(ctype, header, data_rows, stacked=True, height=380):
    cells = "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in header) + "</tr>"
    for r in data_rows:
        cells += "<tr>" + "".join(f"<td>{esc(str(v))}</td>" for v in r) + "</tr>"
    params = [("type", ctype), ("legend", "true"), ("width", "760"), ("height", str(height)),
              ("dataDisplay", "after")]
    if stacked and ctype == "bar":
        params.append(("stacked", "true"))
    pstr = "".join(f'<ac:parameter ac:name="{k}">{v}</ac:parameter>' for k, v in params)
    return (f'<ac:structured-macro ac:name="chart">{pstr}'
            f'<ac:rich-text-body><table><tbody>{cells}</tbody></table></ac:rich-text-body>'
            f'</ac:structured-macro>')


def render_body(history):
    quarters = sorted(history["quarters"].keys())
    latest = quarters[-1]
    snap = history["quarters"][latest]

    # Horizon by quarter (stacked bar)
    hz_rows = [[q] + [history["quarters"][q]["horizon"].get(k, 0) for k in HZ_ORDER] for q in quarters]
    horizon_chart = chart_macro("bar", ["Quarter"] + HZ_ORDER, hz_rows, stacked=True)

    # Primary Theme by quarter (stacked bar, 11 series)
    th_rows = [[q] + [history["quarters"][q]["primary"].get(t, 0) for t in THEMES] for q in quarters]
    theme_chart = chart_macro("bar", ["Quarter"] + THEMES, th_rows, stacked=True, height=460)

    # Latest quarter matrix (plain table — always visible)
    pct = lambda n: f"{round(100*n/snap['total'])}%" if snap['total'] else "0%"
    mh = [f"<h2>Latest quarter — {latest} (as of {snap['date']})</h2>",
          f"<p><strong>{snap['total']}</strong> initiatives · "
          + " · ".join(f"{k} {snap['horizon'].get(k,0)} ({pct(snap['horizon'].get(k,0))})" for k in HZ_ORDER) + "</p>",
          "<table><tbody><tr><th>Primary Theme</th>" + "".join(f"<th>{k}</th>" for k in HZ_ORDER) + "<th>Total</th></tr>"]
    for t in sorted(THEMES, key=lambda x: -sum(snap["matrix"].get(x, {}).values())):
        row = snap["matrix"].get(t, {})
        tot = sum(row.values())
        if tot == 0:
            continue
        mh.append(f"<tr><td>{esc(t)}</td>" + "".join(f"<td>{row.get(k,0)}</td>" for k in HZ_ORDER)
                  + f"<td><strong>{tot}</strong></td></tr>")
    mh.append("<tr><td><strong>Total</strong></td>"
              + "".join(f"<td><strong>{snap['horizon'].get(k,0)}</strong></td>" for k in HZ_ORDER)
              + f"<td><strong>{snap['total']}</strong></td></tr></tbody></table>")

    return (INTRO
            + "<h2>Horizon mix by quarter</h2>" + horizon_chart
            + "<h2>Primary Theme by quarter</h2>" + theme_chart
            + "".join(mh))


async def get_property(pid):
    r = await c.client.get(f"{SITE}/wiki/rest/api/content/{pid}/property/{PROP_KEY}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def _run(dry=False):
    now = datetime.now(timezone.utc).astimezone()
    q, date_str = quarter_of(now), now.strftime("%Y-%m-%d")
    snap = await compute()
    snap["date"] = date_str

    # find page
    found = await c.raw_get("/wiki/rest/api/content", spaceKey=SPACE_KEY, title=PAGE_TITLE,
                            expand="version", limit=1)
    results = found.get("results", [])

    if results:
        pid = results[0]["id"]
        ver = results[0]["version"]["number"]
        prop = await get_property(pid)
        history = prop["value"] if prop else {"quarters": {}}
    else:
        pid, ver, prop, history = None, None, None, {"quarters": {}}

    history["quarters"][q] = {k: snap[k] for k in ("date", "total", "horizon", "primary", "matrix")}
    body = render_body(history)

    if dry:
        # Read-only: Jira compute + Confluence page/property fetch already ran above;
        # here we STOP before any write so the job can be tested against live data
        # without mutating Confluence.
        action = "update" if pid else "create"
        print(f"[DRY RUN] would {action} page "
              f"{pid if pid else '(new)'} in {SPACE_KEY} -> v{(ver + 1) if pid else 1} for {q}")
        print(f"[DRY RUN] {q}: {snap['total']} initiatives | "
              + " ".join(f"{k}={snap['horizon'][k]}" for k in HZ_ORDER))
        print(f"[DRY RUN] quarters that would be on record: {sorted(history['quarters'])}")
        print(f"[DRY RUN] rendered body: {len(body)} chars. No writes performed.")
        return

    if pid:
        r = await c.client.put(f"{SITE}/wiki/rest/api/content/{pid}", json={
            "id": pid, "type": "page", "title": PAGE_TITLE, "space": {"key": SPACE_KEY},
            "version": {"number": ver + 1, "message": f"Quarterly snapshot {q}"},
            "body": {"storage": {"value": body, "representation": "storage"}}})
        r.raise_for_status()
        print(f"[updated] page {pid} -> v{ver+1} ({q})")
    else:
        r = await c.client.post(f"{SITE}/wiki/rest/api/content", json={
            "type": "page", "title": PAGE_TITLE, "space": {"key": SPACE_KEY},
            "body": {"storage": {"value": body, "representation": "storage"}}})
        r.raise_for_status()
        pid = r.json()["id"]
        print(f"[created] page {pid} ({q})")

    # upsert content property (source of truth)
    if prop:
        r = await c.client.put(f"{SITE}/wiki/rest/api/content/{pid}/property/{PROP_KEY}",
                               json={"value": history, "version": {"number": prop["version"]["number"] + 1}})
    else:
        r = await c.client.post(f"{SITE}/wiki/rest/api/content/{pid}/property",
                                json={"key": PROP_KEY, "value": history})
    r.raise_for_status()

    print(f"Page: {SITE}/wiki/spaces/{SPACE_KEY}/pages/{pid}")
    print(f"Quarters on record: {sorted(history['quarters'])}")
    print(f"{q}: {snap['total']} initiatives | " + " ".join(f"{k}={snap['horizon'][k]}" for k in HZ_ORDER))


async def main():
    dry = "--dry-run" in sys.argv
    try:
        await _run(dry=dry)
    except Exception:
        import traceback
        from alerts import alert_failure
        alert_failure(traceback.format_exc())
        raise


if __name__ == "__main__":
    asyncio.run(main())
