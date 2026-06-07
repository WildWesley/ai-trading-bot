# Raspberry Pi Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot deployable on a Raspberry Pi as an auto-restarting systemd service, with the Streamlit dashboard reachable from anywhere via a Cloudflare Tunnel (read-only).

**Architecture:** A `deploy/` folder holds a setup script, two systemd unit files, and a deployment README. The Streamlit config is updated to bind all interfaces. Most artifacts here are shell/config files validated by inspection and on-device runs rather than unit tests — so this plan uses creation + verification steps instead of TDD.

**Tech Stack:** Raspberry Pi OS Bookworm (64-bit), systemd, bash, cloudflared, Node.js (for Claude Code CLI), Python venv.

**Working directory for authoring (on Windows):** `C:\Users\natha\Programming\AI_Trading_Bot\trading-bot`. The on-device steps run on the Pi over SSH and are listed in `deploy/README.md`; they are NOT run from this authoring session.

**Prerequisite:** Task 1 of the trading-improvements plan (or at least the GitHub repo) is done. The repo already exists at `github.com/WildWesley/ai-trading-bot`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `deploy/setup-pi.sh` | One-shot Pi setup (deps, Node, cloudflared, units) | Create |
| `deploy/trading-bot.service` | systemd unit for the bot | Create |
| `deploy/cloudflared.service` | systemd unit for the tunnel | Create |
| `deploy/README.md` | Step-by-step deploy guide | Create |
| `.streamlit/config.toml` | Bind dashboard to all interfaces | Modify |

Note: line endings — `.gitattributes` already forces LF for `*.sh`, so the script will check out with Unix line endings on the Pi (critical: a CRLF shebang breaks bash).

---

## Task 1: Streamlit server binding

**Files:**
- Modify: `.streamlit/config.toml`

- [ ] **Step 1: Add a `[server]` section** — in `.streamlit/config.toml`, after the existing `[browser]` block (after line 15), add:

```toml

[server]
# Bind all interfaces so the Cloudflare Tunnel (and LAN) can reach the
# dashboard on the Pi. headless=true suppresses the first-run email prompt.
headless = true
address = "0.0.0.0"
port = 8501
```

- [ ] **Step 2: Verify the file parses as valid TOML**

Run: `python -c "import tomllib; tomllib.load(open('.streamlit/config.toml','rb')); print('TOML OK')"`
Expected: `TOML OK`

- [ ] **Step 3: Commit**

```bash
git add .streamlit/config.toml
git commit -m "feat: bind Streamlit dashboard to all interfaces for remote access

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 2: systemd unit files

**Files:**
- Create: `deploy/trading-bot.service`
- Create: `deploy/cloudflared.service`

The units use `%h` (the service user's home) instead of a hardcoded `/home/pi`, so they work regardless of the Pi username. They assume the repo is cloned to `~/ai-trading-bot` and the venv lives at `~/ai-trading-bot/trading-bot/venv`.

- [ ] **Step 1: Create `deploy/trading-bot.service`**

```ini
[Unit]
Description=AI Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/ai-trading-bot/trading-bot
ExecStart=%h/ai-trading-bot/trading-bot/venv/bin/python -m bot.main --headless
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create `deploy/cloudflared.service`**

```ini
[Unit]
Description=Cloudflare Tunnel for trading-bot dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared tunnel --config %h/.cloudflared/config.yml run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Verify both files are valid INI**

Run: `python -c "import configparser; [configparser.ConfigParser(strict=False).read(f) for f in ['deploy/trading-bot.service','deploy/cloudflared.service']]; print('units OK')"`
Expected: `units OK`

- [ ] **Step 4: Commit**

```bash
git add deploy/trading-bot.service deploy/cloudflared.service
git commit -m "feat: add systemd units for bot and cloudflare tunnel

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 3: Pi setup script

**Files:**
- Create: `deploy/setup-pi.sh`

This script is run once on the Pi after cloning. It is idempotent where practical (venv creation and apt installs can be re-run safely).

The units are **system** services (so they start at boot without anyone logging in). System units can't use `%h`, so the script substitutes the real home path and injects a `User=<login user>` line as it copies each unit into `/etc/systemd/system/`. That makes the bot run as your normal Pi user (not root) while still starting on boot.

- [ ] **Step 1: Create `deploy/setup-pi.sh`**

```bash
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
```

- [ ] **Step 2: Verify the script is syntactically valid bash**

Run (uses the Bash tool / WSL): `bash -n deploy/setup-pi.sh && echo "syntax OK"`
Expected: `syntax OK`

- [ ] **Step 3: Commit**

