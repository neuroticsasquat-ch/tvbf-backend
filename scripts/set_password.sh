#!/usr/bin/env bash
# Set a local user's password to a known value.
# Usage: ./scripts/set_password.sh <email> [password]   (default password: localdev)

set -euo pipefail

EMAIL="${1:-}"
PASSWORD="${2:-localdev}"

if [[ -z "$EMAIL" ]]; then
  echo "usage: $0 <email> [password]" >&2
  exit 1
fi

LOCAL_PG_CONTAINER="${LOCAL_PG_CONTAINER:-tbc_postgresql_db}"
LOCAL_DB="${LOCAL_DB:-tvbf}"
LOCAL_DB_USER="${LOCAL_DB_USER:-root}"

HASH=$(docker compose exec -T tvbf-backend python -c \
  "from tvbf.app.passwords import hash_password; print(hash_password('$PASSWORD'))")

docker exec -i "$LOCAL_PG_CONTAINER" \
  psql -U "$LOCAL_DB_USER" -d "$LOCAL_DB" \
  -v "hash=$HASH" -v "email=$EMAIL" <<'SQL'
UPDATE app."user" SET password_hash = :'hash' WHERE email = :'email';
SQL

echo "✓ Password updated for $EMAIL (use '$PASSWORD' to log in)."
