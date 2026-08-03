"""Jira Cloud MCP — AI-powered Jira Cloud administration and analysis.

Single MCP server for both read and write operations via Jira Cloud REST API v3.
"""

import argparse

from mcp.server.fastmcp import FastMCP

from config import settings
from jira_client import jira
from issue_tools import register_issue_tools
from field_tools import register_field_tools
from workflow_tools import register_workflow_tools
from scheme_tools import register_scheme_tools
from project_tools import register_project_tools
from screen_tools import register_screen_tools
from user_tools import register_user_tools
from automation_tools import register_automation_tools
from admin_tools import register_admin_tools
from assets_tools import register_assets_tools

mcp = FastMCP(
    "jira-cloud",
    instructions="""You are a Jira Cloud administration assistant. You can analyze configuration
and execute changes via the Jira Cloud REST API v3.

=== SAFETY ===
- Before EVERY destructive action (delete, remove, reassign schemes), explain
  what you're about to do and ASK FOR CONFIRMATION
- After execution, show the result

=== ANALYSIS PATTERN ===
When analyzing a project, use this order:
1. get_project_config — full picture (schemes, roles, components, versions)
2. get_project_roles — who has access
3. jql_search — find issues
4. list_automation_rules — see automation

When analyzing configuration:
1. list_* to see all items in a domain
2. get_* for deep dive into specific items
3. Cross-reference schemes to find conflicts

=== RESPONSE FORMAT ===
- Always show what was done: issue key, object ID, operation status
- For errors — show the error text and suggest a fix
- For related changes — do everything in one session
""",
)

register_issue_tools(mcp, jira)
register_field_tools(mcp, jira)
register_workflow_tools(mcp, jira)
register_scheme_tools(mcp, jira)
register_project_tools(mcp, jira)
register_screen_tools(mcp, jira)
register_user_tools(mcp, jira)
register_automation_tools(mcp, jira)
register_admin_tools(mcp, jira)
register_assets_tools(mcp, jira)


def main():
    parser = argparse.ArgumentParser(description="Jira Cloud MCP")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=settings.mcp_transport,
    )
    parser.add_argument("--host", default=settings.mcp_host)
    parser.add_argument("--port", type=int, default=settings.mcp_port)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
