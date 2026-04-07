"""Jira Cloud native automation rules tools.

Uses the internal gateway API that the Jira UI itself calls:
  /gateway/api/automation/internal-api/jira/{cloudId}/pro/rest/{scope}/rules

This works with basic auth (email:API token) unlike the official
automation API at api.atlassian.com which requires OAuth 2.0.
"""

import json
from jira_client import JiraCloudClient


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _scope(project_key: str) -> str:
    """Return 'GLOBAL' or the project key as the automation scope."""
    return project_key.strip() if project_key.strip() else "GLOBAL"


def register_automation_tools(mcp, client: JiraCloudClient):

    @mcp.tool()
    async def list_automation_rules(project_key: str = "") -> str:
        """List automation rules. If project_key given, list project rules; otherwise global."""
        scope = _scope(project_key)
        data = await client.automation_get(scope)
        return _fmt(data)

    @mcp.tool()
    async def get_automation_rule(rule_id: str, project_key: str = "") -> str:
        """Get automation rule details \u2014 trigger, conditions, actions."""
        scope = _scope(project_key)
        data = await client.automation_get(scope, f"/{rule_id}")
        return _fmt(data)

    @mcp.tool()
    async def enable_automation_rule(rule_id: str, project_key: str = "") -> str:
        """Enable an automation rule."""
        scope = _scope(project_key)
        data = await client.automation_put(scope, f"/{rule_id}/enable")
        return _fmt(data or {"status": "enabled", "ruleId": rule_id})

    @mcp.tool()
    async def disable_automation_rule(rule_id: str, project_key: str = "") -> str:
        """Disable an automation rule."""
        scope = _scope(project_key)
        data = await client.automation_put(scope, f"/{rule_id}/disable")
        return _fmt(data or {"status": "disabled", "ruleId": rule_id})
