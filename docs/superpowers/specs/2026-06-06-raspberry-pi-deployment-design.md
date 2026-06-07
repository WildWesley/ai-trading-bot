# Raspberry Pi Deployment Design

## Goal

Run the trading bot 24/7 on a Raspberry Pi with the Streamlit dashboard accessible from any device via a secure public HTTPS URL (Cloudflare Tunnel). The bot auto-starts on boot and auto-restarts on crash. The dashboard is read-only.

## Prerequisites

- Raspberry Pi 4 or 5 running Raspberry Pi OS Bookworm (64-bit)
- Raspberry Pi on the same network as your router (Ethernet recommended)
- Free Cloudflare account at cloudflare.com
- Private GitHub repo with the trading bot code (set up first)
- `.env` file with valid Alpaca and Anthropic API keys

## Architecture

```
[Your phone/laptop anywhere]
        │  HTTPS
        ▼
[Cloudflare Edge]
        │  Tunnel (outbound from Pi, no port-forward needed)
        ▼
[Raspberry Pi]
  ├─ systemd: trading-bot.service
  │    └─ python -m bot.main --headless  (bot + Streamlit on :8501)
  └─ systemd: cloudflared.service
       └─ cloudflared tunnel run         (tunnels :8501 → public URL)
```

The `cloudflared` daemon makes an outbound connection to Cloudflare — no router port-forwarding is needed, and your home IP is never exposed.

## Files

| Path | What it is |
|------|-----------|
| `deploy/setup-pi.sh` | One-shot setup script (run once after cloning) |
| `deploy/trading-bot.service` | systemd unit for the bot |
| `deploy/cloudflared.service` | systemd unit for the tunnel daemon |
| `deploy/README.md` | Step-by-step deployment guide |
| `.streamlit/config.toml` | Add `[server]` block: headless, address, port |
| `.gitignore` | Add `data/*.log` |
| `.env.example` | Template for the required environment variables |

## `setup-pi.sh`

Installs all dependencies and configures the tunnel. Run once as the `pi` user:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# System packages
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git curl

# Python virtualenv + dependencies
python3 -m venv "$REPO_DIR/venv"
"$REPO_DIR/venv/bin/pip" install --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# Node.js (for Claude Code CLI)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g @anthropic-ai/claude-code

# cloudflared
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update -y && sudo apt-get install -y cloudflared

# Install systemd units
sudo cp "$REPO_DIR/deploy/trading-bot.service" /etc/systemd/system/
sudo cp "$REPO_DIR/deploy/cloudflared.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo ""
echo "Setup complete. Next steps:"
echo "  1. Copy .env.example to .env and fill in your API keys:"
echo "     cp $REPO_DIR/.env.example $REPO_DIR/.env && nano $REPO_DIR/.env"
echo "  2. Authenticate cloudflared (one-time, opens browser):"
echo "     cloudflared tunnel login"
echo "  3. Create and configure the tunnel:"
echo "     cloudflared tunnel create trading-bot"
echo "     cloudflared tunnel route dns trading-bot trading-bot"
echo "  4. Enable and start the services:"
echo "     sudo systemctl enable --now trading-bot cloudflared"
echo "  5. Check status:"
echo "     sudo systemctl status trading-bot cloudflared"
```

## `trading-bot.service`

```ini
[Unit]
Description=AI Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ai-trading-bot/trading-bot
ExecStart=/home/pi/ai-trading-bot/trading-bot/venv/bin/python -m bot.main --headless
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`--headless` suppresses the `webbrowser.open()` call — there's no desktop on the Pi. Streamlit still starts on port 8501.

## `cloudflared.service`

```ini
[Unit]
Description=Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/cloudflared tunnel --config /home/pi/.cloudflared/config.yml run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Cloudflare Tunnel config (`~/.cloudflared/config.yml`, created by setup steps):

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/pi/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: trading-bot.<your-domain>.workers.dev
    service: http://localhost:8501
  - service: http_status:404
```

## `.streamlit/config.toml` changes

Add a `[server]` section so Streamlit binds to all interfaces (required for the tunnel to reach it):

```toml
[server]
headless = true
address = "0.0.0.0"
port = 8501
```

## Deployment Workflow

After the one-time setup, updating the bot on the Pi is:

```bash
cd ~/ai-trading-bot
git pull
~/ai-trading-bot/trading-bot/venv/bin/pip install -r trading-bot/requirements.txt
sudo systemctl restart trading-bot
```

## Ongoing Git Workflow

Throughout development on Windows:
```bash
git add <files>
git commit -m "feat: description"
git push
```

On Pi to pick up changes:
```bash
git pull && sudo systemctl restart trading-bot
```

## Security Notes

- The dashboard is read-only by design (no trading controls in Streamlit)
- API keys stay in `.env` on the Pi only — never committed to git
- Cloudflare handles TLS; your home IP is never exposed
- The Cloudflare free tier supports one tunnel with unlimited traffic
