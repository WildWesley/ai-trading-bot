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
  # Install the prebuilt .deb straight from Cloudflare's GitHub releases for
  # this machine's architecture (arm64 on a 64-bit Pi). This avoids the apt
  # repo, which can 404 on some Raspberry Pi OS codenames.
  ARCH="$(dpkg --print-architecture)"
  curl -fsSL \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
    -o /tmp/cloudflared.deb
  sudo dpkg -i /tmp/cloudflared.deb || sudo apt-get install -f -y
  rm -f /tmp/cloudflared.deb
fi

echo "==> Installing systemd units (substituting repo/home)..."
for unit in trading-bot dashboard cloudflared tunnel-watchdog; do
  # Substitute placeholders with the real paths and install as a system unit.
  #   %REPO% -> the cloned repo directory (wherever it actually lives)
  #   %h     -> the user's home (used by cloudflared's --config path)
  # A '#' sed delimiter avoids escaping slashes; matches are global so the %h
  # mid-line in cloudflared's config path is also resolved. (System units don't
  # expand these specifiers to the User's values, so we bake in literals here.)
  sed -e "s#%REPO%#${REPO_DIR}#g" -e "s#%h#${RUN_HOME}#g" \
    "$REPO_DIR/deploy/${unit}.service" \
    | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
  # Run the service as the invoking (non-root) user.
  sudo sed -i "/^\[Service\]/a User=${RUN_USER}" \
    "/etc/systemd/system/${unit}.service"
done
sudo cp "$REPO_DIR/deploy/tunnel-watchdog.timer" /etc/systemd/system/tunnel-watchdog.timer
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
       sudo systemctl enable --now trading-bot dashboard cloudflared
       sudo systemctl enable --now tunnel-watchdog.timer
  4. Check status / logs:
       systemctl status trading-bot dashboard cloudflared
       systemctl status tunnel-watchdog.timer
       journalctl -u trading-bot -f
EOF
