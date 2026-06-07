#!/usr/bin/env bash
# One-shot Raspberry Pi setup for the AI trading bot.
# Run once after cloning:  bash deploy/setup-pi.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
RUN_HOME="$HOME"

echo "==> Repo:  $REPO_DIR"
echo "==> User:  $RUN_USER"
echo "==> Home:  $RUN_HOME"

echo "==> Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git curl lsb-release

echo "==> Creating Python virtualenv and installing dependencies..."
python3 -m venv "$REPO_DIR/venv"
"$REPO_DIR/venv/bin/pip" install --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> Installing Node.js 20 and Claude Code CLI..."
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
sudo npm install -g @anthropic-ai/claude-code

echo "==> Installing cloudflared..."
if ! command -v cloudflared >/dev/null 2>&1; then
  sudo mkdir -p /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y cloudflared
fi

echo "==> Installing systemd units (substituting user/home)..."
for unit in trading-bot cloudflared; do
  sed -e "s/^WorkingDirectory=%h/WorkingDirectory=${RUN_HOME//\//\\/}/" \
      -e "s/^ExecStart=%h/ExecStart=${RUN_HOME//\//\\/}/" \
      "$REPO_DIR/deploy/${unit}.service" \
    | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
  # Run the service as the invoking (non-root) user.
  sudo sed -i "/^\[Service\]/a User=${RUN_USER}" \
    "/etc/systemd/system/${unit}.service"
done
sudo systemctl daemon-reload

cat <<EOF

==> Base setup complete.

Next (manual) steps — see deploy/README.md for detail:
  1. Create your .env:
       cp "$REPO_DIR/.env.example" "$REPO_DIR/.env" && nano "$REPO_DIR/.env"
  2. Authenticate and create the Cloudflare tunnel:
       cloudflared tunnel login
       cloudflared tunnel create trading-bot
       # then write ~/.cloudflared/config.yml (template in README) and:
       cloudflared tunnel route dns trading-bot <your-hostname>
  3. Enable and start the services:
       sudo systemctl enable --now trading-bot cloudflared
  4. Check status / logs:
       systemctl status trading-bot cloudflared
       journalctl -u trading-bot -f
EOF
