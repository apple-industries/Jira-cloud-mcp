# Beta-lab hookup brief — Orchard → Jira Sync status page (EC2-hosted beta lab)

Hand this to a session working in the **beta-lab repo**. It has the app-specific facts; follow the
beta-lab repo's own conventions for each step (process/service definition, tunnel/ALB + Access,
secrets). The beta lab runs as a **hosted EC2 service**, so the sync executes **on the EC2 host**,
co-located with the page.

## What this is
A tiny internal status/trigger page for the **Orchard → Jira reference-data sync** (`orchard_jira_sync`:
Assets Products + Content Codes in the `ORCH` schema, and the Back Office Org/Operator cascading
field). Shows the last run (per-module counts, ok/warn/error) with a **Run sync now** button.

## App facts
- **File:** `jira-cloud-mcp/scripts/orchard_sync_web.py` (stdlib only — no pip deps)
- **Start:** `python3 scripts/orchard_sync_web.py` (cwd = the `jira-cloud-mcp` checkout)
- **Binds:** `127.0.0.1:8787` (env `ORCHARD_SYNC_WEB_HOST`/`_PORT`). Keep on loopback; reach it only
  through the tunnel/ALB.
- **Endpoints:** `GET /` page · `POST /run` → runs `orchard_jira_sync.py --apply` in a background
  thread · `GET /api/status` JSON. Page auto-refreshes every 4s while a run is active.
- **Status source:** `scripts/.last_sync.json`, written by the sync each run.

## ⚠️ Access is the only gate
No built-in auth, and `POST /run` performs **live production writes** (Jira Assets + Back Office
field) and reads the Orchard replica. Cloudflare Access + Entra **must** front it with a **tight
invite-only allowlist** (R&D / authorized sync operators). Never expose 8787.

## Execution model (recommended)
Run **both** the page and the sync on the EC2 host, and **move the nightly onto EC2** (systemd timer
or cron), retiring the Mac scheduled task `daily-backoffice-org-operator-resync`. This makes the whole
thing server-hosted and robust instead of depending on Jesse's laptop being awake.

### EC2 prerequisites (verify FIRST — the gating item)
1. **Network reach to the Orchard read replica** (`ADMIN_DB_URL` RDS). The sync shells out to `psql`.
   The EC2 must be in/peered to that VPC and the RDS security group must allow inbound from the EC2's
   SG. **Test early:** `psql "$ADMIN_DB_URL" -c 'select 1'` from the box. If this fails, nothing else
   matters — fix networking first (or fall back, see below).
2. **Egress** to `api.atlassian.com` (Assets) and the Jira site (`*.atlassian.net`).
3. **Tooling:** `python3` (3.10+) and the `psql` client installed.
4. **Code:** the `jira-cloud-mcp` checkout (and `orchard-mcp` if you keep the delegated org-operator
   module — it reads `orchard-mcp/.env`; alternatively point `ADMIN_DB_URL` via the environment).
   `jira-infrastructure/authored/assets/orch-schema.json` must be reachable (the sync looks in the
   sibling `jira-infrastructure` checkout or a co-located copy).

