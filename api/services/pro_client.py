import httpx
from ..settings import settings


class ProClient:
    """HTTP client to call the PRO server API."""

    def __init__(self):
        self.base_url = settings.pro_api_url
        self.license_key = settings.pro_license_key

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.license_key)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.is_configured:
            raise Exception("PRO mode not configured. Set CRAWLER_PRO_API_URL and CRAWLER_PRO_LICENSE_KEY.")

        headers = {"X-License-Key": self.license_key}
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            )
            if resp.status_code == 401:
                raise Exception("Invalid license key")
            if resp.status_code == 429:
                raise Exception("Rate limit exceeded")
            resp.raise_for_status()
            return resp.json()

    async def verify_key(self) -> dict:
        return await self._request("POST", "/pro/api/verify-key")

    async def get_usage(self) -> dict:
        return await self._request("GET", "/pro/api/usage")

    async def auto_add(self, url: str, site_id: str = None, site_name: str = None,
                       browser: bool = False) -> dict:
        return await self._request("POST", "/pro/api/auto-add", json={
            "url": url, "site_id": site_id, "site_name": site_name, "browser": browser,
        })

    async def summarize(self, text: str, title: str = None) -> dict:
        return await self._request("POST", "/pro/api/summarize", json={
            "text": text, "title": title,
        })


pro_client = ProClient()
