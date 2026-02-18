import httpx
import time
import logging
from typing import Optional, Dict, Any

from ..config import get_settings

logger = logging.getLogger(__name__)


class GitHubClient:
    """Client for GitHub API to fetch release information."""

    def __init__(self):
        self.settings = get_settings()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0

    def _is_cache_valid(self) -> bool:
        """Check if the cached data is still valid."""
        if self._cache is None:
            return False
        return (time.time() - self._cache_time) < self.settings.github_cache_ttl

    async def get_latest_release(self) -> Dict[str, Any]:
        """Fetch the latest release from GitHub API.

        Returns cached data if available and not expired.

        Returns:
            Dict with release information including:
            - tag_name: Version tag (e.g., "v4.2.4")
            - name: Release name
            - published_at: Release date
            - html_url: URL to release page
        """
        if self._is_cache_valid():
            logger.debug("Returning cached GitHub release data")
            return self._cache

        url = f"{self.settings.github_api_base}/repos/{self.settings.github_repo}/releases/latest"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers={"Accept": "application/vnd.github.v3+json"},
                    timeout=10.0
                )
                response.raise_for_status()

                data = response.json()
                self._cache = {
                    "tag_name": data.get("tag_name", ""),
                    "name": data.get("name", ""),
                    "published_at": data.get("published_at", ""),
                    "html_url": data.get("html_url", ""),
                    "body": data.get("body", "")[:500]  # Truncate release notes
                }
                self._cache_time = time.time()

                logger.info(f"Fetched latest release: {self._cache['tag_name']}")
                return self._cache

        except httpx.HTTPStatusError as e:
            logger.error(f"GitHub API HTTP error: {e.response.status_code}")
            raise RuntimeError(f"GitHub API error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"GitHub API request error: {e}")
            raise RuntimeError(f"Failed to connect to GitHub: {e}")

    def clear_cache(self):
        """Clear the cached release data."""
        self._cache = None
        self._cache_time = 0


# Singleton instance
github_client = GitHubClient()
