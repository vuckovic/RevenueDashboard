#!/usr/bin/env bash
# Install a package made by pack.sh on this machine (needs Docker Desktop running).
# Usage:  ./install.sh [target folder]        default: ~/RevenueDashboard
#         ./install.sh [target folder] --build  rebuild the app image here
#           (use --build if this machine has a different CPU type, e.g. Apple Silicon <-> Intel/Windows)
set -euo pipefail
PKG="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/RevenueDashboard}"
BUILD="${2:-}"
[ "${1:-}" = "--build" ] && { DEST="$HOME/RevenueDashboard"; BUILD="--build"; }

echo "Installing into: $DEST"
mkdir -p "$DEST"
tar xzf "$PKG/source.tar.gz" -C "$DEST"     # never overwrites an existing .env (not in the package)

if [ "$BUILD" != "--build" ]; then
  echo "Loading Docker images"
  gunzip -c "$PKG/images.tar.gz" | docker load
fi

cd "$DEST"
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created $DEST/.env - fill in the passwords (from your password manager), then run this again:"
  echo "  \"$PKG/install.sh\" \"$DEST\" $BUILD"
  exit 1
fi

echo "Starting database"
docker compose up -d db
until docker compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; do sleep 2; done

if [ -f "$PKG/revenue.dump" ]; then
  echo "Restoring data"
  docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
    < "$PKG/revenue.dump" || echo "(pg_restore reported warnings - usually harmless on a first install)"
fi

echo "Starting web + worker"
if [ "$BUILD" = "--build" ]; then docker compose up -d --build; else docker compose up -d; fi
echo
echo "Ready: http://localhost:8000  and  http://localhost:8000/cashflow"
