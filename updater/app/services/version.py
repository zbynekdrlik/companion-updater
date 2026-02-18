import re
from typing import Tuple, Optional


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a version string into a tuple of integers for comparison.

    Args:
        version_str: Version string like "4.2.3" or "v4.2.3"

    Returns:
        Tuple of integers like (4, 2, 3)
    """
    # Remove 'v' prefix if present
    version_str = version_str.lstrip("v")

    # Extract numeric parts
    parts = re.findall(r"\d+", version_str)
    return tuple(int(p) for p in parts)


def compare_versions(current: str, latest: str) -> int:
    """Compare two version strings.

    Args:
        current: Current version string
        latest: Latest version string

    Returns:
        -1 if current < latest (update available)
         0 if current == latest (up to date)
         1 if current > latest (ahead of release)
    """
    current_parts = parse_version(current)
    latest_parts = parse_version(latest)

    # Pad shorter tuple with zeros
    max_len = max(len(current_parts), len(latest_parts))
    current_padded = current_parts + (0,) * (max_len - len(current_parts))
    latest_padded = latest_parts + (0,) * (max_len - len(latest_parts))

    if current_padded < latest_padded:
        return -1
    elif current_padded > latest_padded:
        return 1
    return 0


def is_update_available(current: str, latest: str) -> bool:
    """Check if an update is available.

    Args:
        current: Current version string
        latest: Latest version string

    Returns:
        True if latest > current
    """
    return compare_versions(current, latest) < 0


def format_version(version: Optional[str]) -> str:
    """Format a version string for display.

    Args:
        version: Version string or None

    Returns:
        Formatted version string
    """
    if not version:
        return "Unknown"
    # Ensure 'v' prefix for display
    if not version.startswith("v"):
        return f"v{version}"
    return version
