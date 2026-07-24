#!/usr/bin/env bash
# Health-checks the Streamlit dashboard and its Cloudflare Quick Tunnel, and
# restarts whichever is broken.
#
# Why this exists: cloudflared's Quick Tunnel can get stuck in an internal
# reconnect loop (repeated "control stream encountered a failure") without
# ever exiting the process. Because it never exits, systemd's
# Restart=on-failure never fires, so the tunnel can sit dead for days with
# no automatic recovery. Run via the tunnel-watchdog.timer on a short
# interval to catch that case.
set -euo pipefail

if ! curl -fsS -o /dev/null -m 5 http://localhost:8501; then
  echo "dashboard on :8501 is not responding, restarting dashboard.service"
  sudo systemctl restart dashboard
  exit 0
fi

TUNNEL_URL="$(journalctl -u cloudflared --no-pager -n 200 2>/dev/null \
  | grep -oE 'https://[a-z-]+\.trycloudflare\.com' | tail -1)"

if [ -z "$TUNNEL_URL" ]; then
  echo "no tunnel URL found in recent cloudflared logs yet, skipping this check"
  exit 0
fi

if ! curl -fsS -o /dev/null -m 10 "$TUNNEL_URL"; then
  echo "tunnel URL $TUNNEL_URL is not responding, restarting cloudflared.service"
  sudo systemctl restart cloudflared
fi
