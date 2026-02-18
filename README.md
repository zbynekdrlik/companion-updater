# Companion Update Dashboard

A lightweight web dashboard for managing Bitfocus Companion container updates.

![Dashboard Screenshot](https://img.shields.io/badge/Companion-Update%20Dashboard-blue)

## Features

- View current vs latest Companion version
- One-click updates with live progress streaming
- Rate limiting (5-minute cooldown between updates)
- Container status monitoring
- GitHub API integration for version checking

## Prerequisites

- Docker and Docker Compose
- Bitfocus Companion running in Docker
- Companion container named `companion`
- Companion docker-compose at `/opt/companion-docker/`

## Quick Start

1. Clone this repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/companion-updater.git /opt/companion-updater
   ```

2. Update your Companion Dockerfile to use the `:latest` tag:
   ```dockerfile
   FROM ghcr.io/bitfocus/companion/companion:latest
   ```

3. Start the dashboard:
   ```bash
   cd /opt/companion-updater
   docker compose up -d --build
   ```

4. Access the dashboard at `http://<your-server-ip>:8081`

## Configuration

Environment variables (set in `docker-compose.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPANION_DOCKER_PATH` | `/opt/companion-docker` | Path to Companion's docker-compose directory |
| `COMPANION_CONTAINER_NAME` | `companion` | Name of the Companion container |
| `GITHUB_REPO` | `bitfocus/companion` | GitHub repository to check for releases |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard HTML page |
| `/api/status` | GET | JSON with version status |
| `/api/update` | POST | Trigger update (non-streaming) |
| `/api/update/stream` | GET | SSE stream for live update progress |

## How Updates Work

1. Pulls the latest `ghcr.io/bitfocus/companion/companion:latest` image
2. Rebuilds your custom Companion image with `docker compose build --no-cache`
3. Restarts Companion with `docker compose up -d`
4. Verifies the new version is running

**Your Companion data is preserved** - it's stored in `/opt/companion` on the host, not inside the container.

## Architecture

```
Browser (LAN) --> Update Dashboard (port 8081)
                        |
                        +-- GitHub API (check latest version)
                        +-- Docker socket (rebuild/restart Companion)
```

## File Structure

```
/opt/companion-updater/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
└── app/
    ├── __init__.py
    ├── main.py              # FastAPI app + HTML template
    ├── config.py            # Settings
    └── services/
        ├── __init__.py
        ├── docker_ops.py    # Docker operations
        ├── github.py        # GitHub API client
        └── version.py       # Version comparison
```

## Security Notes

- Dashboard is intended for LAN access only
- Docker socket is mounted for container control
- Rate limiting prevents accidental repeated updates

## License

MIT
