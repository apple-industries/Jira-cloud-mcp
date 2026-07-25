"""Jira Cloud automation rules tools.

Uses the PUBLIC automation REST API:
  /gateway/api/automation/public/jira/{cloudId}/rest/v1

Endpoints:
  GET    /rule/summary        list rule summaries (cursor-paginated)
  GET    /rule/{uuid}         get a rule
  POST   /rule               create a rule from a Rule Payload
  PUT    /rule/{uuid}/state  enable/disable a rule
  DELETE /rule/{uuid}         delete a disabled rule

Works with basic auth (email:API token); Forge/OAuth2 apps are excluded.
"""

import json
from jira_client import JiraCloudClient


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def register_automation_tools(mcp, client: JiraCloudClient):

    @mcp.tool()
    async def list_automation_rules(project_key: str = "") -> str:
        """List automation rule summaries (all rules on the site).

        The public API is site-wide (no project scope in the URL). If project_key
        is given, results are filtered client-side to rules scoped to that project.
        """
        out = []
        path = "/rule/summary"
        while path:
            page = await client.automation_get(path)
            if isinstance(page, dict):
                out.extend(page.get("data", []))
                nxt = (page.get("links") or {}).get("next")
                if not nxt:
                    break
                path = ("/rule/summary" + nxt) if nxt.startswith("?") else nxt
            else:  # non-paginated fallback
                out.extend(page if isinstance(page, list) else [page])
                break
        if project_key.strip():
            pk = project_key.strip()
            out = [
                r for r in out
                if any(str(p.get("key") or p.get("projectId")) == pk
                       for p in (r.get("projects") or []))
            ]
        return _fmt(out)

    @mcp.tool()
    async def get_automation_rule(rule_id: str) -> str:
        """Get an automation rule by UUID — trigger, conditions, actions."""
        data = await client.automation_get(f"/rule/{rule_id}")
        return _fmt(data)

    @mcp.tool()
    async def enable_automation_rule(rule_id: str) -> str:
        """Enable an automation rule."""
        data = await client.automation_put(f"/rule/{rule_id}/state", {"state": "ENABLED"})
        return _fmt(data or {"status": "ENABLED", "ruleId": rule_id})

    @mcp.tool()
    async def disable_automation_rule(rule_id: str) -> str:
        """Disable an automation rule."""
        data = await client.automation_put(f"/rule/{rule_id}/state", {"state": "DISABLED"})
        return _fmt(data or {"status": "DISABLED", "ruleId": rule_id})

    @mcp.tool()
    async def create_automation_rule(rule_json: str = "", rule_file: str = "") -> str:
        """Create an automation rule via POST /rule (public API).

        Provide exactly one of:
          rule_json: the Rule Payload as a JSON string, or
          rule_file: a path to a .json file containing the Rule Payload.
        The payload shape matches what GET /rule/{uuid} returns, i.e.
        {"rule": {...}, "connections": {...}}. Create the rule with
        "state": "DISABLED" first, then round-trip with get_automation_rule.
        """
        raw = rule_json
        if rule_file:
            try:
                with open(rule_file, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as e:
                return _fmt({"error": f"cannot read rule_file: {e}"})
        if not raw.strip():
            return _fmt({"error": "provide rule_json or rule_file"})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return _fmt({"error": f"invalid rule JSON: {e}"})
        data = await client.automation_post("/rule", payload)
        return _fmt(data or {"status": "created"})

    @mcp.tool()
    async def delete_automation_rule(rule_id: str) -> str:
        """Delete a (disabled) automation rule by UUID."""
        await client.automation_delete(f"/rule/{rule_id}")
        return _fmt({"status": "deleted", "ruleId": rule_id})
