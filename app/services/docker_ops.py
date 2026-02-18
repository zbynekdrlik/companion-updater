import subprocess
import json
import logging
import time
from pathlib import Path
from typing import Optional, Generator, Dict, Any

from ..config import get_settings

logger = logging.getLogger(__name__)


class DockerOperations:
    """Handle Docker operations for Companion container management."""

    def __init__(self):
        self.settings = get_settings()
        self._last_update_time: float = 0

    def can_update(self) -> bool:
        """Check if enough time has passed since the last update."""
        elapsed = time.time() - self._last_update_time
        return elapsed >= self.settings.update_cooldown

    def get_cooldown_remaining(self) -> int:
        """Get seconds remaining until next update allowed."""
        elapsed = time.time() - self._last_update_time
        remaining = self.settings.update_cooldown - elapsed
        return max(0, int(remaining))

    def get_running_version(self) -> Optional[str]:
        """Get the version of the running Companion container.

        Reads the version from the container's image labels.

        Returns:
            Version string like "4.2.3" or None if not found
        """
        container_name = self.settings.companion_container_name

        try:
            # Get version from container image labels
            result = subprocess.run(
                [
                    "docker", "inspect", "--format",
                    '{{index .Config.Labels "org.opencontainers.image.version"}}',
                    container_name
                ],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                logger.error(f"Container {container_name} not found")
                return None

            version = result.stdout.strip()
            if version and version != "<no value>":
                # Remove 'v' prefix if present for consistency
                version = version.lstrip("v")
                logger.info(f"Current Companion version: {version}")
                return version

            logger.warning("Version label not found in container")
            return None

        except subprocess.SubprocessError as e:
            logger.error(f"Subprocess error: {e}")

        return None

    def get_container_status(self) -> Dict[str, Any]:
        """Get the current status of the Companion container."""
        container_name = self.settings.companion_container_name

        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                return {
                    "exists": False,
                    "status": "not found",
                    "running": False
                }

            status = result.stdout.strip()
            return {
                "exists": True,
                "status": status,
                "running": status == "running"
            }

        except subprocess.SubprocessError as e:
            return {
                "exists": False,
                "status": f"error: {e}",
                "running": False
            }

    def pull_base_image(self) -> Generator[str, None, None]:
        """Pull the latest Companion base image.

        Yields:
            Progress messages
        """
        yield "Pulling latest Companion base image..."

        try:
            # Pull the image using Docker CLI for better progress output
            process = subprocess.Popen(
                ["docker", "pull", "ghcr.io/bitfocus/companion/companion:latest"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if line:
                    yield f"  {line}"

            process.wait()

            if process.returncode != 0:
                raise RuntimeError("Failed to pull base image")

            yield "Base image pulled successfully"

        except subprocess.SubprocessError as e:
            logger.error(f"Failed to pull image: {e}")
            raise RuntimeError(f"Failed to pull image: {e}")

    def rebuild_image(self) -> Generator[str, None, None]:
        """Rebuild the Companion image with docker compose.

        Yields:
            Progress messages
        """
        yield "Rebuilding Companion image..."

        companion_path = Path(self.settings.companion_docker_path)

        if not companion_path.exists():
            raise RuntimeError(f"Companion directory not found: {companion_path}")

        try:
            process = subprocess.Popen(
                ["docker", "compose", "build", "--no-cache"],
                cwd=str(companion_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if line:
                    yield f"  {line}"

            process.wait()

            if process.returncode != 0:
                raise RuntimeError("Failed to rebuild image")

            yield "Image rebuilt successfully"

        except subprocess.SubprocessError as e:
            logger.error(f"Failed to rebuild image: {e}")
            raise RuntimeError(f"Failed to rebuild image: {e}")

    def restart_container(self) -> Generator[str, None, None]:
        """Restart the Companion container with docker compose.

        Yields:
            Progress messages
        """
        yield "Restarting Companion container..."

        companion_path = Path(self.settings.companion_docker_path)

        try:
            process = subprocess.Popen(
                ["docker", "compose", "up", "-d"],
                cwd=str(companion_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if line:
                    yield f"  {line}"

            process.wait()

            if process.returncode != 0:
                raise RuntimeError("Failed to restart container")

            yield "Container restarted successfully"

        except subprocess.SubprocessError as e:
            logger.error(f"Failed to restart container: {e}")
            raise RuntimeError(f"Failed to restart container: {e}")

    def perform_update(self) -> Generator[str, None, None]:
        """Perform a full update of the Companion container.

        Yields:
            Progress messages for each step
        """
        if not self.can_update():
            remaining = self.get_cooldown_remaining()
            raise RuntimeError(f"Update cooldown active. Please wait {remaining} seconds.")

        logger.info("Starting Companion update process")
        yield "Starting update process..."

        # Pull latest base image
        yield from self.pull_base_image()

        # Rebuild the image
        yield from self.rebuild_image()

        # Restart the container
        yield from self.restart_container()

        # Update cooldown timer
        self._last_update_time = time.time()

        # Wait a moment for container to start
        yield "Waiting for Companion to start..."
        time.sleep(5)

        # Verify the new version
        new_version = self.get_running_version()
        if new_version:
            yield f"Update complete! Now running version {new_version}"
        else:
            yield "Update complete! Could not verify new version."

        logger.info("Companion update completed")


# Singleton instance
docker_ops = DockerOperations()
