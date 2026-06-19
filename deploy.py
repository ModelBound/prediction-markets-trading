"""Deploy the trading agent to DigitalOcean App Platform."""
import json
import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

DO_TOKEN = os.getenv("DIGITALOCEAN_TOKEN")
DO_API = "https://api.digitalocean.com/v2"

HEADERS = {
    "Authorization": f"Bearer {DO_TOKEN}",
    "Content-Type": "application/json",
}


def create_app_spec():
    """Create the DigitalOcean App Platform spec."""
    return {
        "spec": {
            "name": "kalshi-trading-agent",
            "region": "nyc",
            "workers": [
                {
                    "name": "trading-agent",
                    "dockerfile_path": "Dockerfile",
                    "github": None,  # Will use image registry instead
                    "envs": [
                        {"key": "KALSHI_API_KEY_ID", "value": os.getenv("KALSHI_API_KEY_ID"), "type": "SECRET"},
                        {"key": "KALSHI_API_RSA", "value": os.getenv("KALSHI_API_RSA"), "type": "SECRET"},
                        {"key": "OPENAI_KEY", "value": os.getenv("OPENAI_KEY"), "type": "SECRET"},
                        {"key": "PREDICTION_MARKET_PROVIDER", "value": os.getenv("PREDICTION_MARKET_PROVIDER", "kalshi"), "type": "GENERAL"},
                        {"key": "AI_PROVIDER", "value": os.getenv("AI_PROVIDER", "openai"), "type": "GENERAL"},
                        {"key": "TRADING_MODE", "value": "demo", "type": "GENERAL"},
                    ],
                    "instance_count": 1,
                    "instance_size_slug": "apps-s-1vcpu-0.5gb",
                }
            ],
        }
    }


def create_droplet_spec():
    """Create a DigitalOcean Droplet spec for the trading agent."""
    # Cloud-init script to set up the droplet
    user_data = f"""#!/bin/bash
set -e

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Create app directory
mkdir -p /opt/trading-agent/data

# Write environment file
cat > /opt/trading-agent/.env << 'ENVEOF'
KALSHI_API_KEY_ID={os.getenv('KALSHI_API_KEY_ID')}
KALSHI_API_RSA={os.getenv('KALSHI_API_RSA')}
OPENAI_KEY={os.getenv('OPENAI_KEY')}
PREDICTION_MARKET_PROVIDER={os.getenv('PREDICTION_MARKET_PROVIDER', 'kalshi')}
AI_PROVIDER={os.getenv('AI_PROVIDER', 'openai')}
TRADING_MODE=demo
ENVEOF

# Write docker-compose
cat > /opt/trading-agent/docker-compose.yml << 'COMPOSEEOF'
version: '3.8'
services:
  trading-agent:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
COMPOSEEOF

echo "Setup complete. Upload code and run: docker compose up -d"
"""

    return {
        "name": "kalshi-trading-agent",
        "region": "nyc1",
        "size": "s-1vcpu-1gb",
        "image": "docker-20-04",
        "ssh_keys": [int(os.getenv("DIGITALOCEAN_SSH_KEY_ID", "0"))] if os.getenv("DIGITALOCEAN_SSH_KEY_ID") else [],
        "user_data": user_data,
        "tags": ["trading-agent"],
    }


def list_droplets():
    """List existing droplets."""
    resp = requests.get(f"{DO_API}/droplets", headers=HEADERS, params={"tag_name": "trading-agent"})
    if resp.status_code == 200:
        return resp.json().get("droplets", [])
    print(f"Error listing droplets: {resp.status_code} {resp.text}")
    return []


def create_droplet():
    """Create a new droplet for the trading agent."""
    spec = create_droplet_spec()
    print(f"Creating droplet: {spec['name']} in {spec['region']}...")

    resp = requests.post(f"{DO_API}/droplets", headers=HEADERS, json=spec)
    if resp.status_code == 202:
        droplet = resp.json()["droplet"]
        print(f"Droplet created! ID: {droplet['id']}")
        print("Waiting for IP address...")

        # Wait for droplet to be active
        for _ in range(30):
            time.sleep(10)
            check = requests.get(f"{DO_API}/droplets/{droplet['id']}", headers=HEADERS)
            if check.status_code == 200:
                d = check.json()["droplet"]
                if d["status"] == "active":
                    networks = d.get("networks", {}).get("v4", [])
                    public_ips = [n["ip_address"] for n in networks if n["type"] == "public"]
                    if public_ips:
                        print(f"\nDroplet active! IP: {public_ips[0]}")
                        print(f"\nNext steps:")
                        print(f"  1. SSH into the droplet: ssh root@{public_ips[0]}")
                        print(f"  2. Upload code: scp -r *.py Dockerfile requirements.txt root@{public_ips[0]}:/opt/trading-agent/")
                        print(f"  3. Build and run: cd /opt/trading-agent && docker compose up -d")
                        print(f"  4. Check logs: docker compose logs -f")
                        return droplet
            print(".", end="", flush=True)

        print("\nDroplet created but not yet active. Check DigitalOcean dashboard.")
        return droplet
    else:
        print(f"Error creating droplet: {resp.status_code} {resp.text}")
        return None


def destroy_droplet(droplet_id):
    """Destroy a droplet."""
    resp = requests.delete(f"{DO_API}/droplets/{droplet_id}", headers=HEADERS)
    if resp.status_code == 204:
        print(f"Droplet {droplet_id} destroyed.")
    else:
        print(f"Error: {resp.status_code} {resp.text}")


def get_account():
    """Get DigitalOcean account info."""
    resp = requests.get(f"{DO_API}/account", headers=HEADERS)
    if resp.status_code == 200:
        account = resp.json()["account"]
        print(f"Account: {account['email']}")
        print(f"Status: {account['status']}")
        print(f"Droplet Limit: {account['droplet_limit']}")
        return account
    print(f"Error: {resp.status_code} {resp.text}")
    return None


if __name__ == "__main__":
    if not DO_TOKEN:
        print("Error: DIGITALOCEAN_TOKEN not set")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python deploy.py [account|list|create|destroy <id>]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "account":
        get_account()
    elif command == "list":
        droplets = list_droplets()
        if droplets:
            for d in droplets:
                ips = [n["ip_address"] for n in d.get("networks", {}).get("v4", []) if n["type"] == "public"]
                print(f"  {d['id']}: {d['name']} ({d['status']}) - {ips[0] if ips else 'no IP'}")
        else:
            print("No trading agent droplets found.")
    elif command == "create":
        create_droplet()
    elif command == "destroy" and len(sys.argv) > 2:
        destroy_droplet(sys.argv[2])
    else:
        print("Usage: python deploy.py [account|list|create|destroy <id>]")
