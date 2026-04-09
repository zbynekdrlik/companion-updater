# Bitfocus Companion Setup

Production setup for [Bitfocus Companion](https://bitfocus.io/companion) with USB hot-plug support and remote access.

## Architecture

- **Companion**: Native systemd service via [companion-pi](https://github.com/bitfocus/companion-pi) — direct USB/udev access for reliable Stream Deck hot-plug detection
- **Cloudflare Tunnel**: Native systemd service for remote access without port forwarding
- **Update Dashboard**: Docker container for checking and applying Companion updates
- **Persistent Data**: Configuration stored at `~companion/.config/companion-nodejs/`

## Quick Start

### 1. Install Companion (native)

```bash
curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash
```

This installs Companion as a systemd service with USB support, udev rules, and auto-start on boot.

### 2. Install Update Dashboard (Docker)

```bash
sudo mkdir -p /opt/companion-updater
sudo chown $USER:$USER /opt/companion-updater
cp -r updater/* /opt/companion-updater/
cd /opt/companion-updater
docker compose up -d --build
```

### 3. Access

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
1. Installs udev rules from `host/` to `/etc/udev/rules.d/`
2. Updates Companion via `companion-update`
3. Restarts the Companion systemd service
4. Updates the companion-updater Docker container
5. Waits for health checks to pass

**Requirements:** `sshpass` must be installed on the machine running the deploy.

## Directory Structure

```
Repository:
├── companion/           # Docker setup (legacy, kept for reference)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── entrypoint.sh
│   └── .env.example
├── updater/             # Update Dashboard (Docker)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── app/
├── host/                # Udev rules for the target host
│   ├── 50-elgato.rules
│   └── 99-nldevicessetup.rules
├── deploy.sh            # Deploy to remote host via SSH
├── setup.sh             # One-click local installer (legacy)
└── README.md
```

## Configuration

### Cloudflare Tunnel

Install cloudflared natively on the host:

```bash
sudo cloudflared service install <TUNNEL_TOKEN>
```

Get the token from [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → Networks → Tunnels.

## Ports

| Service | Port | Description |
|---------|------|-------------|
| Companion | 8000 | Main web interface |
| Companion | 16622 | Satellite API |
| Update Dashboard | 8081 | Update management UI |

## How Updates Work

1. Run `sudo companion-update` on the host, or
2. Use the Update Dashboard at port 8081 to check for updates and apply them

Configuration is preserved across updates.

## Troubleshooting

### Stream Deck not detected
```bash
# Check that hidraw devices exist
ls -la /dev/hidraw*
# Verify udev rules are installed
ls -la /etc/udev/rules.d/50-companion.rules
# Check Companion service logs
sudo journalctl -u companion --no-pager -n 50 | grep -i stream
# Restart Companion service
sudo systemctl restart companion
```

### Companion won't start
```bash
# Check service status
sudo systemctl status companion
# Check full logs
sudo journalctl -u companion --no-pager -n 100
```

### Cloudflare tunnel not working
```bash
# Check tunnel service
sudo systemctl status cloudflared
# Check tunnel logs
sudo journalctl -u cloudflared --no-pager -n 50
```

### Update Dashboard issues
```bash
# Check Docker logs
docker logs companion-updater
```

## License

MIT
