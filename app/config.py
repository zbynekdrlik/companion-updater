from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Companion Docker configuration
    companion_docker_path: str = "/opt/companion-docker"
    companion_container_name: str = "companion"

    # GitHub configuration
    github_repo: str = "bitfocus/companion"
    github_api_base: str = "https://api.github.com"

    # Rate limiting (seconds between updates)
    update_cooldown: int = 300  # 5 minutes

    # Cache TTL for GitHub API (seconds)
    github_cache_ttl: int = 60  # 1 minute

    class Config:
        env_prefix = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
