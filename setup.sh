#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Bitfocus Companion Docker Setup ==="
echo ""

# Create data directory
echo "Creating /opt/companion for persistent data..."
sudo mkdir -p /opt/companion
sudo chown "$USER:$USER" /opt/companion

# Copy companion setup to /opt/companion-docker
echo "Installing Companion to /opt/companion-docker..."
sudo mkdir -p /opt/companion-docker
sudo cp -r "$SCRIPT_DIR/companion/"* /opt/companion-docker/
sudo chown -R "$USER:$USER" /opt/companion-docker

# Create .env file if it doesn't exist
if [ ! -f /opt/companion-docker/.env ]; then
    echo "Creating .env file..."
    cp "$SCRIPT_DIR/companion/.env.example" /opt/companion-docker/.env
    echo "  Edit /opt/companion-docker/.env to configure settings"
fi

# Copy updater setup to /opt/companion-updater
echo "Installing Updater Dashboard to /opt/companion-updater..."
sudo mkdir -p /opt/companion-updater
sudo cp -r "$SCRIPT_DIR/updater/"* /opt/companion-updater/
sudo chown -R "$USER:$USER" /opt/companion-updater

echo ""
echo "=== Starting Services ==="

# Start Companion
echo "Starting Companion..."
cd /opt/companion-docker
docker compose up -d --build

# Start Updater
echo "Starting Update Dashboard..."
cd /opt/companion-updater
docker compose up -d --build

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Companion:         http://$(hostname -I | awk '{print $1}'):8000"
echo "Update Dashboard:  http://$(hostname -I | awk '{print $1}'):8081"
echo ""
echo "Data directory:    /opt/companion"
echo ""
