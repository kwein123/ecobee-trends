#!/usr/bin/env bash
#
# Ecobee Trends — first-time setup.
#
#   ./setup.sh            connect a real ecobee account and start logging
#   ./setup.sh --demo     skip the account; generate synthetic data and browse
#
# Everything it needs is asked for interactively. Defaults are in [brackets] —
# press Enter to accept. Nothing is sent anywhere except to ecobee's own API.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
warn() { printf '%s!  %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '\n%sError: %s%s\n' "$YELLOW" "$*" "$OFF" >&2; exit 1; }

cd "$(dirname "$0")"
DEMO=no
[ "${1:-}" = "--demo" ] && DEMO=yes

say "${BOLD}Ecobee Trends setup${OFF}"
say "${DIM}Logs your ecobee thermostats every few minutes and charts the history.${OFF}"

# ---------------------------------------------------------------- prerequisites
step "Checking prerequisites"
if ! command -v docker >/dev/null 2>&1; then
  die "Docker is not installed. Install Docker Desktop (Mac/Windows) or Docker Engine (Linux), then re-run this script.
     https://docs.docker.com/get-started/get-docker/"
fi
if ! docker compose version >/dev/null 2>&1; then
  die "Docker is installed but 'docker compose' is not available. Update Docker to a current version."
fi
if ! docker info >/dev/null 2>&1; then
  die "Docker is installed but not running. Start Docker and re-run this script."
fi
say "${GREEN}✓${OFF} Docker is installed and running"

# ---------------------------------------------------------------------- config
step "Configuration"
DEFAULT_PORT=8321
read -r -p "Port to serve the dashboard on [${DEFAULT_PORT}]: " PORT
PORT="${PORT:-$DEFAULT_PORT}"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "'$PORT' is not a number."

if [ "$DEMO" = "no" ]; then
  DEFAULT_INTERVAL=300
  read -r -p "Seconds between polls [${DEFAULT_INTERVAL}]: " INTERVAL
  INTERVAL="${INTERVAL:-$DEFAULT_INTERVAL}"
  [[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "'$INTERVAL' is not a number."
  [ "$INTERVAL" -lt 180 ] && warn "ecobee only refreshes its cloud data every few minutes; under 180s just repeats readings."
else
  INTERVAL=300
fi

cat > .env <<EOF
# Written by setup.sh — safe to edit, then: docker compose up -d
DASHBOARD_PORT=$PORT
POLL_INTERVAL=$INTERVAL
# The containers run as you, so they can write to ./data (needed on Linux).
ECOBEE_UID=$(id -u)
ECOBEE_GID=$(id -g)
EOF
say "${GREEN}✓${OFF} Wrote .env"

mkdir -p data/auth data/db
chmod 700 data/auth

# ------------------------------------------------------------------- build
step "Building the container image (first run takes a minute)"
docker compose build --quiet
say "${GREEN}✓${OFF} Image built"

# ------------------------------------------------------------------- demo mode
if [ "$DEMO" = "yes" ]; then
  step "Generating demo data (no ecobee account used)"
  docker compose run --rm --no-deps -T logger \
    python tools/make_demo_db.py --db /data/db/ecobee.sqlite3
  step "Starting the dashboard"
  docker compose up -d dashboard
  say ""
  say "${GREEN}Done.${OFF} Demo dashboard: ${BOLD}http://localhost:${PORT}/${OFF}"
  say "${DIM}This is synthetic data for three fictional thermostats."
  say "When you want real data: docker compose down && ./setup.sh${OFF}"
  exit 0
fi

# ------------------------------------------------------------------- ecobee login
step "Connecting your ecobee account"
say "You'll be asked for your ecobee.com email and password (and a"
say "verification code if your account uses two-factor authentication)."
say "${DIM}Your password is used once to obtain access tokens and is NOT stored."
say "Only the tokens are saved, in data/auth/ecobee.conf on this machine.${OFF}"
say ""

if ! docker compose run --rm --no-deps logger \
      python app/ecobee_login.py --config /data/auth/ecobee.conf; then
  die "Login did not complete. Re-run ./setup.sh to try again."
fi

# ------------------------------------------------------------------- start
step "Starting logger and dashboard"
docker compose up -d
sleep 3

if docker compose ps --status running --format '{{.Service}}' | grep -q logger; then
  say "${GREEN}✓${OFF} Logger is running"
else
  warn "Logger is not running. Check: docker compose logs logger"
fi

say ""
say "${GREEN}Done.${OFF} Dashboard: ${BOLD}http://localhost:${PORT}/${OFF}"
say ""
say "The first poll happens immediately; charts fill in as readings accumulate"
say "(a few hours makes them interesting, a few days more so)."
say ""
say "${BOLD}Useful commands${OFF}"
say "  docker compose logs -f logger      watch it poll"
say "  docker compose ps                  service status"
say "  docker compose down                stop"
say "  docker compose up -d               start again"
say ""
say "Rename or reorder thermostats on the dashboard:"
say "  docker compose exec logger python app/ecobee_units.py list"
say ""
say "New to the dashboard? docs/user-guide.md explains how to read it."