```bash
git add deploy/setup-pi.sh
git commit -m "feat: add one-shot Raspberry Pi setup script

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 4: Deployment README

**Files:**
- Create: `deploy/README.md`

- [ ] **Step 1: Create `deploy/README.md`**

````markdown
# Raspberry Pi Deployment

Run the trading bot 24/7 on a Raspberry Pi with the dashboard reachable
from anywhere via a Cloudflare Tunnel (read-only).

## Prerequisites

- Raspberry Pi 4/5 on Raspberry Pi OS Bookworm (64-bit), on your network
- A free Cloudflare account with a domain added to Cloudflare (needed for a
  named tunnel + DNS route). If you have no domain, use a Quick Tunnel
  (see "No-domain option" below) — the URL changes on restart.
- Your Alpaca paper keys and Anthropic key

## 1. Clone the repo

```bash
cd ~
git clone https://github.com/WildWesley/ai-trading-bot.git
cd ai-trading-bot/trading-bot
```

## 2. Run the setup script

```bash
bash deploy/setup-pi.sh
```

This installs Python deps (in `venv/`), Node.js + the Claude Code CLI,
`cloudflared`, and the two systemd units (resolved to your user/home).

## 3. Configure secrets

```bash
cp .env.example .env
nano .env   # paste ALPACA_API_KEY, ALPACA_SECRET_KEY, ANTHROPIC_API_KEY
```

## 4. Set up the Cloudflare Tunnel

```bash
cloudflared tunnel login                  # opens a browser auth flow
cloudflared tunnel create trading-bot     # prints a tunnel UUID + creds file
```

Create `~/.cloudflared/config.yml` (replace `<UUID>` and the hostname):

```yaml
tunnel: <UUID>
credentials-file: /home/<youruser>/.cloudflared/<UUID>.json

ingress:
  - hostname: trading-bot.yourdomain.com
    service: http://localhost:8501
  - service: http_status:404
```

Route DNS to the tunnel:

```bash
cloudflared tunnel route dns trading-bot trading-bot.yourdomain.com
```

## 5. Enable and start the services

```bash
sudo systemctl enable --now trading-bot cloudflared
systemctl status trading-bot cloudflared
```

The dashboard is now at `https://trading-bot.yourdomain.com`. It is
read-only — it shows account, positions, trades, equity curve, and the
strategy chart, with no controls to place or cancel trades.

## Updating the bot later

```bash
cd ~/ai-trading-bot
git pull
~/ai-trading-bot/trading-bot/venv/bin/pip install -r trading-bot/requirements.txt
sudo systemctl restart trading-bot
```

## Logs and troubleshooting

```bash
journalctl -u trading-bot -f     # live bot log
journalctl -u cloudflared -f     # live tunnel log
```

- **Bot exits immediately:** check `.env` exists and keys are valid paper
  keys (`journalctl -u trading-bot` shows the validation messages).
- **Dashboard 502 via the tunnel:** confirm Streamlit is up on the Pi
  (`curl -I http://localhost:8501`) and that `config.yml` points at port 8501.
- **Tunnel won't start:** verify `~/.cloudflared/config.yml` path and that
  the credentials JSON referenced exists.

## No-domain option (Quick Tunnel)

If you don't have a domain on Cloudflare, skip step 4 and run a Quick
Tunnel manually for a temporary public URL (changes each run):

```bash
cloudflared tunnel --url http://localhost:8501
```

For an always-on Quick Tunnel, set the cloudflared service `ExecStart` to
`/usr/bin/cloudflared tunnel --url http://localhost:8501` instead of the
config-file form, and read the current URL from `journalctl -u cloudflared`.
````

- [ ] **Step 2: Verify it renders as valid Markdown (no broken fences)**

Run: `python -c "t=open('deploy/README.md',encoding='utf-8').read(); assert t.count('```')%2==0, 'unbalanced fences'; print('README OK')"`
Expected: `README OK`

- [ ] **Step 3: Commit**

```bash
git add deploy/README.md
git commit -m "docs: add Raspberry Pi deployment guide

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 5: Link deployment from the main README

**Files:**
- Modify: `trading-bot/README.md`

- [ ] **Step 1: Read the current README** to find a sensible insertion point.

Run: `python -c "print(open('README.md',encoding='utf-8').read()[:400])"`

- [ ] **Step 2: Append a deployment section** to the end of `README.md`:

```markdown

## Running on a Raspberry Pi (24/7)

To run the bot continuously on a Raspberry Pi with a public, read-only
dashboard accessible from anywhere, see [`deploy/README.md`](deploy/README.md).
It covers the one-shot setup script, systemd services, and a Cloudflare
Tunnel for remote access.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: link Raspberry Pi deployment guide from README

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Final verification (authoring side)

- [ ] **Confirm all deploy artifacts exist and are committed**

Run: `git status --porcelain deploy .streamlit/config.toml README.md`
Expected: no output (clean — everything committed).

- [ ] **Confirm the bash script and units pass static checks**

Run: `bash -n deploy/setup-pi.sh && echo OK`
Expected: `OK`

## On-device acceptance (run on the Pi, after authoring)

These are not part of the Windows authoring session — they verify the real deployment:

- [ ] Clone, run `setup-pi.sh`, create `.env`, configure the tunnel.
- [ ] `sudo systemctl enable --now trading-bot cloudflared` — both `active (running)`.
- [ ] `curl -I http://localhost:8501` returns HTTP 200 on the Pi.
- [ ] The public tunnel URL loads the dashboard from a phone on cellular (off the LAN), confirming "from anywhere" access.
- [ ] Reboot the Pi; both services come back up automatically (`systemctl is-enabled trading-bot cloudflared` → `enabled`).
