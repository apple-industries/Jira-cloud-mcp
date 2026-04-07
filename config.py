"""Configuration for Jira Cloud MCP server."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Jira Cloud
    jira_url: str = "https://your-domain.atlassian.net"
    jira_email: str = ""
    jira_api_token: str = ""
    jira_ssl_verify: bool = True

    # Cloud ID — auto-resolved from _edge/tenant_info if not set
    jira_cloud_id: str = ""

    # MCP transport
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8001

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    @property
    def api_v3_url(self) -> str:
        return f"{self.jira_url.rstrip('/')}/rest/api/3"

    @property
    def api_v2_url(self) -> str:
        return f"{self.jira_url.rstrip('/')}/rest/api/2"

    @property
    def auth(self) -> tuple[str, str]:
        """Basic auth tuple for Cloud (email:token)."""
        return (self.jira_email, self.jira_api_token)


settings = Settings()
