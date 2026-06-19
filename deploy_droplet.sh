#!/bin/bash
# Deploy trading agent to a DigitalOcean droplet
# Usage: ./deploy_droplet.sh <droplet_ip>

set -e

if [ -z "$1" ]; then
    echo "Usage: ./deploy_droplet.sh <droplet_ip>"
    exit 1
fi

DROPLET_IP=$1
REMOTE_DIR="/opt/trading-agent"
SSH_KEY="$HOME/.ssh/id_rsa_digitalocean"

echo "Deploying to $DROPLET_IP..."

if [ ! -f ".env" ]; then
    echo "Missing .env. Copy .env.example to .env and fill in your own secrets first."
    exit 1
fi

# Create remote directory
ssh -i "$SSH_KEY" root@$DROPLET_IP "mkdir -p $REMOTE_DIR/data"

# Upload application files. Keep this broad enough that support modules used by
# trading_cycle.py, the reviewer, research, and dashboard API do not go stale.
scp -i "$SSH_KEY" *.py requirements.txt Dockerfile docker-compose.yml .env root@$DROPLET_IP:$REMOTE_DIR/

if [ -f "data/modelbound_cache.json" ]; then
    scp -i "$SSH_KEY" data/modelbound_cache.json root@$DROPLET_IP:$REMOTE_DIR/data/
fi

# Build and start on remote
ssh -i "$SSH_KEY" root@$DROPLET_IP "cd $REMOTE_DIR && docker compose up -d --build"

echo ""
echo "Deployment complete!"
echo "Check logs: ssh -i \"$SSH_KEY\" root@$DROPLET_IP 'cd $REMOTE_DIR && docker compose logs -f'"
echo "Stop agent: ssh -i \"$SSH_KEY\" root@$DROPLET_IP 'cd $REMOTE_DIR && docker compose down'"
