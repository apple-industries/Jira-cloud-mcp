"""Jira Cloud automation rules tools.

Uses the official public automation API via the gateway proxy:
  /gateway/api/automation/public/jira/{cloudId}/rest/v1/...

This works with basic auth (email:API token) through the Jira gateway.
"""

import json
from jira_client import JiraCloudClient


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def register_automation_tools(mcp, client: JiraCloudClient):

    @mcp.tool()
    async def list_automation_rules(project_key: str = "") -> str:
        """List automation rules. If project_key given, filter to that project; otherwise list all."""
        if project_key.strip():
            # Resolve project key to ID, then build the scope ARI
            proj = await client.get(f"/project/{project_key.strip()}")
            pid = proj["id"]
            cloud_id = await client.get_cloud_id()
            scope_ari = f"ari:cloud:jira:{cloud_id}:project/{pid}"
            data = await client.automation_post(
                "/rule/summary",
                body={"scope": scope_ari},
            )
        else:
            data = await client.automation_get("/rule/summary")
        return _fmt(data)

    @mcp.tool()
    async def get_automation_rule(rule_uuid: str) -> str:
        """Get automation rule details — trigger, conditions, actions."""
        data = await client.automation_get(f"/rule/{rule_uuid.strip()}")
        return _fmt(data)

    @mcp.tool()
    async def enable_automation_rule(rule_uuid: str) -> str:
        """Enable an automation rule."""
        data = await client.automation_put(
            f"/rule/{rule_uuid.strip()}/state",
            body={"state": "ENABLED"},
        )
        return _fmt(data or {"status": "enabled", "ruleUuid": rule_uuid})

    @mcp.tool()
    async def disable_automation_rule(rule_uuid: str) -> str:
        """Disable an automation rule."""
        data = await client.automation_put(
            f"/rule/{rule_uuid.strip()}/state",
            body={"state": "DISABLED"},
        )
        return _fmt(data or {"status": "disabled", "ruleUuid": rule_uuid})
