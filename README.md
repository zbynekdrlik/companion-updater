# Bitfocus Companion Docker Setup

Complete Docker setup for [Bitfocus Companion](https://bitfocus.io/companion) with automatic updates dashboard.

## Features

- **Companion Container**: Custom image with mDNS/Avahi support, Cloudflare Tunnel, Stream Deck USB access
- **Update Dashboard**: Web UI to check for updates and one-click update Companion
- **Persistent Data**: All configuration stored on host, survives updates
- **USB Support**: Stream Deck and other USB devices work out of the box

## Quick Start

```bash
# Clone the repository
git clone https://github.com/zbynekdrlik/companion-updater.git
cd companion-updater

# Run setup script
chmod +x setup.sh
./setup.sh
```

That's it! Access:
- **Companion**: `http://<your-ip>:8000`
- **Update Dashboard**: `http://<your-ip>:8081`

## Remote Deploy

Deploy to `companion.lan` (or any target host) over SSH:

```bash
# Default: deploys to companion.lan as newlevel
./deploy.sh

# Override target host, user, or password
COMPANION_HOST=10.0.0.50 COMPANION_USER=admin COMPANION_PASS=secret ./deploy.sh
```

The deploy script:
1. Copies `companion/` and `updater/` files to the target
2. Installs udev rules from `host/` to `/etc/udev/rules.d/`
3. Rebuilds and restarts both Docker containers
4. Waits for health checks to pass

**Requirements:** `sshpass` must be installed on the machine running the deploy.

## Manual Installation

### 1. Companion

```bash
# Create directories
sudo mkdir -p /opt/companion /opt/companion-docker
sudo chown $USER:$USER /opt/companion /opt/companion-docker

# Copy files
cp -r companion/* /opt/companion-docker/
cp companion/.env.example /opt/companion-docker/.env

# Edit configuration (optional)
nano /opt/companion-docker/.env

# Start Companion
cd /opt/companion-docker
docker compose up -d --build
```

### 2. Update Dashboard

```bash
# Copy files
sudo mkdir -p /opt/companion-updater
sudo chown $USER:$USER /opt/companion-updater
cp -r updater/* /opt/companion-updater/

# Start dashboard
cd /opt/companion-updater
docker compose up -d --build
```

## Directory Structure

```
Repository:
├── companion/           # Companion Docker setup
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── entrypoint.sh
│   └── .env.example
├── updater/             # Update Dashboard
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── app/
├── host/                # Udev rules for the target host
│   ├── 50-elgato.rules
│   └── 99-nldevicessetup.rules
├── deploy.sh            # Deploy to remote host via SSH
├── setup.sh             # One-click local installer
└── README.md
```

## Configuration

### Environment Variables (companion/.env)

| Variable | Description |
|----------|-------------|
| `CLOUDFLARE_TUNNEL_TOKEN` | Optional: Cloudflare Tunnel token for remote access |

## Ports

| Service | Port | Description |
|---------|------|-------------|
| Companion | 8000 | Main web interface |
| Companion | 16622 | Satellite API |
| Update Dashboard | 8081 | Update management UI |

## How Updates Work

1. Dashboard checks GitHub for latest Companion release
2. Click "Update Now" to:
   - Pull latest `ghcr.io/bitfocus/companion/companion:latest`
   - Rebuild your custom image
   - Restart Companion container
3. Your data in `/opt/companion` is preserved

## Included Features

### Companion Container
- **mDNS/Avahi**: Device discovery on local network
- **Cloudflare Tunnel**: Optional remote access without port forwarding
- **USB passthrough**: Stream Deck and other controllers
- **Timezone**: Configurable (default: Europe/Bratislava)

### Update Dashboard
- Version comparison (current vs latest)
- Live update progress via Server-Sent Events
- Rate limiting (5-minute cooldown)
- Container health monitoring

## Troubleshooting

### Stream Deck not detected
```bash
# Check that hidraw devices exist
ls -la /dev/hidraw*
# Verify udev rules are installed
ls -la /etc/udev/rules.d/50-elgato.rules
# Verify the container is running in privileged mode
docker inspect companion --format='{{.HostConfig.Privileged}}'
```

### Container won't start
```bash
# Check logs
docker logs companion
docker logs companion-updater
```

### Update fails
```bash
# Manual update
cd /opt/companion-docker
docker compose pull
docker compose build --no-cache
docker compose up -d
```

## License

MIT
