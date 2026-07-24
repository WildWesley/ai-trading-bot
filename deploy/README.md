# Raspberry Pi Deployment

Run the trading bot 24/7 on a Raspberry Pi with the dashboard reachable from anywhere via a Cloudflare Tunnel (read-only).

## Prerequisites

- Raspberry Pi 4/5 on Raspberry Pi OS Bookworm (64-bit), on your network
- A free Cloudflare account with a domain added to Cloudflare (needed for a named tunnel + DNS route). If you have no domain, use a Quick Tunnel (see "No-domain option" below) — the URL changes on restart.
- Your Alpaca paper keys and Anthropic key

## 1. Clone the repo

```bash
cd ~
git clone git@github.com:WildWesley/ai-trading-bot.git
cd ai-trading-bot
```

## 2. Run the setup script

```bash
bash deploy/setup-pi.sh
```

This installs Python deps (in `venv/`), Node.js + the Claude Code CLI, `cloudflared`, and the three systemd units (resolved to your user/home).

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
sudo systemctl enable --now trading-bot dashboard cloudflared
systemctl status trading-bot dashboard cloudflared
```

The **trading-bot** service runs the bot headless; the **dashboard** service runs Streamlit on port 8501 (what the tunnel serves); **cloudflared** exposes it.

The dashboard is now at `https://trading-bot.yourdomain.com`. It is read-only — it shows account, positions, trades, equity curve, and the strategy chart, with no controls to place or cancel trades.

## Tunnel watchdog

`cloudflared`'s Quick Tunnel can get stuck in an internal reconnect loop
(repeated "control stream encountered a failure" in `journalctl -u
cloudflared`) without the process ever exiting — so `Restart=on-failure`
never kicks in, and the dashboard can be unreachable from the outside for
days while looking "running" in `systemctl status`.

`tunnel-watchdog.timer` runs `deploy/tunnel-watchdog.sh` every 5 minutes. It
curls the local dashboard and the current public tunnel URL (parsed from the
cloudflared log), and restarts whichever service is unresponsive.

`setup-pi.sh` installs and can enable it; on an existing install:

```bash
sudo cp deploy/tunnel-watchdog.service /etc/systemd/system/
sudo sed -i "/^\[Service\]/a User=$(whoami)" /etc/systemd/system/tunnel-watchdog.service
sudo sed -i "s#%REPO%#$(pwd)#g" /etc/systemd/system/tunnel-watchdog.service
sudo cp deploy/tunnel-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tunnel-watchdog.timer
```

## Updating the bot later

```bash
cd ~/ai-trading-bot
git pull
~/ai-trading-bot/venv/bin/pip install -r requirements.txt
sudo systemctl restart trading-bot
```

## Logs and troubleshooting

```bash
journalctl -u trading-bot -f     # live bot log
journalctl -u dashboard -f      # live dashboard (Streamlit) log
journalctl -u cloudflared -f     # live tunnel log
```

- **Bot exits immediately:** check `.env` exists and keys are valid paper keys (`journalctl -u trading-bot` shows the validation messages).
- **Dashboard 502 via the tunnel:** confirm Streamlit is up on the Pi (`curl -I http://localhost:8501`), ensure the **dashboard** service is running (`systemctl status dashboard`), and that `config.yml` points at port 8501.
- **Tunnel won't start:** verify `~/.cloudflared/config.yml` path and that the credentials JSON referenced exists.

## No-domain option (Quick Tunnel)

If you don't have a domain on Cloudflare, skip step 4 and run a Quick Tunnel manually for a temporary public URL (changes each run):

```bash
cloudflared tunnel --url http://localhost:8501
```

For an always-on Quick Tunnel, set the cloudflared service `ExecStart` to `/usr/bin/cloudflared tunnel --url http://localhost:8501` instead of the config-file form, and read the current URL from `journalctl -u cloudflared`.
