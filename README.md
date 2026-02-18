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
├── setup.sh             # One-click installer
└── README.md

On your server after setup:
/opt/companion/          # Persistent data (configs, buttons, connections)
/opt/companion-docker/   # Companion container files
/opt/companion-updater/  # Update dashboard files
```

## Configuration

### Environment Variables (companion/.env)

| Variable | Description |
|----------|-------------|
| `COMPANION_USB_GID` | Group ID for USB access (default: 983) |
| `CLOUDFLARE_TUNNEL_TOKEN` | Optional: Cloudflare Tunnel token for remote access |

### Finding USB Group ID

```bash
# Find the group ID for USB devices
getent group | grep -E 'usb|plugdev'
# Or check Stream Deck device
ls -la /dev/hidraw*
```

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
# Check USB group
ls -la /dev/hidraw*
# Update COMPANION_USB_GID in .env to match the group
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