### Secrets (do NOT commit .env on EC2)
Provide these via SSM Parameter Store / Secrets Manager, injected into the service environment:
`JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `ASSETS_WORKSPACE_ID` (=`54312ac0-661d-4578-ab7f-81c799c73010`),
`ADMIN_DB_URL`, `BO_ORG_OPERATOR_FIELD_ID`. (The Orchard *Basic* auth token used by the Jira automation
is NOT needed here — that lives in the PI-167 rule, not the sync.)

## Wire-up checklist (adapt to the repo's pattern — systemd shown as a concrete example)
1. **Service (page):** run `orchard_sync_web.py` under systemd (or the repo's container/ECS pattern),
   bound to `127.0.0.1:8787`, env from SSM. Sample unit below.
2. **Timer (nightly):** a oneshot service + timer running `orchard_jira_sync.py --apply` daily
   (~06:00), replacing the Mac task. Sample below.
3. **Fronting:** route a hostname (suggest `orchard-sync.<beta-domain>`) to `127.0.0.1:8787` via the
   beta lab's existing cloudflared tunnel (or ALB), and attach a Cloudflare Access app (IdP = Entra,
   invite-only allowlist).
4. **Verify:** hostname → Entra login → page loads (OK badge + counts). Optionally click **Run sync
   now** once; confirm it completes and status refreshes (real `--apply`, idempotent). And confirm the
   timer's first nightly run succeeds, then **disable the Mac scheduled task**.

### Sample systemd units (adjust paths/user; env via EnvironmentFile populated from SSM at deploy)
```ini
# /etc/systemd/system/orchard-sync-web.service
[Unit]
Description=Orchard->Jira sync status page
After=network-online.target
[Service]
WorkingDirectory=/opt/jira-cloud-mcp
EnvironmentFile=/etc/orchard-sync/env      # written from SSM at deploy; contains the secrets above
Environment=ORCHARD_SYNC_WEB_HOST=127.0.0.1 ORCHARD_SYNC_WEB_PORT=8787
ExecStart=/usr/bin/python3 /opt/jira-cloud-mcp/scripts/orchard_sync_web.py
Restart=always
[Install]
WantedBy=multi-user.target
```
```ini
# /etc/systemd/system/orchard-jira-sync.service  (oneshot, run by the timer)
[Unit]
Description=Nightly Orchard->Jira sync (apply)
[Service]
Type=oneshot
WorkingDirectory=/opt/jira-cloud-mcp
EnvironmentFile=/etc/orchard-sync/env
ExecStart=/usr/bin/python3 /opt/jira-cloud-mcp/scripts/orchard_jira_sync.py --apply
```
```ini
# /etc/systemd/system/orchard-jira-sync.timer
[Unit]
Description=Run Orchard->Jira sync nightly
[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

## Fallback (only if EC2 cannot reach the Orchard replica)
Keep the nightly on the Mac; make the EC2 page **read-only** (remove/disable `POST /run`) and have the
Mac push `.last_sync.json` to S3 for the page to read. This is strictly worse (page can't trigger a
run, split infra) — prefer fixing EC2→RDS networking so the sync can live on EC2.

## Manual "Sync now" endpoint (for the Jira on-demand sync action)

`orchard_sync_web.py` also exposes a **machine-to-machine** route used by a Jira manual
automation ("Sync Back Office Org/Operator from Orchard", available on EPRO Unit Setup +
CUSTSVC Software Setup) to beat the nightly cron when someone just created an org/operator
in Back Office:

- **`POST /run/<module>`** — runs ONE sync module synchronously and returns JSON. Modules:
  `content-codes | products | unit-product | sim-types | org-operator`. The Jira rule calls
  `/run/org-operator`.
- **Auth:** requires header `X-Sync-Token: <ORCHARD_SYNC_RUN_TOKEN>` (set that env from SSM;
  it's the shared secret between Jira and the service). Returns 401 without it.

To let the Jira automation reach it **through Cloudflare Access**, create an Access
**service token** and allow it on the app's Access policy; the Jira rule sends
`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers (plus `X-Sync-Token`).

**Wire up the Jira rule** (source: `jira-infrastructure/authored/automations/sync-backoffice-org-operator-manual.json`):
fill the `<<SYNC_ENDPOINT_URL>>` (the hostname's base, e.g. `https://orchard-sync.<beta-domain>`),
`<<ORCHARD_SYNC_RUN_TOKEN>>`, `<<CF_ACCESS_CLIENT_ID>>`, `<<CF_ACCESS_CLIENT_SECRET>>`, then
create it DISABLED via the jira-cloud MCP `create_automation_rule`, round-trip, and enable.
Test: run it from a Unit Setup / Software Setup → expect a comment with the sync counts, then
the new org/operator appears in the Back Office Org/Operator dropdown. (The other modules can
get their own manual rules later the same way, pointing at `/run/products` etc.)

## Cross-references
- Sync: `jira-cloud-mcp/scripts/orchard_jira_sync.py` · Page: `orchard_sync_web.py`
- Assets id-map: `jira-infrastructure/authored/assets/orch-schema.json`
- Current nightly (to retire): scheduled task `daily-backoffice-org-operator-resync`
