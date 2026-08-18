#!/usr/bin/env bash
# One command to run the patient simulator against the agent under test.
# Starts the public tunnel + relay server, does the work, tears down.
#
#   ./run.sh                                  # all scenarios -> FINDINGS.md
#   ./run.sh scenarios/11_*.yaml              # just these scenarios
#   ./run.sh tune                             # tune the caller to sound more human
#   ./run.sh attack                           # evolve a red-team attacker
#
# Requires: .venv set up, .env filled in, cloudflared installed.
set -euo pipefail
cd "$(dirname "$0")"

# pick what to run once the tunnel + server are up
case "${1:-}" in
  tune)   MODE=(./.venv/bin/python src/tuner.py);        shift ;;
  attack) MODE=(./.venv/bin/python src/attack_tuner.py); shift ;;
  *)      MODE=(./.venv/bin/python src/runner.py) ;;
esac

CF_PID=""; SRV_PID=""
cleanup() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
            [ -n "$CF_PID" ] && kill "$CF_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 1. public tunnel
cloudflared tunnel --url http://localhost:5050 > /tmp/cf.log 2>&1 &
CF_PID=$!
for _ in $(seq 1 20); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf.log | head -1) && [ -n "$URL" ] && break
  sleep 1
done
[ -z "${URL:-}" ] && { echo "tunnel failed"; cat /tmp/cf.log; exit 1; }
export PUBLIC_BASE_URL="${URL#https://}"
echo "tunnel: $PUBLIC_BASE_URL"

# 2. relay server (reads keys + STREAM_SECRET from .env)
./.venv/bin/python -m uvicorn server:app --app-dir src --host 0.0.0.0 --port 5050 > /tmp/server.log 2>&1 &
SRV_PID=$!
for _ in $(seq 1 20); do curl -sf "https://$PUBLIC_BASE_URL/health" >/dev/null && break; sleep 1; done
curl -sf "https://$PUBLIC_BASE_URL/health" >/dev/null || { echo "server unhealthy"; cat /tmp/server.log; exit 1; }
echo "server: healthy"

# 3. run the selected job (scenario suite / tuner / attacker)
"${MODE[@]}" "$@"
