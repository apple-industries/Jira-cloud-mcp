"""HTTP client for Jira Cloud REST API v3."""

import asyncio
import sys

import httpx

from config import settings


class JiraCloudClient:
    """Async HTTP client with rate-limit handling for Jira Cloud."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._rate_limit_remaining: int = 100
        self._rate_limit_reset: float = 0
        self._cloud_id: str = settings.jira_cloud_id

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=(settings.jira_email, settings.jira_api_token),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                verify=settings.jira_ssl_verify,
                timeout=30.0,
            )
        return self._client

    async def _handle_rate_limit(self, resp: httpx.Response):
        """Track rate limit headers and back off if needed."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        reset = resp.headers.get("Retry-After")
        if resp.status_code == 429:
            wait = int(reset) if reset else 5
            await asyncio.sleep(wait)
            return True  # Should retry
        return False

    # --- Cloud ID resolution ---

    async def get_cloud_id(self) -> str:
        """Resolve and cache the Jira Cloud ID.

        Uses JIRA_CLOUD_ID env var if set, otherwise fetches from
        the _edge/tenant_info endpoint.
        """
        if self._cloud_id:
            return self._cloud_id

        url = f"{settings.jira_url.rstrip('/')}/_edge/tenant_info"
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            data = resp.json()
            self._cloud_id = data.get("cloudId", "")
            if not self._cloud_id:
                print(
                    "Warning: Could not resolve cloudId from tenant_info. "
                    "Set JIRA_CLOUD_ID in your .env file.",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(
                f"Warning: Failed to resolve cloudId: {exc}. "
                f"Set JIRA_CLOUD_ID in your .env file.",
                file=sys.stderr,
            )
        return self._cloud_id

    # --- Core HTTP methods ---

    async def get(self, path: str, api: str = "v3", **params) -> dict | list:
        base = settings.api_v3_url if api == "v3" else settings.api_v2_url
        url = f"{base}{path}"
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        for attempt in range(3):
            resp = await self.client.get(url, params=params)
            if await self._handle_rate_limit(resp):
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    async def post(self, path: str, body: dict, api: str = "v3") -> dict:
        base = settings.api_v3_url if api == "v3" else settings.api_v2_url
        url = f"{base}{path}"
        for attempt in range(3):
            resp = await self.client.post(url, json=body)
            if await self._handle_rate_limit(resp):
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()

    async def put(self, path: str, body: dict, api: str = "v3") -> dict:
        base = settings.api_v3_url if api == "v3" else settings.api_v2_url
        url = f"{base}{path}"
        for attempt in range(3):
            resp = await self.client.put(url, json=body)
            if await self._handle_rate_limit(resp):
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()

    async def delete(self, path: str, api: str = "v3") -> bool:
        base = settings.api_v3_url if api == "v3" else settings.api_v2_url
        url = f"{base}{path}"
        for attempt in range(3):
            resp = await self.client.delete(url)
            if await self._handle_rate_limit(resp):
                continue
            resp.raise_for_status()
            return True
        resp.raise_for_status()

    # --- Convenience for raw URLs (non-standard API paths) ---

    async def raw_get(self, url: str, **params) -> dict | list:
        full_url = f"{settings.jira_url.rstrip('/')}{url}"
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        resp = await self.client.get(full_url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def raw_post(self, url: str, body: dict) -> dict:
        full_url = f"{settings.jira_url.rstrip('/')}{url}"
        resp = await self.client.post(full_url, json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def raw_put(self, url: str, body: dict) -> dict:
        full_url = f"{settings.jira_url.rstrip('/')}{url}"
        resp = await self.client.put(full_url, json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def raw_delete(self, url: str) -> bool:
        full_url = f"{settings.jira_url.rstrip('/')}{url}"
        resp = await self.client.delete(full_url)
        resp.raise_for_status()
        return True

    # --- Automation API (internal gateway) ---

    async def _automation_url(self, scope: str, path: str = "") -> str:
        """Build the internal gateway automation URL.

        Args:
            scope: 'GLOBAL' for site-wide rules, or a project ID for
                   project-scoped rules.
            path: optional trailing path (e.g. '/<rule_id>')
        """
        cloud_id = await self.get_cloud_id()
        if not cloud_id:
            raise RuntimeError(
                "Cloud ID not available. Set JIRA_CLOUD_ID in .env or "
                "ensure _edge/tenant_info is accessible."
            )
        base = settings.jira_url.rstrip("/")
        return (
            f"{base}/gateway/api/automation/internal-api/jira/"
            f"{cloud_id}/pro/rest/{scope}/rules{path}"
        )

    async def automation_get(self, scope: str, path: str = "", **params) -> dict | list:
        """GET from the automation internal API."""
        url = await self._automation_url(scope, path)
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        resp = await self.client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def automation_put(self, scope: str, path: str = "", body: dict | None = None) -> dict:
        """PUT to the automation internal API."""
        url = await self._automation_url(scope, path)
        resp = await self.client.put(url, json=body or {})
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


jira = JiraCloudClient()
