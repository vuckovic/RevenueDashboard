#!/usr/bin/env bash
# Pack the whole stack for another machine: Docker images + source + database dump.
# Usage (from the project folder, with the containers running):  ./scripts/pack.sh
# Result: dist/revenuedashboard-<date>.tar.gz   (contains customer data - keep it confidential)
# .env (passwords) is NOT included - move those via your password manager.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="revenuedashboard-$(date +%Y%m%d-%H%M)"
WORK="dist/$NAME"
mkdir -p "$WORK"

echo "1/4 Building app image"
docker compose build

echo "2/4 Saving images (app + postgres)"
docker save revenuedashboard-app:latest postgres:16-alpine | gzip > "$WORK/images.tar.gz"

echo "3/4 Dumping database"
docker compose up -d db >/dev/null
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$WORK/revenue.dump"

echo "4/4 Packing source (without .env, .git, dist)"
tar czf "$WORK/source.tar.gz" \
  --exclude='./.env' --exclude='./.git' --exclude='./dist' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' .
cp scripts/install.sh "$WORK/install.sh"
chmod +x "$WORK/install.sh"

tar czf "dist/$NAME.tar.gz" -C dist "$NAME"
rm -rf "$WORK"
echo
echo "Done: dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
echo "Copy it to the other machine, then:"
echo "  tar xzf $NAME.tar.gz && cd $NAME && ./install.sh"
