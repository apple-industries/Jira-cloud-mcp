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

    @mcp.tool()
    async def create_automation_rule(
        rule_json: str, project_key: str = "", path: str = "/import"
    ) -> str:
        """Create automation rule(s) by POSTing a JSON definition to the internal API.

        rule_json: the rule definition as a JSON string. For the default '/import'
            endpoint this matches the export format, typically
            {"rules": [ {<rule>} ], "connections": {...}} (a single-rule export uses
            {"rule": {...}, "connections": {...}} — wrap it under "rules" for import).
        project_key: '' creates a GLOBAL rule; otherwise the project scope (e.g. 'PI').
        path: create endpoint on the rules API (default '/import').

        Tip: fetch an existing rule with get_automation_rule and adapt it as a template.
        """
        scope = _scope(project_key)
        try:
            payload = json.loads(rule_json)
        except json.JSONDecodeError as e:
            return _fmt({"error": f"invalid rule_json: {e}"})
        data = await client.automation_post(scope, path, payload)
        return _fmt(data or {"status": "created", "scope": scope})

    @mcp.tool()
    async def delete_automation_rule(rule_id: str, project_key: str = "") -> str:
        """Delete an automation rule by id."""
        scope = _scope(project_key)
        await client.automation_delete(scope, f"/{rule_id}")
        return _fmt({"status": "deleted", "ruleId": rule_id})
